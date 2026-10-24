"""Public, conditional close/restore obligations; never actuator shortcuts.

The engine owns order publication and physics. This module owns only the ledger,
public policy checks and evidence emitted by the common engine substeps.
"""
from copy import deepcopy

from .capabilities import EPS, finite, unknown_fields
from .recovery import receiving_status


COMMITMENT_KEYS = ('id', 'firm', 'target', 'created_minutes', 'deadline_minutes',
    'closing_order_id', 'restore_order_id', 'status', 'off_effective_minutes',
    'restore_dispatched_minutes', 'restore_effective_minutes', 'milestone_minutes',
    'milestone_kind', 'reasons', 'reservation_active', 'route_reserved',
    'bucket_end_minutes', 'initial_uncontained', 'projected_milestone_minutes',
    'projected_milestone_kind', 'milestone_evidence')
EVIDENCE_KEYS = ('physical_rate', 'running', 'stock', 'window_eligible', 'start_eligible')


def enabled(view):
    return view.get('restart_guard', {}).get('enabled') is True


def public_records(records):
    result = []
    for record in records:
        item = {key: deepcopy(record[key]) for key in COMMITMENT_KEYS if key in record}
        if isinstance(item.get('milestone_evidence'), dict):
            item['milestone_evidence'] = {key: item['milestone_evidence'][key]
                for key in EVIDENCE_KEYS if key in item['milestone_evidence']}
        result.append(item)
    return result


def outstanding(view):
    return [c for c in view.get('restart_commitments', []) if c.get('route_reserved')]


def reserved(view):
    return sum(bool(c.get('reservation_active')) for c in view.get('restart_commitments', [])) if enabled(view) else 0


def new_closures(view, rates):
    if not enabled(view) or not isinstance(rates, dict): return []
    firms = view['firms']
    firms = firms if isinstance(firms, dict) else {f['id']: f for f in firms}
    locked = {c['firm'] for c in outstanding(view)}
    return [fid for fid, target in rates.items() if fid in firms and
        finite(target) and target == 0 and firms[fid]['rate'] > EPS and fid not in locked and
        not any(o['firm'] == fid and o['kind'] == 'rate' and o['status'] in ('notified', 'acknowledged')
                and o['target'] == 0 for o in view.get('orders', []))]


def deadline(view):
    b = view.get('notice_budget', {})
    width = b.get('window_minutes')
    report = view.get('recovery', {}).get('capacity_confirmation') or {}
    if not finite(width) or width <= 0 or not finite(report.get('valid_until_minutes')):
        return None
    return min(view['minute'] + 240, view['end_tick'] * 15,
        report['valid_until_minutes'], (view['minute'] // width + 1) * width)


def register(s, orders, projections, emit):
    if not enabled(s): return
    for projection in projections:
        fid = projection['firm']
        order = next(o for o in orders if o['firm'] == fid and o['kind'] == 'rate' and o['target'] == 0)
        c = dict(id='R' + str(len(s['restart_commitments']) + 1), firm=fid,
            target=projection['target'], created_minutes=s['minute'],
            deadline_minutes=projection['deadline_minutes'], closing_order_id=order['id'],
            restore_order_id=None, status='closing', off_effective_minutes=None,
            restore_dispatched_minutes=None, restore_effective_minutes=None,
            milestone_minutes=None, milestone_kind=None, reasons=[],
            reservation_active=True, route_reserved=True,
            bucket_end_minutes=projection['bucket_end_minutes'],
            initial_uncontained=s['firms'][fid]['uncontained'],
            projected_milestone_minutes=projection.get('projected_milestone_minutes'),
            projected_milestone_kind=projection.get('projected_milestone_kind'),
            milestone_evidence=None)
        s['restart_commitments'].append(c)
        emit(s, 'restart_reserved', 'conditional_public_projection', commitment_id=c['id'],
            firm=fid, target=c['target'], deadline_minutes=c['deadline_minutes'])


def note(s, c, kind, reason, emit, minute=None, **details):
    if kind in ('restart_endangered', 'restart_missed', 'restart_failed', 'restart_cancelled') and reason not in c['reasons']:
        c['reasons'].append(reason)
    emit(s, kind, reason, minute=minute, commitment_id=c['id'], firm=c['firm'],
        target=c['target'], deadline_minutes=c['deadline_minutes'], **details)


def rejection(s, order, emit):
    for c in outstanding(s):
        if order['id'] == c['closing_order_id']:
            c.update(status='cancelled', reservation_active=False, route_reserved=False)
            note(s, c, 'restart_cancelled', 'closing_order_rejected_unused_reservation', emit)
        elif order['id'] == c['restore_order_id']:
            c['status'] = 'failed'
            note(s, c, 'restart_failed', 'restore_order_rejected_no_retry', emit)


def after_execution(s, publish_basic, emit):
    """Dispatch through the real publisher after the execution iterator finishes."""
    if not enabled(s): return
    orders = {o['id']: o for o in s['orders']}
    for c in outstanding(s):
        close = orders.get(c['closing_order_id'])
        if not close or close['status'] != 'effective' or c['restore_order_id'] or c['status'] == 'failed':
            continue
        c['off_effective_minutes'] = close['effective_at_minutes']
        try:
            made = publish_basic(s, {c['firm']: c['target']}, None, _commitment=c)
        except ValueError as exc:
            c['status'] = 'failed'
            note(s, c, 'restart_failed', 'restore_dispatch_rejected:' + str(exc), emit)
            continue
        if len(made) != 1:
            c['status'] = 'failed'
            note(s, c, 'restart_failed', 'restore_dispatch_missing_order', emit)
            continue
        c.update(restore_order_id=made[0]['id'], restore_dispatched_minutes=s['minute'],
            reservation_active=False)
        if c['status'] != 'missed': c['status'] = 'restore_dispatched'
        note(s, c, 'restart_dispatched', 'ordinary_charged_notice', emit, order_id=made[0]['id'])


def eligibility(s, f, now):
    """Check the same known local start/window restrictions, including empty tanks."""
    p = s['capabilities'].get(f['id'], {}); a = f['actuator']
    if unknown_fields(p): return False, False
    window = p['mode'] != 'window' or any(start <= now < end and
        (a['running'] or now + p['min_on_minutes'] <= end)
        for start, end in p['windows_minutes'])
    start = (a['running'] or (now >= a['last_stop_minutes'] + p['min_off_minutes'] and
        len([t for t in a['start_minutes'] if t > now - 60]) < p['max_starts_per_hour']))
    return window, start


def after_physics(s, emit, cell_released):
    if not enabled(s): return
    now = s['minute']; end = now + 5
    orders = {o['id']: o for o in s['orders']}
    for c in outstanding(s):
        if c['status'] == 'failed': continue
        f = s['firms'][c['firm']]; a = f['actuator']
        report = receiving_status(s['recovery'], now)
        problem = None
        if not report['known']: problem = report['reason']
        elif unknown_fields(s['capabilities'].get(c['firm'], {})): problem = 'capability_unknown'
        elif any(not finite(f.get(key)) for key in ('ack_delay', 'switch_delay', 'stock',
                'capacity', 'production_rate', 'production_multiplier')) or not isinstance(f.get('production_enabled'), bool):
            problem = 'required_observation_unknown'
        elif f['uncontained'] > c['initial_uncontained'] + EPS: problem = 'new_firm_overflow_before_restore'
        close = orders.get(c['closing_order_id']); restore = orders.get(c['restore_order_id'])
        if close and close['status'] in ('notified', 'acknowledged') and close['earliest_minutes'] >= c['deadline_minutes']:
            problem = problem or 'delayed_close_endangers_deadline'
        if restore and restore['status'] in ('notified', 'acknowledged') and restore['earliest_minutes'] >= c['deadline_minutes']:
            problem = problem or 'delayed_restore_endangers_deadline'
        if problem and problem not in c['reasons']:
            if c['status'] != 'missed': c['status'] = 'endangered'
            note(s, c, 'restart_endangered', problem, emit)
        if restore and restore['status'] == 'effective':
            c['restore_effective_minutes'] = restore['effective_at_minutes']
            window, start = eligibility(s, f, now)
            evidence = dict(physical_rate=a['physical_rate'], running=a['running'], stock=f['stock'],
                window_eligible=window, start_eligible=start)
            actual = (cell_released.get(c['firm'], 0) > EPS and a['running'] and
                abs(a['physical_rate'] - c['target']) <= EPS)
            empty = f['stock'] <= EPS and window and start
            # Transient endangerment remains visible even when recovery later
            # meets the original deadline. Late real recovery releases locks
            # while retaining the missed obligation and its original deadline.
            if (actual or empty) and not problem and end <= c['deadline_minutes'] and c['status'] != 'missed':
                kind = 'restored' if actual else 'ready_empty'
                c.update(status=kind, milestone_kind=kind, milestone_minutes=end,
                    milestone_evidence=evidence, route_reserved=False)
                note(s, c, 'restart_' + kind, 'physical_target_and_operating_opportunity' if actual else
                    'empty_positive_target_known_start_window', emit, minute=end, **evidence)
                continue
            if (actual or empty) and problem in (None, 'new_firm_overflow_before_restore'):
                if end > c['deadline_minutes']:
                    if c['status'] != 'missed':
                        c['status'] = 'missed'
                        note(s, c, 'restart_missed', 'original_deadline_unfulfilled', emit, minute=end)
                    label = 'restored' if actual else 'ready_empty'
                    c.update(milestone_kind=label+'_late', milestone_minutes=end,
                        milestone_evidence=evidence, route_reserved=False)
                    note(s, c, 'restart_late_'+label, 'real_recovery_after_original_deadline', emit, minute=end, **evidence)
                else:
                    c.update(status='failed', milestone_kind='restored_after_breach' if actual else 'ready_empty_after_breach',
                        milestone_minutes=end, milestone_evidence=evidence, route_reserved=False)
                    note(s, c, 'restart_failed', 'firm_overflow_breached_conditional_safeguard', emit, minute=end)
                    note(s, c, 'restart_recovered_after_breach', 'physical_recovery_does_not_erase_overflow', emit, minute=end, **evidence)
                continue
        if end >= c['deadline_minutes'] and c['status'] != 'missed':
            c['status'] = 'missed'
            note(s, c, 'restart_missed', 'original_deadline_unfulfilled', emit, minute=end)

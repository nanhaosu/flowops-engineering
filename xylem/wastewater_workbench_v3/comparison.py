"""Frozen, resumable 48h comparison. All results concern synthetic water volume.

python -m xylem.wastewater_workbench_v3.comparison --freeze --output DIR
python -m xylem.wastewater_workbench_v3.comparison --run --output DIR
python -m xylem.wastewater_workbench_v3.comparison --audit --output DIR

Completed results are immutable. Only explicitly paused runs can resume, with
identical sources and protocol. --max-new-runs is an intentional pause gate.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from .scenarios import build_protocol, canonical_hash, END_TICK

HERE = Path(__file__).resolve().parent
OUT = HERE / 'artifacts/restart_v3'
LEGACY_RESULTS = HERE.parent / 'wastewater_workbench_v2/artifacts/executable_v2_r2/results.json'
EPS = 1e-6
METHODS = ('hold', 'simple', 'preview')
PAIR_METRICS = ('uncontained', 'received', 'stored', 'terminal_transit',
                'late_uncontained', 'stored_area_m3_h', 'notices', 'setpoint_changes',
                'physical_starts', 'physical_stops', 'runtime_minutes',
                'unknown_decisions', 'deferred_decisions', 'compute_ms')
RESTART_METRICS = ('restart_reservations_created', 'restart_notices', 'restart_restored',
    'restart_ready_empty', 'restart_deadline_missed', 'restart_failed', 'restart_rejected',
    'restart_cancelled', 'restart_endangered', 'restart_outstanding_reserved_slots',
    'restart_candidate_guard_refusals', 'restart_guard_rejected_decisions',
    'restart_late_restored', 'restart_late_ready_empty', 'restart_recovered_after_breach')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_hashes():
    return {path.relative_to(HERE).as_posix(): sha(path)
            for path in sorted(HERE.rglob('*.py')) if 'artifacts' not in path.parts}


def _json_bytes(value, pretty=False):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      indent=2 if pretty else None,
                      separators=None if pretty else (',', ':')).encode('utf-8')


def _create_json(path, value):
    """Exclusive creation: neither completions nor existing trace files change."""
    with Path(path).open('xb') as stream:
        stream.write(_json_bytes(value, pretty=True))


def _replace_state(path, value):
    temporary = Path(path).with_suffix('.pending')
    temporary.write_bytes(_json_bytes(value, pretty=True))
    temporary.replace(path)


def validate_frozen(frozen):
    if canonical_hash(frozen['protocol']) != frozen['protocol_sha256']:
        raise RuntimeError('Frozen protocol hash mismatch')
    if _source_hashes() != frozen['source_sha256']:
        raise RuntimeError('Sources changed after freezing; use an explicit new result version')
    if frozen.get('legacy_results_sha256') != sha(LEGACY_RESULTS):
        raise RuntimeError('Immutable v2 r2 reference results changed')


def freeze_protocol(path=OUT / 'protocol.json', protocol=None):
    path = Path(path)
    protocol = build_protocol() if protocol is None else deepcopy(protocol)
    hashes = _source_hashes()
    if path.exists():
        frozen = json.loads(path.read_text(encoding='utf-8'))
        validate_frozen(frozen)
        if canonical_hash(protocol) != frozen['protocol_sha256']:
            raise RuntimeError('Protocol changed; preserve existing freeze and use new output directory')
        return frozen
    path.parent.mkdir(parents=True, exist_ok=True)
    result = dict(frozen_at_utc=datetime.now(timezone.utc).isoformat(),
                  protocol=protocol, protocol_sha256=canonical_hash(protocol), source_sha256=hashes,
                  legacy_results_sha256=sha(LEGACY_RESULTS))
    _create_json(path, result)
    return result


def inventory(history_row):
    return (sum(history_row['firm_stocks'].values()) +
            sum(history_row['route_stocks'].values()) + history_row['transit'])


def _sum(value):
    return sum(value.values()) if isinstance(value, dict) else value


def reveal_boundary(sim):
    """Reveal only at boundaries, preserving prior history and pending orders."""
    if sim['tick'] == 64:
        sim['end_tick'] = END_TICK
        sim['events'].append(dict(tick=64, minute=960, kind='observation_extension',
                                  reason='original observation finished; continue to48h'))
    if sim['tick'] == 96:
        sim['production_end'] = 144
        sim['events'].append(dict(tick=96, minute=1440, kind='production_shift',
                                  reason='second shift now confirmed; production through tick144'))


def _guard_caused_refusal(view, rates, admitted):
    """Counterfactual cheap gate, matching the planner's candidate attribution."""
    if admitted or view.get('restart_guard', {}).get('enabled') is not True:
        return False
    from .capabilities import assess_bundle
    control = deepcopy(view)
    control['restart_guard'] = {'enabled': False}
    return assess_bundle(control, rates)['allowed']


def admit_bundle(sim, rates, production=None):
    """Common atomic physical publisher; all planners already see its quota."""
    from .engine import public_state, publish, assess_action
    if production:
        raise AssertionError('Production changes are outside this comparison')
    view = public_state(sim)
    assessment = assess_action(view, rates, production)
    guard_caused_refusal = _guard_caused_refusal(view, rates, assessment['allowed'])
    applied = deepcopy(rates) if assessment['allowed'] else {}
    orders = publish(sim, applied, production={})
    return dict(admitted=bool(assessment['allowed']), assessment=assessment,
                guard_caused_refusal=guard_caused_refusal,
                applied_rates=applied, created_ids=[order['id'] for order in orders])


def run_controller(config, method):
    from .engine import new_simulation, public_state, advance, mass_balance
    from .planning import recommend
    if method not in METHODS:
        raise ValueError('Unknown method')
    cfg = deepcopy(config)
    if cfg['end_tick'] != 64 or cfg['production_end'] != 48:
        raise ValueError('This protocol starts with16h observation and first12h shift')
    sim = new_simulation(cfg)
    decisions = []
    while sim['tick'] < END_TICK:
        reveal_boundary(sim)
        if sim['tick'] % 4 == 0:
            view = public_state(sim)
            started = perf_counter()
            if method == 'hold':
                action = dict(rates={}, production={}, method='hold', status='conditional_simulation',
                              executable_in_field=False, reason='retain effective arrangement',
                              horizon=16, evaluated_candidates=0, predicted_objective=None,
                              capability_reasons=[])
            else:
                action = recommend(view, method=method)
            elapsed = (perf_counter() - started) * 1000
            if action.get('production'):
                raise AssertionError('Planner attempted to change production')
            if action.get('executable_in_field') is not False:
                raise AssertionError('Synthetic action must explicitly deny field executability')
            admission = admit_bundle(sim, action['rates'], action.get('production'))
            decisions.append(dict(tick=sim['tick'], minute=sim['tick'] * 15,
                                  visible_input=view, input_sha256=canonical_hash(view),
                                  action=action, action_sha256=canonical_hash(action),
                                  admission=admission, compute_ms=elapsed))
        advance(sim, 1)
    trace = dict(schema=3, method=method, configuration_sha256=canonical_hash(cfg),
                 initial=sim['initial'], initial_rates={fid: firm['rate']
                                                      for fid, firm in cfg['firms'].items()},
                 decisions=decisions, history=sim['history'], orders=sim['orders'],
                 events=sim['events'], world_events=sim.get('world_events', []),
                 restart_guard=deepcopy(sim.get('restart_guard', cfg.get('restart_guard', {}))),
                 restart_commitments=deepcopy(sim.get('restart_commitments', [])),
                 terminal_transit=sim['transit'], terminal_balance=mass_balance(sim))
    trace['metrics'] = derive_metrics(trace)
    trace['phases'] = {name: phase_summary(trace['history'], start, stop)
                       for name, start, stop in [('original16h', 0, 64), ('after16h', 64, 192),
                           ('pre_second_shift', 64, 96), ('second_production', 96, 144),
                           ('final_clearance', 144, 192)]}
    return trace


def phase_summary(history, start, stop):
    a, b = history[start], history[stop]
    return dict(start_tick=start, end_tick=stop,
                generated=b['generated'] - a['generated'],
                received=b['received'] - a['received'],
                uncontained=b['uncontained'] - a['uncontained'],
                start_stored=inventory(a), end_stored=inventory(b),
                max_stored=max(inventory(h) for h in history[start:stop + 1]),
                stored_area_m3_h=sum((inventory(history[t]) + inventory(history[t + 1])) * .125
                                     for t in range(start, stop)))


def derive_metrics(trace):
    """Derive scalar outcomes from saved history/events, without simulator totals."""
    from .capabilities import profile_status
    history, events, decisions = trace['history'], trace['events'], trace['decisions']
    first, last = history[0], history[-1]
    current, signs = deepcopy(trace['initial_rates']), {fid: 0 for fid in trace['initial_rates']}
    changes, variation, reversals = 0, 0., 0
    for event in events:
        if event['kind'] == 'setpoint':
            fid = event['firm']
            value = event['value']
            delta = value - current[fid]
            sign = 1 if delta > EPS else (-1 if delta < -EPS else 0)
            if sign:
                changes += 1
                variation += abs(delta)
                reversals += bool(signs[fid] and signs[fid] != sign)
                signs[fid] = sign
            current[fid] = value
    unknown_capacity = sum(d['visible_input'].get('downstream_capacity') is None for d in decisions)
    needs_confirmation = [d['action'].get('status') == 'needs_confirmation' for d in decisions]
    unknown_profiles = [bool(profile_status(d['visible_input'])['unknown_fields']) for d in decisions]
    unknown_decisions = sum(needs or unknown_profile or d['visible_input'].get('downstream_capacity') is None
                            for d, needs, unknown_profile in zip(decisions, needs_confirmation, unknown_profiles))
    reasons = Counter()
    for decision in decisions:
        for reason in decision['admission']['assessment'].get('reasons', []):
            reasons[str(reason)] += 1
        for reason in decision['action'].get('capability_reasons', []):
            reasons[str(reason)] += 1
    errors = [abs(trace['initial'] + h['generated'] - h['received'] -
                  h['uncontained'] - inventory(h)) for h in history]
    orders = trace['orders']
    return dict(generated=last['generated'], received=last['received'],
                uncontained=last['uncontained'], prevented=last['prevented'],
                stored=inventory(last), terminal_transit=last['transit'],
                firm_stored=sum(last['firm_stocks'].values()),
                middle_stored=sum(last['route_stocks'].values()),
                stored_area_m3_h=phase_summary(history, 0, END_TICK)['stored_area_m3_h'],
                late_uncontained=last['uncontained'] - history[64]['uncontained'],
                notices=len(orders), setpoint_changes=changes,
                rate_variation_m3_per_h=variation, reversals=reversals,
                physical_starts=sum(e['kind'] == 'start' for e in events),
                physical_stops=sum(e['kind'] == 'stop' for e in events),
                runtime_minutes=_sum(last['runtime_minutes']) - _sum(first['runtime_minutes']),
                runtime_minutes_by_firm={fid: last['runtime_minutes'][fid] - first['runtime_minutes'][fid]
                                         for fid in trace['initial_rates']},
                unknown_decisions=unknown_decisions,
                needs_confirmation_decisions=sum(needs_confirmation),
                capability_unknown_decisions=sum(unknown_profiles),
                receiving_unknown_decisions=unknown_capacity,
                deferred_decisions=sum(not d['admission']['admitted'] or
                                       d['action'].get('status') == 'needs_confirmation'
                                       for d in decisions),
                deferred_actuations=sum(e['kind'] == 'deferred' for e in events),
                constrained_action_reasons=dict(sorted(reasons.items())),
                pending=sum(o['status'] in ('notified', 'acknowledged') for o in orders),
                daily_notices=[sum(day * 96 <= o['created'] < (day + 1) * 96 for o in orders)
                               for day in range(2)],
                max_abs_balance_error=max(errors), balance_error=errors[-1],
                compute_ms=sum(d['compute_ms'] for d in decisions),
                compute_mean_ms=sum(d['compute_ms'] for d in decisions) / len(decisions),
                compute_max_ms=max(d['compute_ms'] for d in decisions),
                decisions=len(decisions), horizon_ticks=last['tick'], **restart_metrics(trace))


def restart_metrics(trace):
    """Tokens are obligations; only the saved ordinary order list counts notices."""
    commitments = trace.get('restart_commitments', [])
    events = trace['events']
    restore_ids = {c.get('restore_order_id') for c in commitments} - {None}
    def count(kind):
        return len({e['commitment_id'] for e in events if e['kind'] == kind})
    return dict(restart_reservations_created=count('restart_reserved'),
        restart_notices=sum(o['id'] in restore_ids for o in trace['orders']),
        restart_restored=count('restart_restored'), restart_ready_empty=count('restart_ready_empty'),
        restart_deadline_missed=count('restart_missed'), restart_failed=count('restart_failed'),
        restart_cancelled=count('restart_cancelled'), restart_endangered=count('restart_endangered'),
        restart_late_restored=count('restart_late_restored'),
        restart_late_ready_empty=count('restart_late_ready_empty'),
        restart_recovered_after_breach=count('restart_recovered_after_breach'),
        restart_rejected=sum(o['id'] in restore_ids and o['status'] == 'rejected' for o in trace['orders']),
        restart_outstanding_reserved_slots=sum(bool(c.get('reservation_active')) for c in commitments),
        restart_candidate_guard_refusals=sum(d['action'].get('restart_guard_refusals', 0)
                                             for d in trace['decisions']),
        restart_guard_rejected_decisions=sum(_guard_caused_refusal(
            d['visible_input'], d['action'].get('rates', {}), d['admission']['admitted'])
            for d in trace['decisions']))


def audit_control_compatibility(rows, *, require_complete=True):
    """Every original scalar/vector/reason metric must match, except compute time."""
    baseline = json.loads(LEGACY_RESULTS.read_text(encoding='utf-8'))
    old = {(r['group'], r['case_id']): r for r in baseline['rows']}
    checked = set()
    for row in rows:
        if not row['group'].startswith('control__'):
            continue
        key = (row['group'].removeprefix('control__'), row['case_id'])
        assert key in old, 'Unknown control reference: ' + str(key)
        for method in METHODS:
            if method not in row:
                continue
            for metric, value in old[key][method].items():
                if not metric.startswith('compute_'):
                    assert row[method][metric] == value, (key, method, metric,
                                                         row[method][metric], value)
            checked.add((*key, method))
    if require_complete:
        assert len(checked) == len(old) * len(METHODS) == 384
    return len(checked)


def classify(delta):
    return 'win' if delta < -EPS else ('loss' if delta > EPS else 'tie')


def pair_comparison(a, b):
    delta = {key: a[key] - b[key] for key in PAIR_METRICS}
    delta['outcome'] = classify(delta['uncontained'])
    delta['not_worse_water_storage_notices'] = all(delta[k] <= EPS
                                                  for k in ('uncontained', 'stored', 'notices'))
    return delta


def summarize(rows, methods=METHODS):
    groups = {}
    for group in dict.fromkeys(row['group'] for row in rows):
        subset = [row for row in rows if row['group'] == group]
        comparisons = {}
        for a, b in (('preview', 'simple'), ('preview', 'hold'), ('simple', 'hold')):
            if a not in methods or b not in methods:
                continue
            pair = a + '_vs_' + b
            comparisons[pair] = dict(
                wins=sum(r[pair]['outcome'] == 'win' for r in subset),
                ties=sum(r[pair]['outcome'] == 'tie' for r in subset),
                losses=sum(r[pair]['outcome'] == 'loss' for r in subset),
                loss_case_ids=[r['case_id'] for r in subset if r[pair]['outcome'] == 'loss'],
                tie_case_ids=[r['case_id'] for r in subset if r[pair]['outcome'] == 'tie'],
                deltas={key: sum(r[pair][key] for r in subset) for key in PAIR_METRICS})
        groups[group] = dict(cases=len(subset), comparisons=comparisons,
            totals={method: {key: sum(row[method][key] for row in subset)
                             for key in ('generated', 'prevented', *PAIR_METRICS, *RESTART_METRICS)} for method in methods})
    return groups


def _trace_key(group, case, method):
    return f'{group}--{case}--{method}'


def _read_trace(output, reference):
    compressed = (Path(output) / reference['path']).read_bytes()
    if hashlib.sha256(compressed).hexdigest() != reference['sha256']:
        raise RuntimeError('Compressed trace hash mismatch')
    raw = gzip.decompress(compressed)
    if hashlib.sha256(raw).hexdigest() != reference['uncompressed_sha256']:
        raise RuntimeError('Uncompressed trace hash mismatch')
    return json.loads(raw)


def _adopt_orphan_traces(frozen, output, state):
    """Close the atomic-file/checkpoint interruption window without re-running."""
    expected = {}
    protocol = frozen['protocol']
    for group in protocol['groups']:
        for case in protocol['cases']:
            config = protocol['configurations'][group['id'] + '/' + case['id']]
            for method in protocol['methods']:
                expected[_trace_key(group['id'], case['id'], method)] = (config, method)
    for path in sorted((output / 'traces').glob('*.json.gz')):
        key = path.name.removesuffix('.json.gz')
        if key in state['completed']:
            continue
        if key not in expected:
            raise RuntimeError('Unexpected orphan trace; preserve and inspect this output directory')
        compressed = path.read_bytes()
        raw = gzip.decompress(compressed)
        trace = json.loads(raw)
        config, method = expected[key]
        assert trace['method'] == method
        # A complete unpublished trace must replay the frozen physical case;
        # its presence is never accepted merely on matching filename.
        audit_trace(trace, config, replay=True)
        state['completed'][key] = dict(path='traces/' + path.name,
            sha256=hashlib.sha256(compressed).hexdigest(),
            uncompressed_sha256=hashlib.sha256(raw).hexdigest())


def _build_rows(frozen, output, state):
    rows = []
    protocol = frozen['protocol']
    for group in protocol['groups']:
        for case in protocol['cases']:
            row = dict(group=group['id'], case_id=case['id'], traces={}, phases={})
            initials, generated = [], []
            for method in protocol['methods']:
                key = _trace_key(group['id'], case['id'], method)
                reference = state['completed'][key]
                trace = _read_trace(output, reference)
                row[method] = trace['metrics']
                row['traces'][method] = reference
                row['phases'][method] = trace['phases']
                initials.append(trace['decisions'][0]['input_sha256'])
                generated.append([h['generated'] for h in trace['history']])
            assert len(set(initials)) == 1, 'Initial public inputs differ between methods'
            assert all(values == generated[0] for values in generated), 'Per-time production differs'
            row['initial_visible_sha256'] = initials[0]
            for a, b in (('preview', 'simple'), ('preview', 'hold'), ('simple', 'hold')):
                if a in protocol['methods'] and b in protocol['methods']:
                    row[a + '_vs_' + b] = pair_comparison(row[a], row[b])
            rows.append(row)
    return rows


def _physical_initial(view):
    physical = deepcopy(view)
    physical.pop('restart_guard', None)
    physical.pop('restart_commitments', None)
    for key in ('reserved', 'available'):
        physical.get('notice_budget', {}).pop(key, None)
    return physical


def audit_matched_arms(rows, output, methods=METHODS):
    """Compare every saved production time point and initial physical input."""
    by_key = {(r['group'], r['case_id']): r for r in rows}
    checks = 0
    for row in rows:
        if not row['group'].startswith('control__'):
            continue
        partner = by_key.get(('protected__' + row['group'].removeprefix('control__'), row['case_id']))
        if partner is None:
            continue
        for method in methods:
            a = _read_trace(output, row['traces'][method])
            b = _read_trace(output, partner['traces'][method])
            assert _physical_initial(a['decisions'][0]['visible_input']) == _physical_initial(
                b['decisions'][0]['visible_input']), 'Initial physical states differ across arms'
            assert [h['generated'] for h in a['history']] == [h['generated'] for h in b['history']], \
                'Per-time production differs across arms'
            checks += 1
    return checks


def summarize_guard_effect(rows, methods=METHODS):
    by_key = {(r['group'], r['case_id']): r for r in rows}
    results = []
    for row in rows:
        if not row['group'].startswith('control__'):
            continue
        group = row['group'].removeprefix('control__')
        partner = by_key.get(('protected__' + group, row['case_id']))
        if partner is None:
            continue
        results.append(dict(group=group, case_id=row['case_id'],
            methods={method: {**pair_comparison(partner[method], row[method]),
                **{key: partner[method][key] - row[method][key] for key in RESTART_METRICS}}
                for method in methods}))
    return results


def run_matrix(frozen, output=OUT, *, resume=False, max_new_runs=None):
    output = Path(output)
    if (output / 'results.json').exists():
        raise FileExistsError('Completed matrix is immutable; use a fresh output directory')
    validate_frozen(frozen)
    if max_new_runs is not None and (isinstance(max_new_runs, bool) or
            not isinstance(max_new_runs, int) or max_new_runs < 1):
        raise ValueError('max_new_runs must be a positive integer')
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / 'run_state.json'
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if not resume or state['status'] != 'paused':
            raise RuntimeError('Only an explicitly paused run can resume with --resume')
        if (state['protocol_sha256'] != frozen['protocol_sha256'] or
                state['source_sha256'] != frozen['source_sha256']):
            raise RuntimeError('Resume hashes differ')
        for reference in state['completed'].values():
            _read_trace(output, reference)
        _adopt_orphan_traces(frozen, output, state)
    else:
        if resume or (output / 'traces').exists():
            raise RuntimeError('No matching intentional-pause checkpoint; preserve this directory')
        state = dict(schema=2, protocol_sha256=frozen['protocol_sha256'],
                     source_sha256=frozen['source_sha256'], completed={})
        (output / 'traces').mkdir()
    state['status'] = 'running'
    _replace_state(state_path, state)
    new_runs = 0
    try:
        for group in frozen['protocol']['groups']:
            for case in frozen['protocol']['cases']:
                config = frozen['protocol']['configurations'][group['id'] + '/' + case['id']]
                for method in frozen['protocol']['methods']:
                    key = _trace_key(group['id'], case['id'], method)
                    if key in state['completed']:
                        continue
                    trace = run_controller(config, method)
                    audit_trace(trace, config)
                    raw = _json_bytes(trace)
                    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
                    filename = key + '.json.gz'
                    final_path = output / 'traces' / filename
                    if final_path.exists():
                        raise FileExistsError('Existing trace cannot be overwritten: ' + filename)
                    # Interrupted staging files stay preserved and cannot be
                    # mistaken for complete gzip traces on a future resume.
                    pending_path = final_path.with_name(filename + '.pending.' + uuid4().hex)
                    with pending_path.open('xb') as stream:
                        stream.write(compressed)
                    pending_path.rename(final_path)
                    state['completed'][key] = dict(path='traces/' + filename,
                        sha256=hashlib.sha256(compressed).hexdigest(),
                        uncompressed_sha256=hashlib.sha256(raw).hexdigest())
                    _replace_state(state_path, state)
                    new_runs += 1
                    print(f'{len(state["completed"])} completed: {key}', flush=True)
                    if max_new_runs is not None and new_runs >= max_new_runs:
                        state.update(status='paused', pause_reason='intentional_max_new_runs')
                        _replace_state(state_path, state)
                        return state
    except KeyboardInterrupt:
        state.update(status='paused', pause_reason='intentional_keyboard_interrupt')
        _replace_state(state_path, state)
        raise
    except BaseException:
        state['status'] = 'failed'
        _replace_state(state_path, state)
        raise
    rows = _build_rows(frozen, output, state)
    result = dict(schema=3, completed_at_utc=datetime.now(timezone.utc).isoformat(),
                  protocol_sha256=frozen['protocol_sha256'], source_sha256=frozen['source_sha256'],
                  groups=summarize(rows, frozen['protocol']['methods']), rows=rows,
                  guard_effect=summarize_guard_effect(rows, frozen['protocol']['methods']))
    audit_control_compatibility(rows, require_complete=len(rows) == 256)
    audit_matched_arms(rows, output, frozen['protocol']['methods'])
    validate_frozen(frozen)
    _create_json(output / 'results.json', result)
    state['status'] = 'complete'
    _replace_state(state_path, state)
    return result


def audit_trace(trace, config, *, replay=False):
    """Independent history/event metrics plus optional recorded-action replay."""
    from .engine import new_simulation, public_state, advance, assess_action
    assert trace['configuration_sha256'] == canonical_hash(config)
    assert len(trace['history']) == 193 and len(trace['decisions']) == 48
    assert [h['tick'] for h in trace['history']] == list(range(193))
    assert trace['metrics'] == derive_metrics(trace), 'Saved scalar metrics do not derive from histories'
    assert trace['metrics']['max_abs_balance_error'] <= EPS
    assert trace['metrics']['prevented'] == 0
    assert abs(trace['terminal_balance']['stored'] - trace['metrics']['stored']) <= EPS
    assert abs(sum(x['volume'] for x in trace['terminal_transit']) - trace['metrics']['terminal_transit']) <= EPS
    assert trace['initial'] == inventory(trace['history'][0])
    expected_initial = public_state(new_simulation(deepcopy(config)))
    assert canonical_hash(expected_initial) == trace['decisions'][0]['input_sha256']
    for index, decision in enumerate(trace['decisions']):
        view, action = decision['visible_input'], decision['action']
        assert decision['tick'] == index * 4
        assert canonical_hash(view) == decision['input_sha256']
        assert canonical_hash(action) == decision['action_sha256']
        assert not {'scenario', 'disturbances', 'world_events', 'future_shift',
                    'physical_capacity', 'physical_downstream_capacity'} & set(view)
        assert view['production_end'] == (48 if decision['tick'] < 96 else 144)
        assert view['end_tick'] == (64 if decision['tick'] < 64 else 192)
        assert action.get('executable_in_field') is False
        assert not action.get('production')
        before_assessment = canonical_hash(view)
        assessment = assess_action(view, action['rates'], action.get('production'))
        assert canonical_hash(view) == before_assessment, 'Assessment mutated its public input'
        assert assessment == decision['admission']['assessment']
        assert decision['admission']['admitted'] == assessment['allowed']
        expected = action['rates'] if assessment['allowed'] else {}
        assert decision['admission']['applied_rates'] == expected
    events = trace['events']
    assert [e['minute'] for e in events] == sorted(e['minute'] for e in events)
    assert trace['metrics']['physical_starts'] == _sum(trace['history'][-1]['starts']) - _sum(trace['history'][0]['starts'])
    assert trace['metrics']['physical_stops'] == _sum(trace['history'][-1]['stops']) - _sum(trace['history'][0]['stops'])
    notice_limit = config['notice_budget']['limit']
    if notice_limit is not None:
        assert max(trace['metrics']['daily_notices']) <= notice_limit
    audit_restart_lifecycle(trace, config)
    for name, phase in trace['phases'].items():
        assert phase == phase_summary(trace['history'], phase['start_tick'], phase['end_tick']), name
    if replay:
        sim = new_simulation(deepcopy(config))
        for tick in range(END_TICK):
            reveal_boundary(sim)
            if tick % 4 == 0:
                decision = trace['decisions'][tick // 4]
                assert canonical_hash(public_state(sim)) == decision['input_sha256']
                assert admit_bundle(sim, decision['action']['rates']) == decision['admission']
            advance(sim)
            assert sim['history'][-1] == trace['history'][tick + 1]
        assert sim['events'] == trace['events'] and sim['orders'] == trace['orders']
        assert sim.get('world_events', []) == trace['world_events']
        assert sim.get('restart_commitments', []) == trace['restart_commitments']
        assert sim.get('restart_guard', config['restart_guard']) == trace['restart_guard']
    return dict(balanced_rows=len(trace['history']), decision_hashes=len(trace['decisions']),
                replayed_steps=END_TICK if replay else 0)


def audit_restart_lifecycle(trace, config):
    """Check charged notices, immutable deadlines and lifecycle evidence separately."""
    from .capabilities import assess_bundle
    # Recompute attribution from public inputs and the disabled cheap gate. Do
    # not trust the recorded marker or infer causality from a reason prefix.
    for decision in trace['decisions']:
        view = decision['visible_input']; admission = decision['admission']
        expected = False
        if view.get('restart_guard', {}).get('enabled') is True and not admission['admitted']:
            control = deepcopy(view); control['restart_guard'] = {'enabled': False}
            expected = assess_bundle(control, decision['action'].get('rates', {}))['allowed']
        assert isinstance(admission.get('guard_caused_refusal'), bool)
        assert admission['guard_caused_refusal'] == expected, 'Guard refusal attribution differs from disabled gate'
    commitments = trace['restart_commitments']
    assert trace['restart_guard'] == config['restart_guard']
    ledger = {c['id']: c for c in commitments}
    assert len(ledger) == len(commitments)
    orders = {o['id']: o for o in trace['orders']}
    assert len(orders) == len(trace['orders'])
    notices = [e for e in trace['events'] if e['kind'] == 'notice']
    assert len(notices) == len(orders), 'Every actual order must charge an ordinary notice'
    assert {e['order_id'] for e in notices} == set(orders)
    for event in notices:
        assert event['minute'] == orders[event['order_id']]['created_minutes']
    policy_events = [e for e in trace['events'] if e['kind'].startswith('restart_')]
    if not config['restart_guard']['enabled']:
        assert not ledger and not policy_events
    for event in policy_events:
        cid = event.get('commitment_id')
        assert cid in ledger, 'Restart event without a saved commitment'
        c = ledger[cid]
        if 'deadline_minutes' in event:
            assert event['deadline_minutes'] == c['deadline_minutes'], 'Promised deadline moved'
        assert event['firm'] == c['firm']
        assert event.get('target') == c['target']
    restore_owner = {}
    for cid, c in ledger.items():
        events = [e for e in policy_events if e['commitment_id'] == cid]
        reserved = [e for e in events if e['kind'] == 'restart_reserved']
        assert len(reserved) == 1 and reserved[0]['minute'] == c['created_minutes']
        assert c['target'] > 0
        closing = orders[c['closing_order_id']]
        assert closing['firm'] == c['firm'] and closing['kind'] == 'rate' and closing['target'] == 0
        assert closing['created_minutes'] == c['created_minutes']
        creation = next(d['visible_input'] for d in trace['decisions']
                        if d['minute'] == c['created_minutes'])
        prior = next(f['rate'] for f in creation['firms'] if f['id'] == c['firm'])
        assert c['target'] == prior, 'Captured target differs from previous nonzero arrangement'
        width = config['notice_budget']['window_minutes']
        expected_deadline = min(c['created_minutes'] + 240, creation['end_tick'] * 15,
            creation['recovery']['capacity_confirmation']['valid_until_minutes'],
            (c['created_minutes'] // width + 1) * width)
        assert c['deadline_minutes'] == expected_deadline
        assert c['projected_milestone_minutes'] <= c['deadline_minutes']
        restore_id = c.get('restore_order_id')
        dispatched = [e for e in events if e['kind'] == 'restart_dispatched']
        if restore_id is not None:
            assert restore_id not in restore_owner
            restore_owner[restore_id] = cid
            restore = orders[restore_id]
            assert restore['firm'] == c['firm'] and restore['kind'] == 'rate'
            assert restore['target'] == c['target']
            assert len(dispatched) == 1 and dispatched[0]['minute'] == restore['created_minutes']
            assert closing['status'] == 'effective'
            assert restore['created_minutes'] == closing['effective_at_minutes']
            if restore['status'] == 'effective':
                assert restore['effective_at_minutes'] > closing['effective_at_minutes']
            assert not c['reservation_active'], 'Charged restore still claims unused token'
        else:
            assert not dispatched
        recoveries = [e for e in events if e['kind'] in ('restart_restored', 'restart_ready_empty',
            'restart_late_restored', 'restart_late_ready_empty', 'restart_recovered_after_breach')]
        assert len(recoveries) <= 1
        if recoveries:
            event = recoveries[0]
            restore = orders[restore_id]
            assert restore['status'] == 'effective'
            assert restore['effective_at_minutes'] <= event['minute']
            assert event['minute'] == c['milestone_minutes']
            if event['kind'] in ('restart_restored', 'restart_ready_empty'):
                assert event['minute'] <= c['deadline_minutes']
                assert c['status'] == event['kind'].removeprefix('restart_')
                assert c['milestone_kind'] == c['status']
            elif event['kind'].startswith('restart_late_'):
                assert event['minute'] > c['deadline_minutes'] and c['status'] == 'missed'
                assert c['milestone_kind'] == event['kind'].removeprefix('restart_late_') + '_late'
                assert any(e['kind'] == 'restart_missed' and
                           c['deadline_minutes'] <= e['minute'] <= event['minute'] for e in events)
            else:
                assert event['minute'] <= c['deadline_minutes'] and c['status'] == 'failed'
                assert c['milestone_kind'] in ('restored_after_breach', 'ready_empty_after_breach')
                assert any(e['kind'] == 'restart_failed' and
                    e['reason'] == 'firm_overflow_breached_conditional_safeguard' for e in events)
                assert any(e['kind'] == 'overflow' and e.get('firm') == c['firm'] and
                    e['minute'] <= event['minute'] and e['volume'] > EPS for e in trace['events'])
            assert not c['route_reserved'] and not c['reservation_active']
            evidence = c['milestone_evidence']
            assert evidence == {key: event[key] for key in
                ('physical_rate', 'running', 'stock', 'window_eligible', 'start_eligible')}
            assert evidence['window_eligible'] and evidence['start_eligible']
            if c['milestone_kind'].startswith('restored'):
                assert evidence['running'] and abs(evidence['physical_rate'] - c['target']) <= EPS
            else:
                assert evidence['stock'] <= EPS
            assert any(e['kind'] == 'setpoint' and e.get('order_id') == restore_id
                       and e['value'] == c['target'] for e in trace['events'])
        cancellations = [e for e in events if e['kind'] == 'restart_cancelled']
        if cancellations:
            assert len(cancellations) == 1 and c['status'] == 'cancelled'
            assert not c['reservation_active'] and not c['route_reserved'] and not recoveries
            reason = cancellations[0]['reason']
            assert reason in c['reasons']
            if reason == 'closing_order_rejected_unused_reservation':
                assert restore_id is None and closing['status'] == 'rejected'
            else:
                assert reason.startswith('explicit_cancel:') and reason.removeprefix('explicit_cancel:').strip()
    # Replay reservation ownership independently from the engine budget counters.
    limit = config['notice_budget']['limit']
    width = config['notice_budget']['window_minutes']
    active, used = set(), Counter()
    for event in trace['events']:
        kind = event['kind']
        if kind == 'restart_reserved':
            active.add(event['commitment_id'])
        elif kind == 'restart_dispatched':
            assert event['commitment_id'] in active
            active.remove(event['commitment_id'])
        elif kind == 'restart_cancelled':
            # An explicit route-lock cancellation can follow a charged dispatch.
            active.discard(event['commitment_id'])
        elif kind == 'notice':
            used[event['minute'] // width] += 1
            if event['order_id'] in restore_owner:
                assert restore_owner[event['order_id']] in active
                continue  # Its bound token is consumed by the matching dispatch event.
        else:
            continue
        if limit is not None:
            assert used[event['minute'] // width] + len(active) <= limit, 'Reserved notice was stolen'
    assert active == {c['id'] for c in commitments if c['reservation_active']}
    # Public snapshots must reserve the captured peak even while the target is zero.
    for decision in trace['decisions']:
        view = decision['visible_input']
        firms = {f['id']: f for f in view['firms']}
        peaks = {fid: f['rate'] for fid, f in firms.items()}
        for order in view['orders']:
            if order['kind'] == 'rate' and order['status'] in ('notified', 'acknowledged'):
                peaks[order['firm']] = max(peaks[order['firm']], order['target'])
        for c in view.get('restart_commitments', []):
            if c['route_reserved']:
                peaks[c['firm']] = max(peaks[c['firm']], c['target'])
        if decision['admission']['admitted']:
            for fid, target in decision['admission']['applied_rates'].items():
                peaks[fid] = max(peaks[fid], target)
        for route in view['routes']:
            assert sum(v for fid, v in peaks.items() if firms[fid]['route'] == route['id']) <= route['capacity'] + EPS
        budget = view['notice_budget']
        if config['restart_guard']['enabled']:
            reserved = sum(c['reservation_active'] for c in view.get('restart_commitments', []))
            assert budget['reserved'] == reserved
            assert budget['available'] == (None if budget['remaining'] is None else max(0, budget['remaining'] - reserved))


def _audit_preserved():
    path = HERE / 'artifacts/setup/preserved_before.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    root = HERE.parents[1]
    for name, expected in manifest.items():
        assert sha(root / name) == expected, 'Preserved historical file changed: ' + name
    return len(manifest)


def audit(output=OUT, *, replay=True):
    output = Path(output)
    frozen = json.loads((output / 'protocol.json').read_text(encoding='utf-8'))
    validate_frozen(frozen)
    result = json.loads((output / 'results.json').read_text(encoding='utf-8'))
    assert result['protocol_sha256'] == frozen['protocol_sha256']
    assert result['source_sha256'] == frozen['source_sha256']
    protocol = frozen['protocol']
    assert result['groups'] == summarize(result['rows'], protocol['methods'])
    assert result['guard_effect'] == summarize_guard_effect(result['rows'], protocol['methods'])
    assert len(result['rows']) == len(protocol['groups']) * len(protocol['cases'])
    expected_pairs = {(g['id'], c['id']) for g in protocol['groups'] for c in protocol['cases']}
    assert {(r['group'], r['case_id']) for r in result['rows']} == expected_pairs
    counts = dict(traces=0, balanced_rows=0, decision_hashes=0, replayed_steps=0,
                  same_initial_public_inputs=0, per_time_production_equalities=0)
    for row in result['rows']:
        config = protocol['configurations'][row['group'] + '/' + row['case_id']]
        initials, generated = [], []
        for method in protocol['methods']:
            trace = _read_trace(output, row['traces'][method])
            assert trace['method'] == method
            assert trace['metrics'] == row[method]
            assert trace['phases'] == row['phases'][method]
            checks = audit_trace(trace, config, replay=replay)
            counts['traces'] += 1
            for key, value in checks.items():
                counts[key] += value
            initials.append(trace['decisions'][0]['input_sha256'])
            generated.append([h['generated'] for h in trace['history']])
        assert len(set(initials)) == 1 and initials[0] == row['initial_visible_sha256']
        assert all(values == generated[0] for values in generated)
        counts['same_initial_public_inputs'] += 1
        counts['per_time_production_equalities'] += 193
        for a, b in (('preview', 'simple'), ('preview', 'hold'), ('simple', 'hold')):
            if a in protocol['methods'] and b in protocol['methods']:
                assert row[a + '_vs_' + b] == pair_comparison(row[a], row[b])
    counts.update(protected_historical_files=_audit_preserved(),
                  control_legacy_metric_records=audit_control_compatibility(result['rows'], require_complete=len(result['rows']) == 256),
                  matched_arm_trajectories=audit_matched_arms(result['rows'], output, protocol['methods']),
                  no_production_orders=True, no_field_executable_claims=True,
                  process_quality_unknown=True, source_and_protocol_unchanged=True)
    audit_path = output / 'audit.json'
    if audit_path.exists():
        previous = json.loads(audit_path.read_text(encoding='utf-8'))
        if previous != counts:
            raise RuntimeError('Existing audit differs; preserve it and use a separate diagnostic output')
    else:
        _create_json(audit_path, counts)
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--max-new-runs', type=int)
    parser.add_argument('--output', type=Path, default=OUT)
    args = parser.parse_args()
    if not (args.freeze or args.run or args.audit):
        parser.error('Choose --freeze, --run or --audit')
    if args.freeze or args.run:
        frozen = freeze_protocol(args.output / 'protocol.json')
        print('Frozen protocol:', frozen['protocol_sha256'], flush=True)
        if args.run:
            result = run_matrix(frozen, args.output, resume=args.resume, max_new_runs=args.max_new_runs)
            print(json.dumps(result.get('groups', dict(status=result.get('status'))),
                             ensure_ascii=False, indent=2))
    if args.audit:
        print(json.dumps(audit(args.output), ensure_ascii=False, indent=2))

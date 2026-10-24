"""Original 32 synthetic cases with one explicit assumed capability profile.

Engineering source categories motivate the fields, never these numerical values.
The v1 protocol is read as immutable data, rather than rebuilding its cases.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEGACY = HERE.parent / 'wastewater_workbench'
LEGACY_PROTOCOL = LEGACY / 'artifacts/review_v2/comparison_protocol.json'
END_TICK = 192
STEP_MINUTES = 15
METHODS = ['hold', 'simple', 'preview']
ORIGINAL_GROUPS = [
    dict(id='mixed_confirmed', notice_limit=None, confirmation_delay_minutes=0),
    dict(id='mixed_confirmation_gap', notice_limit=None, confirmation_delay_minutes=120),
    dict(id='mixed_notice8', notice_limit=8, confirmation_delay_minutes=0),
    dict(id='mixed_notice16', notice_limit=16, confirmation_delay_minutes=0),
]
GROUPS = [dict(group, id=arm + '__' + group['id'], original_group=group['id'],
               restart_guard_enabled=enabled)
          for arm, enabled in (('control', False), ('protected', True))
          for group in ORIGINAL_GROUPS]


def canonical_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def capabilities():
    """Fresh copies of the sole predeclared numerical profile; all assumed."""
    common = dict(min_on_minutes=10, min_off_minutes=10,
                  min_setpoint_hold_minutes=60, max_starts_per_hour=2,
                  basis='assumed', source='v2 frozen synthetic profile; not field calibrated')
    return {
        'A': dict(**common, mode='on_off', duty_rate=20, flow_levels=[0, 20],
                  windows_minutes=[], ramp_up_m3_h_per_minute=None,
                  ramp_down_m3_h_per_minute=None),
        'B': dict(**common, mode='variable', duty_rate=None, flow_levels=[0, 10, 20, 30, 40],
                  windows_minutes=[], ramp_up_m3_h_per_minute=2,
                  ramp_down_m3_h_per_minute=2),
        'C': dict(**common, mode='window', duty_rate=15, flow_levels=[0, 15],
                  windows_minutes=[[0, 360], [420, 1800], [1860, 2880]],
                  ramp_up_m3_h_per_minute=None, ramp_down_m3_h_per_minute=None),
        'D': dict(**common, mode='variable', duty_rate=None, flow_levels=[0, 10, 20, 30],
                  windows_minutes=[], ramp_up_m3_h_per_minute=2,
                  ramp_down_m3_h_per_minute=2),
    }


def original_cases():
    record = json.loads(LEGACY_PROTOCOL.read_text(encoding='utf-8'))
    if canonical_hash(record['protocol']) != record['protocol_sha256']:
        raise RuntimeError('Preserved original protocol hash mismatch')
    cases = deepcopy(record['protocol']['cases'])
    if len(cases) != 32 or len({case['id'] for case in cases}) != 32:
        raise RuntimeError('Expected all original 32 distinct cases')
    return cases, record['protocol_sha256']


def configuration(case, group):
    cfg = deepcopy(case['configuration'])
    cfg['capabilities'] = capabilities()
    cfg['end_tick'], cfg['production_end'] = 64, 48
    cfg['step_minutes'], cfg['substep_minutes'] = STEP_MINUTES, 5
    cfg['notice_budget'] = dict(limit=group['notice_limit'], window_minutes=1440)
    cfg['restart_guard'] = dict(enabled=group['restart_guard_enabled'])
    original = deepcopy(cfg['disturbances'])
    changes = sorted((event for event in original if event['kind'] == 'capacity'),
                     key=lambda event: event['tick'])
    first_change = changes[0]['tick'] * STEP_MINUTES if changes else END_TICK * STEP_MINUTES
    cfg['recovery'] = dict(
        maintenance_complete=False, temporary_supply_available=False,
        process_ready=None, effluent_verified=None,
        capacity_confirmation=dict(value=cfg['downstream_capacity'], confirmed_at_minutes=0,
                                   valid_until_minutes=first_change, revision=0, basis='assumed',
                                   source='synthetic initial receiving confirmation'))
    events = original
    restoration_ticks = []
    for index, event in enumerate(changes):
        minute = event['tick'] * STEP_MINUTES
        delay = group['confirmation_delay_minutes'] if event['value'] == 65 else 0
        expiry = (changes[index + 1]['tick'] * STEP_MINUTES
                  if index + 1 < len(changes) else END_TICK * STEP_MINUTES)
        events.append(dict(tick=event['tick'] + delay // STEP_MINUTES,
                           kind='capacity_confirmation', value=event['value'],
                           confirmed_at_minutes=minute + delay, valid_until_minutes=expiry,
                           revision=index + 1, basis='assumed',
                           source='synthetic independent receiving confirmation'))
        if event['value'] == 65:
            restoration_ticks.append(event['tick'])
            events.append(dict(tick=event['tick'], kind='maintenance',
                               temporary_supply_available=True))
    final_recovery_tick = max(restoration_ticks, default=0)
    events.append(dict(tick=final_recovery_tick + 16, kind='maintenance',
                       maintenance_complete=True))
    # Stable sorting keeps capacity world change before same-time confirmation.
    cfg['disturbances'] = sorted(events, key=lambda event: event['tick'])
    cfg['assumptions'] = [
        'Synthetic numerical profiles; no field calibration or executable-in-field claim.',
        '5-minute physical substeps within 15-minute observations; no transient certification.',
        'Receiving volume is not compliant treatment; process and effluent status stay unknown.',
        'First production [0,48) ticks; second shift [96,144) revealed only at tick96.',
        'Observation horizon extends from64 to192 at tick64, without state reset.',
        'One mixed device profile, same production and protections in all three methods.',
    ]
    return cfg


def build_protocol():
    cases, original_hash = original_cases()
    groups = deepcopy(GROUPS)
    configs = {group['id'] + '/' + case['id']: configuration(case, group)
               for group in groups for case in cases}
    return dict(
        schema=3, label='Matched restart-reservation synthetic 48h water-volume comparison',
        methods=deepcopy(METHODS), cases=cases, groups=groups, configurations=configs,
        end_tick=END_TICK, step_minutes=STEP_MINUTES, physical_step_minutes=5,
        decision_every_ticks=4, forecast_horizon_ticks=16,
        calendar=dict(original_production=[0, 48], next_production=[96, 144],
                      next_shift_revealed_at=96, horizon_extended_at=64, common_stop=192),
        objective_order=['forecast_uncontained', 'forecast_terminal_stored',
                         'new_notices', 'absolute_rate_change'],
        production_policy='unchanged all methods; no production orders',
        information_policy='public observations only; private world schedule withheld',
        decision_timing='decision precedes same-tick world events; reports observable at cell end',
        capability_policy='one explicit assumed mixed numerical profile before matrix',
        budget_policy='common capability-aware alternatives and atomic whole-bundle gate',
        restart_policy='control disabled versus protected shared ordinary close-and-restore reservation',
        primary_comparison='preview minus simple total uncontained m3; lower is better',
        primary_tolerance=1e-6,
        stopping_policy='all runs exactly48h, retained terminal water and all losses',
        retention_policy='all cases, ties, losses, unknown decisions, compressed full traces',
        metric_definitions=dict(
            stored_area_m3_h='trapezoidal area from15-minute saved total-inventory observations',
            unknown_decisions='receiving unknown, capability unknown, or needs-confirmation union; includes hold',
            needs_confirmation_decisions='planner declines a numeric action/forecast for missing required input',
            notices='created orders, separate from acknowledged setpoints and physical starts/stops',
            runtime_minutes='sum actual device-on minutes including partial dry-stop cells',
            late_uncontained='additional uncontained volume after original tick64 boundary'),
        revision_policy='source/protocol frozen before runs; no post-matrix tuning',
        legacy_protocol_sha256=original_hash,
        legacy_protocol_file_sha256=hashlib.sha256(LEGACY_PROTOCOL.read_bytes()).hexdigest(),
        limitations=[
            'All numbers are synthetic; reference material supports categories only.',
            'Unknown process readiness and effluent quality do not imply safe or compliant treatment.',
            'One device-profile cohort; no field effectiveness or global optimality claim.',
            'No seconds-scale transients, external haulage, monetary or operator-time benefit model.',
            'Old v1 cohorts remain separate and are never rewritten or blended into v2.',
        ])

"""Finite public-information controllers on the same executable action space.

Simple uses an urgency allocation and one local correction. Preview retains
every simple candidate and adds a frozen set of alternative reservations.
Neither is a global optimizer or a field-operation certification.
"""
from copy import deepcopy
from itertools import combinations

from ..wastewater_workbench_v3.capabilities import action_options, assess_bundle, profile_status, finite
from .engine import conditional_run, PENDING, assess_action
from ..wastewater_workbench_v3 import restart_guard
from ..wastewater_workbench_v3.recovery import receiving_status

HORIZON = 16
EPS = 1e-8

_TOP = ('schema', 'name', 'tick', 'minute', 'end_tick', 'production_end',
        'step_minutes', 'substep_minutes', 'start_hour', 'mode', 'auto_ack',
        'downstream_capacity', 'assumptions')
_FIRM = ('id', 'name', 'route', 'stock', 'capacity', 'production_rate', 'rate',
         'max_rate', 'ack_delay', 'switch_delay', 'min_hold', 'batch_ticks',
         'production_enabled', 'production_multiplier', 'last_switch',
         'last_release', 'uncontained')
_ACTUATOR = ('running', 'physical_rate', 'last_start_minutes', 'last_stop_minutes',
             'last_setpoint_minutes', 'start_minutes', 'runtime_minutes', 'starts', 'stops')
_ROUTE = ('id', 'name', 'capacity', 'buffer', 'stock', 'delay', 'last_received',
          'uncontained', 'in_transit')
_CAPABILITY = ('mode', 'duty_rate', 'flow_levels', 'windows_minutes',
               'min_on_minutes', 'min_off_minutes', 'min_setpoint_hold_minutes',
               'max_starts_per_hour', 'ramp_up_m3_h_per_minute',
               'ramp_down_m3_h_per_minute', 'basis', 'source')
_ORDER = ('id', 'firm', 'kind', 'target', 'status', 'created', 'ack_due', 'earliest',
          'ack_at', 'effective_at', 'created_minutes', 'ack_due_minutes',
          'earliest_minutes', 'ack_at_minutes', 'effective_at_minutes',
          'terminal_reason')
_CONFIRMATION = ('value', 'confirmed_at_minutes', 'valid_until_minutes',
                 'revision', 'basis', 'source')


def _select(record, names):
    return {key: deepcopy(record[key]) for key in names if key in record}


def _public_copy(snapshot):
    """Nested allowlists prevent opaque scenario/future fields becoming inputs."""
    view = _select(snapshot, _TOP)
    view['firms'] = []
    for firm in snapshot['firms']:
        public = _select(firm, _FIRM)
        public['actuator'] = _select(firm.get('actuator', {}), _ACTUATOR)
        view['firms'].append(public)
    view['routes'] = [_select(route, _ROUTE) for route in snapshot['routes']]
    view['transit'] = [_select(item, ('route', 'volume', 'due', 'due_minutes', 'firm', 'disposition_order_id'))
                       for item in snapshot.get('transit', [])]
    view['orders'] = [_select(order, _ORDER) for order in snapshot.get('orders', [])]
    view['totals'] = _select(snapshot['totals'], ('generated', 'received', 'uncontained', 'prevented'))
    view['balance'] = _select(snapshot['balance'],
        ('initial', 'input', 'stored', 'received', 'uncontained', 'error'))
    view['capabilities'] = {fid: _select(profile, _CAPABILITY)
                            for fid, profile in snapshot.get('capabilities', {}).items()}
    recovery = snapshot.get('recovery', {})
    view['recovery'] = _select(recovery, ('maintenance_complete',
        'temporary_supply_available', 'process_ready', 'effluent_verified'))
    confirmation = recovery.get('capacity_confirmation')
    view['recovery']['capacity_confirmation'] = (
        _select(confirmation, _CONFIRMATION) if isinstance(confirmation, dict) else None)
    view['notice_budget'] = _select(snapshot.get('notice_budget') or {},
        ('limit', 'window_minutes', 'used', 'remaining', 'reserved', 'available'))
    view['restart_guard'] = {'enabled': restart_guard.enabled(snapshot)}
    view['restart_commitments'] = restart_guard.public_records(snapshot.get('restart_commitments', []))
    # The common v5 executor includes committed transfers and return debt in forecasts.
    # Engine.from_public applies the disposition schema allowlist a second time.
    view['disposition'] = deepcopy(snapshot.get('disposition', {}))
    view['history'], view['events'] = [], []
    return view


def locked_rates(view):
    locked = {order['firm']: order['target'] for order in view['orders']
              if order['kind'] == 'rate' and order['status'] in PENDING}
    if restart_guard.enabled(view):
        locked.update({c['firm']: c['target'] for c in restart_guard.outstanding(view)})
    return locked


def _observation_unknowns(view):
    """Do not invent missing observed model state merely to rank actions."""
    reasons = []
    for firm in view['firms']:
        fid = firm['id']
        for key in ('stock', 'capacity', 'production_rate', 'rate', 'max_rate',
                    'ack_delay', 'switch_delay', 'batch_ticks', 'production_multiplier'):
            if not finite(firm.get(key)):
                reasons.append('observation:' + fid + ':' + key)
        actuator = firm['actuator']
        for key in ('physical_rate', 'last_start_minutes', 'last_stop_minutes',
                    'last_setpoint_minutes', 'runtime_minutes', 'starts', 'stops'):
            if not finite(actuator.get(key)):
                reasons.append('actuator:' + fid + ':' + key)
        if not isinstance(actuator.get('running'), bool):
            reasons.append('actuator:' + fid + ':running')
        starts = actuator.get('start_minutes')
        if not isinstance(starts, list) or any(not finite(item) for item in starts):
            reasons.append('actuator:' + fid + ':start_minutes')
        if not isinstance(firm.get('production_enabled'), bool):
            reasons.append('observation:' + fid + ':production_enabled')
    for route in view['routes']:
        for key in ('capacity', 'buffer', 'stock', 'delay'):
            if not finite(route.get(key)):
                reasons.append('observation:' + route['id'] + ':' + key)
    return reasons


def urgency_seed(view, options, horizon, target_fraction=.35, buffer_fraction=.8):
    """Reserve route/storage budget, assigning actual feasible discrete levels."""
    dt = view['step_minutes'] / 60
    hours = horizon * dt
    production_hours = max(0, min(horizon, view['production_end'] - view['tick'])) * dt
    locked = locked_rates(view)
    firms = {firm['id']: firm for firm in view['firms']}
    demand, projected, editable = {}, {}, {}
    for fid, firm in firms.items():
        profile = view['capabilities'][fid]
        last = firm['actuator']['last_setpoint_minutes']
        earliest = max(view['minute'] + (firm['ack_delay'] + firm['switch_delay']) * view['step_minutes'],
                       last + profile['min_setpoint_hold_minutes'])
        wait = min(hours, max(0, earliest - view['minute']) / 60)
        prod = firm['production_rate'] * firm['production_multiplier'] if firm['production_enabled'] else 0
        stock = max(0, firm['stock'] + prod * min(production_hours, wait) - firm['rate'] * wait)
        editable[fid] = max(dt, hours - wait)
        projected[fid] = stock + prod * max(0, production_hours - wait)
        demand[fid] = min(max(options[fid]),
            max(0, (projected[fid] - target_fraction * firm['capacity']) / editable[fid]))
    route_need = {route['id']: sum(demand[f['id']] for f in firms.values() if f['route'] == route['id'])
                  for route in view['routes']}
    total_need = sum(route_need.values())
    rates = {}
    for route in view['routes']:
        rid = route['id']
        members = [f for f in firms.values() if f['route'] == rid]
        share = view['downstream_capacity'] * route_need[rid] / total_need if total_need else 0
        transit = sum(item['volume'] for item in view['transit'] if item['route'] == rid)
        budget = max(0, min(route['capacity'], share +
            (buffer_fraction * route['buffer'] - route['stock'] - transit) / hours))
        remaining = max(0, budget - sum(locked[f['id']] for f in members if f['id'] in locked))
        available = [f for f in members if f['id'] not in locked]
        for firm in available:
            rates[firm['id']] = min(options[firm['id']])
            remaining -= rates[firm['id']]
        while True:
            eligible = []
            for firm in available:
                fid = firm['id']
                higher = [level for level in options[fid] if level > rates[fid] + EPS]
                if higher and rates[fid] < demand[fid] - EPS and min(higher) - rates[fid] <= remaining + EPS:
                    eligible.append((firm, min(higher)))
            if not eligible:
                break
            firm, next_level = max(eligible, key=lambda item: (
                (projected[item[0]['id']] - rates[item[0]['id']] * editable[item[0]['id']]) / item[0]['capacity'],
                item[0]['id']))
            remaining -= next_level - rates[firm['id']]
            rates[firm['id']] = next_level
    return rates


def objective(view, forecast, rates):
    """Same finite-horizon lexicographic loss for every candidate and method."""
    current = {firm['id']: firm['rate'] for firm in view['firms']}
    changes = [abs(value - current[fid]) for fid, value in rates.items()
               if abs(value - current[fid]) > EPS]
    return (round(forecast['totals']['uncontained'] - view['totals']['uncontained'], 7),
            round(forecast['balance']['stored'], 7),
            forecast.get('notices', len(changes)),
            round(sum(changes), 7))


def _neighbors(rates, options):
    # Every single-coordinate feasible level remains available under tight quota.
    for fid, value in rates.items():
        for target in options[fid]:
            if abs(target - value) > EPS:
                candidate = dict(rates)
                candidate[fid] = target
                yield candidate
    # One adjacent equal-volume exchange; never iterate toward a global optimum.
    for left, right in combinations(rates, 2):
        for source, target in ((left, right), (right, left)):
            lower = [level for level in options[source] if level < rates[source] - EPS]
            higher = [level for level in options[target] if level > rates[target] + EPS]
            if lower and higher and abs((rates[source] - max(lower)) - (min(higher) - rates[target])) <= EPS:
                candidate = dict(rates)
                candidate[source], candidate[target] = max(lower), min(higher)
                yield candidate


def recommend(public_snapshot, method='simple'):
    if method not in ('simple', 'preview'):
        raise ValueError('method must be simple or preview')
    if public_snapshot['tick'] >= public_snapshot['end_tick']:
        raise ValueError('simulation is complete')
    view = _public_copy(public_snapshot)
    horizon = min(HORIZON, view['end_tick'] - view['tick'])
    reasons = []
    guard_refusals = 0

    def result(rates, status, predicted=None, count=0):
        return dict(rates=rates, production={}, method=method,
            status=status, executable_in_field=False, horizon=horizon,
            evaluated_candidates=count, predicted_objective=predicted,
            restart_guard_refusals=guard_refusals,
            capability_reasons=sorted(set(reasons)), includes_simple=True,
            production_policy='unchanged_all_methods',
            reason=('Public confirmation or capability information is insufficient; existing effective actions continue.'
                    if status == 'needs_confirmation' else
                    'Finite urgency and local discrete alternatives, gated by the common actuator and notice constraints; '
                    'prediction assumes current public conditions and is synthetic only.'))

    profile = profile_status(view)
    reasons.extend(_observation_unknowns(view))
    unknown = profile.get('unknown_fields', {})
    if isinstance(unknown, dict):
        reasons.extend('capability:' + fid + ':' + field
                       for fid, fields in unknown.items() for field in fields)
    else:
        reasons.extend(str(item) for item in unknown)
    options = {firm['id']: sorted(set(action_options(view, firm['id']))) for firm in view['firms']}
    reasons.extend('unknown_options:' + fid for fid, levels in options.items() if not levels)
    receiving = receiving_status(view['recovery'], view['minute'])
    if not receiving['known']:
        reasons.append('receiving:' + receiving['reason'])
    if reasons:
        return result({}, 'needs_confirmation')
    # Confirmation is the sole capacity source; disregard any inconsistent scalar.
    view['downstream_capacity'] = receiving['capacity_m3_h']
    baseline = conditional_run(view, horizon=horizon)
    if baseline.get('status') != 'conditional_simulation':
        reasons.append('forecast:' + str(baseline.get('reason', 'unknown')))
        return result({}, 'needs_confirmation')
    locked = locked_rates(view)
    current = {firm['id']: firm['rate'] for firm in view['firms'] if firm['id'] not in locked}
    seed = urgency_seed(view, options, horizon)
    evaluated = {}

    def evaluate(rates):
        nonlocal guard_refusals
        key = tuple(sorted(rates.items()))
        if key in evaluated:
            return evaluated[key]
        assessment = assess_action(view, rates)
        if not assessment['allowed']:
            if restart_guard.enabled(view):
                legacy = deepcopy(view);legacy['restart_guard'] = {'enabled':False}
                if assess_bundle(legacy, rates)['allowed']:guard_refusals += 1
            reasons.extend(str(item) for item in assessment.get('reasons', []))
            evaluated[key] = None
            return None
        forecast = baseline if rates == current else conditional_run(view, rates, {}, horizon)
        if forecast.get('status') != 'conditional_simulation':
            reasons.append('forecast:' + str(forecast.get('reason', 'unknown')))
            evaluated[key] = None
            return None
        value = (objective(view, forecast, rates), dict(rates))
        evaluated[key] = value
        return value

    simple_candidates = [current, seed, *_neighbors(current, options), *_neighbors(seed, options)]
    feasible = [candidate for rates in simple_candidates if (candidate := evaluate(rates)) is not None]
    if not feasible:
        return result({}, 'needs_confirmation')
    simple_keys = set(evaluated)
    chosen = min(feasible, key=lambda candidate: candidate[0])
    if method == 'preview':
        extra = [{fid: max(options[fid]) for fid in current}]
        for target in (.0, .2, .5, .7):
            for reserve in (.5, 1.0):
                extra.append(urgency_seed(view, options, horizon, target, reserve))
        extra.extend(_neighbors(chosen[1], options))
        for rates in extra:
            candidate = evaluate(rates)
            if candidate is not None and candidate[0] < chosen[0]:
                chosen = candidate
        assert simple_keys <= set(evaluated)
    count = sum(candidate is not None for candidate in evaluated.values())
    return result(chosen[1], 'conditional_simulation', list(chosen[0]), count)

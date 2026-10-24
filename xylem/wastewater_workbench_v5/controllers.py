"""Common strong rate selector plus a bounded finite disposition comparison.

Simple ranks prospective overflow/occupancy and may store OR return each hour.
Coordinated keeps the same direct rates, considers the simple choice, defer, and
one feasible transfer per source, then evaluates shared 4h execution. Neither
uses the private event schedule, stops production, or invents extra resources.
"""
from copy import deepcopy
from . import engine, disposition, planning

EPS = 1e-7


def _apply(view, rates, transfers, horizon=0):
    sim = engine.from_public(view)
    if rates:
        engine.publish(sim, rates)
    for action in transfers:
        disposition.request(sim, **action)
    if horizon:
        engine.advance(sim, horizon)
    return sim


def _choices(view, forecast):
    """At most one pump-hour of water per proposed transfer; batch never magic."""
    d = view.get('disposition', {})
    if not d.get('enabled'):
        return [], []
    now = view['minute']
    active = d.get('orders', [])
    stored = {f['id']:f for f in view['firms']}
    finals = {f['id']:f for f in forecast['firms']}
    # Do not stack new reservations for a source while its older transfer is open.
    locked = {(o['direction'],o['firm']) for o in active if o['status'] in disposition.ACTIVE}
    stores, returns = [], []
    for fid, firm in stored.items():
        if ('store',fid) not in locked:
            net = max(0.,firm['production_rate']*firm['production_multiplier']-firm['rate'])
            horizon_loss = max(0.,finals[fid]['uncontained']-firm['uncontained'])
            need = horizon_loss + max(0.,finals[fid]['stock'] - .8*firm['capacity'])
            volume = min(d['pump_rate_m3_h'], firm['stock'], max(0.,need))
            gate = disposition.assess(view,'store',fid,max(volume,EPS))
            volume = min(volume,gate['available'])
            if volume > EPS and disposition.assess(view,'store',fid,volume)['allowed']:
                headroom_hours = (firm['capacity']-firm['stock']) / net if net>EPS else float('inf')
                stores.append((horizon_loss>EPS,-headroom_hours,need/firm['capacity'],fid,
                               dict(direction='store',firm=fid,volume=round(volume,8))))
        if ('return',fid) not in locked:
            volume = min(d['pump_rate_m3_h'], d.get('inventory',{}).get(fid,0.))
            gate = disposition.assess(view,'return',fid,max(volume,EPS))
            volume = min(volume,gate['available'])
            if volume > EPS and disposition.assess(view,'return',fid,volume)['allowed']:
                # Oldest stored-water commitment first, stable firm-id tie break.
                timestamps = [o['created_minutes'] for o in active
                              if o['firm']==fid and o['direction']=='store' and o['arrived_m3']>EPS]
                age = now-min(timestamps,default=now)
                returns.append((age,volume,fid,dict(direction='return',firm=fid,volume=round(volume,8))))
    stores.sort(reverse=True,key=lambda item:item[:-1])
    returns.sort(reverse=True,key=lambda item:item[:-1])
    return stores, returns


def _objective(view, sim):
    final = engine.public_state(sim)
    d = final.get('disposition',{})
    before_d = view.get('disposition',{})
    notices = len(final['orders'])-len(view['orders']) + len(d.get('orders',[]))-len(before_d.get('orders',[]))
    return_debt = d.get('in_storage_m3',0.) + d.get('in_transfer_m3',0.) + sum(
        p['volume'] for p in final['transit'] if p.get('disposition_order_id'))
    return (round(final['totals']['uncontained']-view['totals']['uncontained'],7),
            round(final['balance']['stored'],7),round(return_debt,7),notices)


def recommend(view, method='simple'):
    if method not in ('simple','coordinated'):
        raise ValueError('method must be simple or coordinated')
    # Shared allowlist and selector are the information and actuator contract.
    view = planning._public_copy(view)
    base = planning.recommend(view,'simple')
    result = dict(base,method=method,transfers=[],transfer_candidates=0,
                  disposition_reason='资源未启用或当前无需新增处置')
    if not view.get('disposition',{}).get('enabled'):
        return result
    if base['status'] != 'conditional_simulation':
        result['disposition_reason']='预测所需确认不足；保留既有执行并报告未知'
        return result
    horizon=min(16,view['end_tick']-view['tick'])
    common = _apply(view,base['rates'],[])
    post_rates = engine.public_state(common)
    hold = deepcopy(common)
    engine.advance(hold,horizon)
    forecast = engine.public_state(hold)
    stores, returns = _choices(post_rates,forecast)
    candidates = [[entry[-1]] for entry in stores+returns]
    scored = []
    # Both store and return use one cheap candidate-versus-wait comparison.
    # Adding a resource must not force a notice which predicts no improvement.
    hold_value = _objective(view,hold)
    simple = [stores[0][-1]] if stores else []
    if not simple and returns:
        simple=[returns[0][-1]]
    if simple:
        tested=_apply(view,base['rates'],simple,horizon)
        if _objective(view,tested) >= hold_value:
            simple=[]
    if method=='simple':
        result['transfers']=simple
        result['transfer_candidates']=len(candidates)
        result['disposition_reason']='按已知来水预测紧迫度暂存；无紧迫暂存时按最早责任回送'
        return result
    for actions in [simple,[],*candidates]:
        try:
            sim=_apply(view,base['rates'],actions,horizon)
            scored.append((_objective(view,sim),actions))
        except ValueError:
            # Both policies execute through the same final gate; no hidden force.
            continue
    result['transfers']=min(scored,key=lambda row:row[0])[1] if scored else []
    result['transfer_candidates']=len(scored)
    result['disposition_reason']='同一直接排量下有限比较暂存、回送与等待；先避免未容纳，再减少库存及回送责任'
    return result

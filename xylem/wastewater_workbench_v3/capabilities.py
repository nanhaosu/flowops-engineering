"""Explicit actuator capability assumptions; unknown values never become unlimited."""
import math

EPS=1e-8
PENDING=('notified','acknowledged')
TIMING=('min_on_minutes','min_off_minutes','min_setpoint_hold_minutes')


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def validate_profile(p):
    if not isinstance(p,dict):raise ValueError('capability must be a dict')
    mode=p.get('mode','unknown')
    if mode not in ('on_off','window','variable','unknown'):raise ValueError('unsupported mode')
    for key in TIMING:
        v=p.get(key)
        if v is not None and (not finite(v) or v<0 or v%5):raise ValueError(key+' must use the nonnegative 5-minute grid')
    for key in ('duty_rate','ramp_up_m3_h_per_minute','ramp_down_m3_h_per_minute'):
        v=p.get(key)
        if v is not None and (not finite(v) or v<=0):raise ValueError(key+' must be positive finite')
    v=p.get('max_starts_per_hour')
    if v is not None and (not finite(v) or v<1 or int(v)!=v):raise ValueError('invalid max_starts_per_hour')
    levels=p.get('flow_levels')
    if levels is not None and (not isinstance(levels,list) or any(not finite(v) or v<0 for v in levels) or (mode=='variable' and (not levels or 0 not in levels))):raise ValueError('flow_levels require finite levels including zero')
    windows=p.get('windows_minutes')
    if windows is not None:
        if not isinstance(windows,list):raise ValueError('invalid windows')
        for w in windows:
            if not isinstance(w,(tuple,list)) or len(w)!=2 or any(not finite(t) or t<0 or t%5 for t in w) or w[1]<=w[0]:raise ValueError('window must be [start,end) on 5-minute grid')
    if p.get('basis') not in (None,'unknown','assumed','verified'):raise ValueError('invalid capability basis')


def unknown_fields(p):
    try:validate_profile(p)
    except (ValueError,TypeError):return ['invalid_profile']
    mode=p.get('mode','unknown')
    required=list(TIMING)+['max_starts_per_hour','basis','source']
    if mode=='unknown':return ['mode']
    if mode in ('on_off','window'):required+=['duty_rate']
    if mode=='window':required+=['windows_minutes']
    if mode=='variable':required+=['flow_levels','ramp_up_m3_h_per_minute','ramp_down_m3_h_per_minute']
    return [k for k in required if p.get(k) is None or p.get(k)=='unknown' or (k=='source' and not p.get(k))]


def profile_status(view):
    firms=view.get('firms',[])
    if isinstance(firms,dict):firms=list(firms.values())
    unknown={f['id']:unknown_fields(view.get('capabilities',{}).get(f['id'],{})) for f in firms}
    unknown={k:v for k,v in unknown.items() if v}
    assumed=any(view.get('capabilities',{}).get(f['id'],{}).get('basis')!='verified' for f in firms)
    return dict(simulation_only=assumed or bool(unknown),unknown_fields=unknown)


def action_options(view,firm_id):
    p=view.get('capabilities',{}).get(firm_id,{})
    if unknown_fields(p):return []
    if p['mode'] in ('on_off','window'):return [0.,float(p['duty_rate'])]
    return sorted(set(float(x) for x in p['flow_levels']))


def assess_bundle(view,rates):
    reasons=[];required=0
    firms=view.get('firms',[])
    firms=firms if isinstance(firms,dict) else {f['id']:f for f in firms}
    targets={i:f['rate'] for i,f in firms.items()}
    guard=view.get('restart_guard',{}).get('enabled') is True
    commitments=view.get('restart_commitments',[]) if guard else []
    if not isinstance(rates,dict):return dict(allowed=False,reasons=['rates_must_be_object'],required_notices=0)
    for fid,target in rates.items():
        if fid not in firms:reasons.append('unknown_firm:'+str(fid));continue
        f=firms[fid]
        if not finite(target) or target<0:reasons.append('invalid_rate:'+fid);continue
        if abs(target-f['rate'])<EPS:continue
        if any(c['firm']==fid and c.get('route_reserved') for c in commitments):
            reasons.append('restart_locked:'+fid);continue
        if any(o['firm']==fid and o['kind']=='rate' and o['status'] in PENDING and o['target']!=target for o in view.get('orders',[])):
            reasons.append('pending_order:'+fid);continue
        if target not in action_options(view,fid):reasons.append('unknown_or_unsupported_target:'+fid);continue
        if target>f['max_rate']+EPS:reasons.append('above_firm_max:'+fid);continue
        if not any(o['firm']==fid and o['kind']=='rate' and o['status'] in PENDING and o['target']==target for o in view.get('orders',[])):required+=1
        targets[fid]=target
    routes=view.get('routes',[]);routes=routes.values() if isinstance(routes,dict) else routes
    # Existing or pending reservations remain committed until their replacement takes effect.
    for o in view.get('orders',[]):
        if o['kind']=='rate' and o['status'] in PENDING:targets[o['firm']]=max(targets[o['firm']],o['target'])
    for c in commitments:
        if c.get('route_reserved') and c['firm'] in targets:targets[c['firm']]=max(targets[c['firm']],c['target'])
    for route in routes:
        peak=sum(max(firms[i]['rate'],target) for i,target in targets.items() if firms[i]['route']==route['id'])
        if peak>route['capacity']+EPS:reasons.append('route_peak_capacity:'+route['id'])
    budget=view.get('notice_budget',{})
    remaining=budget.get('remaining')
    reserved=sum(bool(c.get('reservation_active')) for c in commitments)
    closures=sum(fid in firms and finite(target) and target==0 and firms[fid]['rate']>EPS and
        not any(c['firm']==fid and c.get('route_reserved') for c in commitments) and
        not any(o['firm']==fid and o['kind']=='rate' and o['status'] in PENDING and o['target']==0 for o in view.get('orders',[]))
        for fid,target in rates.items()) if guard else 0
    if remaining is not None and required+reserved+closures>remaining:reasons.append('notice_budget')
    return dict(allowed=not reasons,reasons=reasons,required_notices=required)

"""Finite, confirmed storage transfer and return; volumes never imply treatment.

The one common pump is interruptible at 5-minute cell boundaries. Transfer orders
reserve capacity, but do not reserve water that has not physically arrived. A
storage/return cancellation releases un-departed reservations only. All numeric
capabilities and water-class declarations are explicit simulation assumptions.
"""
from copy import deepcopy
from ..wastewater_workbench_v3.capabilities import EPS, finite
from ..wastewater_workbench_v3.recovery import receiving_status

ACTIVE = ('notified', 'acknowledged', 'transferring', 'blocked', 'in_transit')


def enabled(s):
    """Whether extension accounting exists, independently of storage availability."""
    d=s.get('disposition', {})
    return d.get('configured') is True or d.get('enabled') is True


def _firms(s):
    values=s.get('firms',{})
    return values if isinstance(values,dict) else {f['id']:f for f in values}


def _routes(s):
    values=s.get('routes',{})
    return values if isinstance(values,dict) else {r['id']:r for r in values}


def _positive(value, name, zero=False):
    if not finite(value) or value < 0 or (not zero and value==0):
        raise ValueError('invalid disposition '+name)


def _confirmation(record, now):
    if not isinstance(record,dict) or not isinstance(record.get('known'),bool):
        raise ValueError('acceptance known must be boolean')
    for key in ('confirmed_at_minutes','valid_until_minutes'):
        _positive(record.get(key),key,True)
    if record['confirmed_at_minutes']>now or record['valid_until_minutes']<=record['confirmed_at_minutes']:
        raise ValueError('future or empty acceptance confirmation')
    types=record.get('allowed_types')
    if types is not None and (not isinstance(types,list) or any(not isinstance(v,str) or not v.strip() for v in types)):
        raise ValueError('acceptance types must be nonempty strings')
    if record['known'] and types is None:raise ValueError('known acceptance requires allowed_types')


def initialize(s, config):
    if not isinstance(config,dict) or not isinstance(config.get('enabled',False),bool):
        raise ValueError('disposition must have boolean enabled')
    if not config.get('enabled') and set(config)<= {'enabled'}:return
    d=deepcopy(config)
    d['configured']=True
    for key in ('storage_capacity_m3','pump_rate_m3_h'):_positive(d.get(key),key)
    for key in ('travel_minutes','ack_delay_minutes','setup_minutes'):
        _positive(d.get(key),key,True)
        if d[key]%5:raise ValueError(key+' must use 5-minute grid')
    if d.get('return_route') not in s['routes']:raise ValueError('unknown return route')
    if not isinstance(d.get('firm_types'),dict):raise ValueError('firm_types required')
    for fid in s['firms']:
        kind=d['firm_types'].get(fid)
        if kind is not None and (not isinstance(kind,str) or not kind.strip()):raise ValueError('invalid firm water type')
    for destination in ('storage','plant'):
        _confirmation(d.get('acceptance',{}).get(destination),s['minute'])
        record=d['acceptance'][destination]
        record.setdefault('basis','assumed')
        record.setdefault('source','Synthetic class-acceptance declaration; no chemical compatibility proof')
        record.setdefault('revision',0)
    inventory={fid:0. for fid in s['firms']}
    for fid,value in d.pop('initial_inventory',{}).items():
        if fid not in inventory:raise ValueError('initial inventory unknown firm')
        _positive(value,'initial inventory',True);inventory[fid]=float(value)
    if sum(inventory.values())>d['storage_capacity_m3']+EPS:raise ValueError('initial storage over capacity')
    d.update(inventory=inventory,orders=[],storage_transit=[],next_order=1,
        metrics=dict(departed_to_storage_m3=0.,arrived_storage_m3=0.,departed_return_m3=0.,
            arrived_return_route_m3=0.,occupancy_m3_minutes=0.,pump_runtime_minutes=0.,
            confirmation_changes=0,blocked_departure_cells=0),_pump_remaining_m3=0.)
    s['disposition']=d
    s['initial']+=sum(inventory.values())
    s['assumptions'].append('v5 synthetic local start-permission interlock: new physical starts recheck current plant/class acceptance; actual site interlock capability is unverified. Already-running observed flow is not automatically stopped by a report change.')


def acceptance_reasons(s, destination, firm):
    if not enabled(s):return []
    d=s['disposition'];kind=d['firm_types'].get(firm)
    if kind is None:return ['water_type_unknown']
    report=d['acceptance'].get(destination)
    if not report or report.get('known') is not True:return ['acceptance_unknown']
    now=s['minute']
    if report['confirmed_at_minutes']>now:return ['acceptance_future']
    if now>=report['valid_until_minutes']:return ['acceptance_expired']
    if kind not in (report.get('allowed_types') or []):return ['incompatible_type']
    return []


def rate_reasons(s, rates):
    if not enabled(s) or not isinstance(rates,dict):return []
    reasons=[];firms=_firms(s)
    for fid,target in rates.items():
        if fid in firms and finite(target) and target>firms[fid]['rate']+EPS:
            reasons += [reason+':plant:'+fid for reason in acceptance_reasons(s,'plant',fid)]
    return reasons


def _remaining(order):
    return max(0.,order['volume_m3']-order['departed_m3']) if order['status'] in ACTIVE else 0.


def _reserved(d,direction,firm=None):
    return sum(_remaining(o) for o in d['orders'] if o['direction']==direction and (firm is None or o['firm']==firm))


def storage_used(s):
    if not enabled(s):return 0.
    d=s['disposition']
    return sum(d['inventory'].values())+sum(x['volume'] for x in d['storage_transit'])


def extra_stored(s):
    return storage_used(s)


def assess(view, direction, firm, volume):
    reasons=[];available=0.
    if not enabled(view):return dict(allowed=False,reasons=['disposition_disabled'],available=0.)
    if direction not in ('store','return'):return dict(allowed=False,reasons=['unknown_direction'],available=0.)
    if firm not in _firms(view):return dict(allowed=False,reasons=['unknown_firm'],available=0.)
    if not finite(volume) or volume<=0:return dict(allowed=False,reasons=['invalid_volume'],available=0.)
    if view['tick']>=view['end_tick']:reasons.append('simulation_ended')
    d=view['disposition'];dest='storage' if direction=='store' else 'plant'
    if direction=='store' and not d.get('enabled'):reasons.append('storage_resource_disabled')
    reasons+=acceptance_reasons(view,dest,firm)
    budget=view.get('notice_budget',{})
    available_notices=budget.get('available',budget.get('remaining'))
    if available_notices is not None and available_notices<1:reasons.append('notice_budget')
    if direction=='store':
        available=max(0.,d['storage_capacity_m3']-storage_used(view)-_reserved(d,'store'))
        source=max(0.,_firms(view)[firm]['stock']-_reserved(d,'store',firm))
        if volume>source+EPS:reasons.append('source_volume_unavailable')
    else:
        r=_routes(view)[d['return_route']]
        route_stock=r['stock']+sum(x['volume'] for x in view['transit'] if x['route']==r['id'])
        route_free=max(0.,r['buffer']-route_stock-_reserved(d,'return'))
        source=max(0.,d['inventory'][firm]-_reserved(d,'return',firm))
        available=min(source,route_free)
        status=receiving_status(view['recovery'],view['minute'])
        if not status['known']:reasons.append(status['reason'])
        elif status['capacity_m3_h']<=EPS:reasons.append('no_receiving_capacity')
        if volume>source+EPS:reasons.append('source_volume_unavailable')
    if volume>available+EPS:reasons.append('destination_capacity_reserved')
    return dict(allowed=not reasons,reasons=sorted(set(reasons)),available=available)


def _log(s,kind,**details):
    s['events'].append(dict(tick=s['minute']/15,minute=s['minute'],kind=kind,reason=details.pop('reason',''),**details))


def request(s,direction,firm,volume):
    from . import engine
    gate=assess(engine.public_state(s),direction,firm,volume)
    if not gate['allowed']:raise ValueError(';'.join(gate['reasons']))
    d=s['disposition'];now=s['minute']
    o=dict(id='D'+str(d['next_order']),direction=direction,firm=firm,volume_m3=float(volume),
        departed_m3=0.,arrived_m3=0.,status='notified',created_minutes=now,
        ack_due_minutes=now+d['ack_delay_minutes'],ack_at_minutes=None,
        earliest_minutes=now+d['ack_delay_minutes']+d['setup_minutes'],
        completed_minutes=None,cancelled_minutes=None,reasons=[])
    d['next_order']+=1;d['orders'].append(o)
    _log(s,'disposition_notice',order_id=o['id'],firm=firm,direction=direction,volume=volume)
    return o


def feedback(s,order_id,decision):
    if not enabled(s):raise ValueError('disposition disabled')
    orders=[o for o in s['disposition']['orders'] if o['id']==order_id]
    if len(orders)!=1 or decision not in ('ack','reject','cancel'):raise ValueError('invalid disposition feedback')
    o=orders[0]
    if o['status'] not in ACTIVE:raise ValueError('terminal disposition cannot be revived')
    if decision=='ack':
        if o['status']!='notified':raise ValueError('already acknowledged')
        if s['minute']<o['ack_due_minutes']:raise ValueError('acknowledgement is too early')
        o['ack_at_minutes']=s['minute'];o['earliest_minutes']=s['minute']+s['disposition']['setup_minutes']
        o['status']='acknowledged'
    elif decision=='reject':
        if o['status']!='notified':raise ValueError('reject only unacknowledged order')
        o['status']='rejected';o['reasons']=['operator_rejected']
    else:o['status']='cancelled';o['cancelled_minutes']=s['minute'];o['reasons']=['operator_cancelled_undeparted']
    _log(s,'disposition_'+decision,order_id=order_id,firm=o['firm'])


def confirm(s,destination,known,valid_for_minutes,allowed_types=None):
    if not enabled(s) or destination not in ('storage','plant'):raise ValueError('unknown destination')
    _positive(valid_for_minutes,'valid_for_minutes')
    record=dict(known=known,allowed_types=deepcopy(allowed_types),confirmed_at_minutes=s['minute'],
        valid_until_minutes=s['minute']+valid_for_minutes,
        revision=s['disposition']['acceptance'][destination].get('revision',0)+1,
        basis='assumed',source='Synthetic operator confirmation; not a verified site acceptance')
    _confirmation(record,s['minute']);s['disposition']['acceptance'][destination]=record
    s['disposition']['metrics']['confirmation_changes']+=1
    _log(s,'acceptance_confirmation',destination=destination,**deepcopy(record))
    return record


def _arrived(s,packet):
    d=s['disposition'];o=next(o for o in d['orders'] if o['id']==packet['disposition_order_id'])
    o['arrived_m3']+=packet['volume']
    if o['direction']=='store':
        d['inventory'][o['firm']]+=packet['volume'];d['metrics']['arrived_storage_m3']+=packet['volume']
    else:d['metrics']['arrived_return_route_m3']+=packet['volume']
    if o['status'] not in ('cancelled','rejected') and o['arrived_m3']>=o['volume_m3']-EPS:
        o['status']='complete';o['completed_minutes']=s['minute'];o['reasons']=[]
    _log(s,'disposition_arrival',order_id=o['id'],firm=o['firm'],direction=o['direction'],volume=packet['volume'])


def begin_cell(s):
    if not enabled(s):return
    d=s['disposition'];d['_pump_remaining_m3']=d['pump_rate_m3_h']*5/60
    due=[x for x in d['storage_transit'] if x['due_minutes']<=s['minute']]
    d['storage_transit']=[x for x in d['storage_transit'] if x['due_minutes']>s['minute']]
    for packet in due:_arrived(s,packet)
    d['metrics']['occupancy_m3_minutes']+=sum(d['inventory'].values())*5
    for o in d['orders']:
        if o['status']=='notified' and s['auto_ack'] and s['minute']>=o['ack_due_minutes']:
            feedback(s,o['id'],'ack')


def route_arrival(s,packet):
    if enabled(s) and packet.get('disposition_order_id'):_arrived(s,packet)


def _blocked(s,o,reasons):
    reasons=sorted(set(reasons))
    if o['status']!='blocked' or o['reasons']!=reasons:
        _log(s,'disposition_blocked',order_id=o['id'],firm=o['firm'],
            direction=o['direction'],reason=';'.join(reasons))
    o['status']='blocked';o['reasons']=reasons


def _ready(s,o):
    if o['status'] not in ('acknowledged','transferring','blocked') or s['minute']<o['earliest_minutes']:return False
    reasons=acceptance_reasons(s,'storage' if o['direction']=='store' else 'plant',o['firm'])
    if o['direction']=='return':
        rec=receiving_status(s['recovery'],s['minute'])
        if not rec['known']:reasons.append(rec['reason'])
        elif rec['capacity_m3_h']<=EPS:reasons.append('no_receiving_capacity')
    if reasons:
        _blocked(s,o,reasons);s['disposition']['metrics']['blocked_departure_cells']+=1
        return False
    return True


def _depart(s,o,volume,route=None):
    if volume<=EPS:return
    d=s['disposition'];o['departed_m3']+=volume;d['_pump_remaining_m3']-=volume
    if o['status']=='blocked':
        _log(s,'disposition_resumed',order_id=o['id'],firm=o['firm'],direction=o['direction'],reason=';'.join(o['reasons']))
    o['reasons']=[]
    o['status']='in_transit' if o['departed_m3']>=o['volume_m3']-EPS else 'transferring'
    due=s['minute']+5+d['travel_minutes']
    packet=dict(firm=o['firm'],volume=volume,due_minutes=due,disposition_order_id=o['id'])
    if o['direction']=='store':
        s['firms'][o['firm']]['stock']-=volume;d['storage_transit'].append(packet)
        d['metrics']['departed_to_storage_m3']+=volume
    else:
        d['inventory'][o['firm']]-=volume;packet.update(route=route,due_minutes=due+s['routes'][route]['delay']*15)
        packet['due']=packet['due_minutes']/15;s['transit'].append(packet)
        d['metrics']['departed_return_m3']+=volume
    d['metrics']['pump_runtime_minutes']+=volume/d['pump_rate_m3_h']*60
    _log(s,'disposition_departure',order_id=o['id'],firm=o['firm'],direction=o['direction'],volume=volume)


def store_departures(s):
    if not enabled(s):return
    d=s['disposition']
    if not d.get('enabled'):return
    for o in d['orders']:
        if o['direction']!='store' or not _ready(s,o):continue
        headroom=max(0.,d['storage_capacity_m3']-storage_used(s))
        volume=min(_remaining(o),s['firms'][o['firm']]['stock'],d['_pump_remaining_m3'],headroom)
        _depart(s,o,volume)
        if volume<=EPS:_blocked(s,o,['storage_source_or_pump_capacity'])


def return_departures(s, route_available, direct_peak):
    if not enabled(s):return
    d=s['disposition'];rid=d['return_route'];r=s['routes'][rid]
    rec=receiving_status(s['recovery'],s['minute'])
    if not rec['known']:capacity=0.
    else:capacity=min(rec['capacity_m3_h'],s['_physical_capacity'])
    receiving_peak=max(0.,capacity-direct_peak)
    for o in d['orders']:
        if o['direction']!='return' or not _ready(s,o):continue
        in_transit=sum(x['volume'] for x in s['transit'] if x['route']==rid)
        headroom=max(0.,r['buffer']-r['stock']-in_transit)
        volume=min(_remaining(o),d['inventory'][o['firm']],d['_pump_remaining_m3'],headroom,
            max(0.,route_available.get(rid,0.))*5/60,receiving_peak*5/60)
        _depart(s,o,volume,rid)
        route_available[rid]-=volume*60/5;receiving_peak-=volume*60/5
        if volume<=EPS:
            _blocked(s,o,['return_route_or_receiving_or_pump_capacity'])


def public(s):
    if not enabled(s):return None
    d=deepcopy(s['disposition']);d.pop('_pump_remaining_m3',None)
    pending=_reserved(d,'store');d['capacity_reserved_m3']=storage_used(s)+pending
    d['available_storage_m3']=max(0.,d['storage_capacity_m3']-d['capacity_reserved_m3'])
    d['pending_volume_m3']=sum(_remaining(o) for o in d['orders'])
    d['unresolved_m3']=sum(f['stock'] for f in s['firms'].values())+sum(r['stock'] for r in s['routes'].values())+sum(x['volume'] for x in s['transit'])+storage_used(s)
    d['in_storage_m3']=sum(d['inventory'].values())
    d['in_transfer_m3']=sum(x['volume'] for x in d['storage_transit'])
    d['open_orders']=sum(o['status'] in ACTIVE for o in d['orders'])
    return d


def restore(view):
    """Only disposition-owned public state, never future scenario or physical data."""
    keys=('enabled','configured','storage_capacity_m3','pump_rate_m3_h','travel_minutes','ack_delay_minutes',
        'setup_minutes','return_route','firm_types','acceptance','inventory','orders',
        'storage_transit','next_order','metrics')
    d={k:deepcopy(view[k]) for k in keys if k in view};d['_pump_remaining_m3']=0.
    allowed_confirmation=('known','allowed_types','confirmed_at_minutes','valid_until_minutes','revision','basis','source')
    d['acceptance']={dest:{k:deepcopy(v) for k,v in record.items() if k in allowed_confirmation}
        for dest,record in d['acceptance'].items() if dest in ('plant','storage')}
    order_keys=('id','direction','firm','volume_m3','departed_m3','arrived_m3','status',
        'created_minutes','ack_due_minutes','ack_at_minutes','earliest_minutes',
        'completed_minutes','cancelled_minutes','reasons')
    d['orders']=[{k:deepcopy(v) for k,v in o.items() if k in order_keys} for o in d['orders']]
    d['storage_transit']=[{k:deepcopy(v) for k,v in x.items() if k in
        ('firm','volume','due_minutes','disposition_order_id')} for x in d['storage_transit']]
    metrics_keys=('departed_to_storage_m3','arrived_storage_m3','departed_return_m3',
        'arrived_return_route_m3','occupancy_m3_minutes','pump_runtime_minutes',
        'confirmation_changes','blocked_departure_cells')
    d['metrics']={k:v for k,v in d['metrics'].items() if k in metrics_keys}
    return d

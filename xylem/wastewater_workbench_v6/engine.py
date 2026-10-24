"""Synthetic 5-minute actuator execution with explicit acknowledgements and provenance.

Volume-only model: received water is never labelled compliant treatment. Local fixed
pumps run at duty flow; dry protection may end a runtime part-way through a cell.
Variable devices ramp linearly within each 5-minute cell. No field calibration.
"""
from copy import deepcopy
import math
from ..wastewater_workbench_v3.capabilities import (PENDING,EPS,finite,validate_profile,unknown_fields,
                           action_options,assess_bundle,profile_status)
from ..wastewater_workbench_v3.recovery import receiving_status,validate_confirmation,STATUS_FIELDS
from ..wastewater_workbench_v3 import restart_guard
from ..wastewater_workbench_v5 import disposition
from . import production as production_model


def number(value,low,high,label):
    if not finite(value) or not low<=value<=high:raise ValueError(label+' must be finite and within bounds')
    return value


def log(s,kind,reason='',minute=None,**details):
    t=s['minute'] if minute is None else minute
    s['events'].append(dict(tick=t/15,minute=t,kind=kind,reason=reason,**details))


def _grid(value,label):
    if not finite(value) or value<0 or value%5:raise ValueError(label+' must use 5-minute grid')


MAINTENANCE_PLAN_KEYS=('expected_restore_minutes','announced_at_minutes','revision','basis','source')


def _maintenance_plan(report,now,previous=None):
    """A dated ETA is an estimate; it never grants receiving capacity."""
    if not isinstance(report,dict):raise ValueError('maintenance notice must be an object')
    for key in ('expected_restore_minutes','announced_at_minutes','revision'):
        if not finite(report.get(key)) or report[key]<0:raise ValueError('invalid maintenance notice '+key)
    if report['announced_at_minutes']>now:raise ValueError('maintenance announcement is in the future')
    if report['expected_restore_minutes']<now:raise ValueError('new maintenance ETA is already in the past')
    if int(report['revision'])!=report['revision']:raise ValueError('maintenance revision must be an integer')
    if report.get('basis') not in ('assumed','verified') or not isinstance(report.get('source'),str) or not report['source'].strip():
        raise ValueError('maintenance notice provenance required')
    if previous and (report['revision']<=previous['revision'] or report['announced_at_minutes']<previous['announced_at_minutes']):
        raise ValueError('stale maintenance notice')
    return {key:deepcopy(report[key]) for key in MAINTENANCE_PLAN_KEYS}


def new_simulation(config):
    c=deepcopy(config)
    if c.get('step_minutes')!=15 or c.get('substep_minutes',5)!=5:raise ValueError('only 15-minute ticks with 5-minute substeps are supported')
    for key in ('end_tick','production_end'):
        number(c[key],0,100000,key)
        if int(c[key])!=c[key]:raise ValueError('tick fields must be integers')
    caps=deepcopy(c.get('capabilities',{}))
    for fid in c['firms']:validate_profile(caps.get(fid,{}))
    rec={k:None for k in STATUS_FIELDS};rec.update(deepcopy(c.get('recovery',{})))
    for key in STATUS_FIELDS:
        if rec[key] is not None and not isinstance(rec[key],bool):raise ValueError('invalid maintenance/process status')
    if rec.get('capacity_confirmation'):validate_confirmation(rec['capacity_confirmation'],0)
    number(c['downstream_capacity'],0,100000,'physical capacity')
    s=dict(schema=2,name=c['name'],tick=0,minute=0,end_tick=c['end_tick'],
        production_end=c['production_end'],step_minutes=15,substep_minutes=5,
        start_hour=c.get('start_hour',0),mode='simulation',auto_ack=c.get('auto_ack',True),
        firms=deepcopy(c['firms']),routes=deepcopy(c['routes']),capabilities=caps,recovery=rec,
        _physical_capacity=c['downstream_capacity'],scenario=c,transit=[],orders=[],next_order=1,
        totals=dict(generated=0.,received=0.,uncontained=0.,prevented=0.),events=[],world_events=[],history=[],
        assumptions=deepcopy(c.get('assumptions',[]))+['v2 synthetic volume-only model; 5-minute linear ramps and true fixed-duty runtime; no field-executable claim.'],
        notice_budget=deepcopy(c.get('notice_budget',dict(limit=None,window_minutes=1440))))
    policy=c.get('restart_guard',{'enabled':False})
    if not isinstance(policy,dict) or not isinstance(policy.get('enabled'),bool):raise ValueError('restart_guard.enabled must be boolean')
    s['restart_guard']={'enabled':policy['enabled']};s['restart_commitments']=[]
    b=s['notice_budget'];b.setdefault('limit',None);b.setdefault('window_minutes',1440)
    if b['limit'] is not None and (not finite(b['limit']) or b['limit']<0 or int(b['limit'])!=b['limit']):raise ValueError('invalid notice limit')
    _grid(b['window_minutes'],'notice window')
    if not b['window_minutes']:raise ValueError('empty notice window')
    for rid,r in s['routes'].items():
        if r.get('id')!=rid:raise ValueError('route key/id mismatch')
        for key in ('capacity','buffer'):number(r[key],EPS,100000,key)
        number(r['stock'],0,r['buffer'],'route stock');number(r['delay'],0,100000,'route delay')
        if int(r['delay'])!=r['delay']:raise ValueError('route delay must be whole ticks')
        r.setdefault('last_received',0.);r.setdefault('uncontained',0.)
    for fid,f in s['firms'].items():
        if f.get('id')!=fid or f['route'] not in s['routes']:raise ValueError('invalid firm route/id')
        number(f['capacity'],EPS,100000,'firm capacity');number(f['stock'],0,f['capacity'],'firm stock')
        number(f['max_rate'],0,100000,'max_rate');number(f['rate'],0,f['max_rate'],'rate')
        for key in ('ack_delay','switch_delay','batch_ticks'):
            number(f[key],0 if key!='batch_ticks' else 1,100000,key)
            if int(f[key])!=f[key]:raise ValueError('delays/batches must be whole ticks')
        for key in ('production_rate','production_multiplier'):number(f[key],0,100000,key)
        p=caps.get(fid,{})
        # Known fixed-duty physics applies even when provenance or protections are incomplete.
        if p.get('mode') in ('on_off','window') and p.get('duty_rate') is not None and f['rate'] not in (0,p['duty_rate']):raise ValueError('initial rate not supported by profile')
        if not unknown_fields(p) and f['rate'] not in action_options({'capabilities':caps},fid):raise ValueError('initial rate not supported by profile')
        if p.get('duty_rate',0) and p.get('mode') in ('on_off','window') and p['duty_rate']>s['routes'][f['route']]['capacity']+EPS:raise ValueError('fixed duty exceeds physical route capacity')
        f.setdefault('uncontained',0.);f['last_release']=0.
        f['actuator']=dict(running=f['rate']>0,physical_rate=float(f['rate']),last_start_minutes=-60.,last_stop_minutes=-60.,
            last_setpoint_minutes=f.get('last_switch',-4)*15,start_minutes=[],runtime_minutes=0.,starts=0,stops=0)
    for r in s['routes'].values():
        if sum(f['rate'] for f in s['firms'].values() if f['route']==r['id'])>r['capacity']+EPS:raise ValueError('initial route peak exceeds capacity')
    for e in c.get('disturbances',[]):
        _grid(e.get('minute',e.get('tick',0)*15),'event time')
    s['initial']=sum(f['stock'] for f in s['firms'].values())+sum(r['stock'] for r in s['routes'].values())
    if c.get('maintenance_plan') is not None:s['maintenance_plan']=_maintenance_plan(c['maintenance_plan'],0)
    disposition.initialize(s,c.get('disposition',{'enabled':False}))
    production_model.initialize(s,c.get('production_control'))
    if 'receiving_ceiling' in c:s['receiving_ceiling']=deepcopy(c['receiving_ceiling'])
    if 'production_floor' in c:s['production_floor']=deepcopy(c['production_floor'])
    s['history']=[history_entry(s)]
    return s


def mass_balance(s):
    stored=sum(f['stock'] for f in s['firms'].values())+sum(r['stock'] for r in s['routes'].values())+sum(x['volume'] for x in s['transit'])+disposition.extra_stored(s)
    t=s['totals']
    return dict(initial=s['initial'],input=t['generated'],stored=stored,received=t['received'],uncontained=t['uncontained'],error=s['initial']+t['generated']-stored-t['received']-t['uncontained'])


def history_entry(s):
    return dict(tick=s['tick'],minute=s['minute'],firm_stocks={i:f['stock'] for i,f in s['firms'].items()},
        route_stocks={i:r['stock'] for i,r in s['routes'].items()},firm_rates={i:f['rate'] for i,f in s['firms'].items()},
        physical_rates={i:f['actuator']['physical_rate'] for i,f in s['firms'].items()},
        runtime_minutes={i:f['actuator']['runtime_minutes'] for i,f in s['firms'].items()},
        starts={i:f['actuator']['starts'] for i,f in s['firms'].items()},stops={i:f['actuator']['stops'] for i,f in s['firms'].items()},
        transit=sum(x['volume'] for x in s['transit']),**deepcopy(s['totals']))


def _budget(s):
    b=deepcopy(s['notice_budget']);width=b['window_minutes'];start=(s['minute']//width)*width
    b['used']=sum(start<=o['created_minutes']<start+width for o in s['orders'])
    if disposition.enabled(s):
        b['used']+=sum(start<=o['created_minutes']<start+width for o in s['disposition']['orders'])
    b['remaining']=None if b['limit'] is None else max(0,b['limit']-b['used'])
    if restart_guard.enabled(s):
        b['reserved']=restart_guard.reserved(s)
        b['available']=None if b['remaining'] is None else max(0,b['remaining']-b['reserved'])
    return b


def public_state(s):
    keys=('schema','name','tick','minute','end_tick','production_end','step_minutes','substep_minutes','start_hour','mode','auto_ack',
          'transit','orders','totals','history','events','assumptions','capabilities','recovery')
    out={k:deepcopy(s[k]) for k in keys};out['firms']=deepcopy(list(s['firms'].values()));out['routes']=deepcopy(list(s['routes'].values()))
    for r in out['routes']:r['in_transit']=sum(x['volume'] for x in s['transit'] if x['route']==r['id'])
    out['downstream_capacity']=receiving_status(s['recovery'],s['minute'])['capacity_m3_h']
    out['notice_budget']=_budget(s);out['balance']=mass_balance(s)
    out['restart_guard']={'enabled':restart_guard.enabled(s)}
    out['restart_commitments']=restart_guard.public_records(s.get('restart_commitments',[]))
    if disposition.enabled(s):out['disposition']=disposition.public(s)
    if production_model.enabled(s):out['production_control']=production_model.public(s)
    if 'receiving_ceiling' in s:out['receiving_ceiling']=_only(s['receiving_ceiling'],('value','valid_until_minutes','basis','source'))
    if 'production_floor' in s:out['production_floor']=_only(s['production_floor'],('rates_m3_h','valid_until_minutes','basis','source'))
    if 'maintenance_plan' in s:out['maintenance_plan']=_only(s['maintenance_plan'],MAINTENANCE_PLAN_KEYS)
    return out


def publish(s,rates,production=None):
    gate=assess_action(public_state(s),rates,production)
    if not gate['allowed']:raise ValueError(';'.join(gate['reasons']))
    made=_publish_basic(s,rates,production)
    restart_guard.register(s,made,gate.get('restart_projections',[]),log)
    return made


def _publish_basic(s,rates,production=None,*,_commitment=None):
    """Ordinary atomic publisher; only the engine may consume a bound token.

    This internal path is used by proof clones and automatic restoration so
    assessment never recursively proves its own close/restore trajectory.
    """
    if s['tick']>=s['end_tick']:raise ValueError('simulation ended')
    if not isinstance(rates,dict) or (production is not None and not isinstance(production,dict)):raise ValueError('rate/production requests must be objects')
    view=public_state(s)
    if _commitment is not None:
        if not any(c is _commitment for c in s['restart_commitments']) or not _commitment['reservation_active'] or rates!={_commitment['firm']:_commitment['target']} or production:
            raise ValueError('invalid_internal_reservation')
        view['restart_commitments']=[c for c in view['restart_commitments'] if c['id']!=_commitment['id']]
    gate=assess_bundle(view,rates)
    if not gate['allowed']:raise ValueError(';'.join(gate['reasons']))
    compatibility=disposition.rate_reasons(view,rates)
    if compatibility:raise ValueError(';'.join(compatibility))
    requests=[]
    for kind,values in (('rate',rates),('production',production or {})):
        for fid,target in values.items():
            if fid not in s['firms']:raise ValueError('unknown firm')
            if kind=='production' and not isinstance(target,bool):raise ValueError('production target must be boolean')
            f=s['firms'][fid];current=f['rate'] if kind=='rate' else f['production_enabled']
            if target==current:continue
            pending=[o for o in s['orders'] if o['firm']==fid and o['kind']==kind and o['status'] in PENDING]
            if pending:
                if pending[-1]['target']==target:continue
                raise ValueError('pending_order:'+fid)
            requests.append((kind,fid,target))
    remaining=_budget(s)['remaining']
    reserved=restart_guard.reserved(view)
    extra=len(restart_guard.new_closures(view,rates))
    if remaining is not None and len(requests)+reserved+extra>remaining:raise ValueError('notice_budget')
    if restart_guard.enabled(s):
        for kind,fid,target in requests:
            if any(not finite(s['firms'][fid].get(key)) for key in ('ack_delay','switch_delay','batch_ticks')):
                raise ValueError('required_dispatch_timing_unknown:'+fid)
    made=[]
    for kind,fid,target in requests:
        f=s['firms'][fid];now=s['minute'];due=now+f['ack_delay']*15;earliest=due+f['switch_delay']*15
        if kind=='rate':earliest=max(earliest,f['actuator']['last_setpoint_minutes']+s['capabilities'][fid]['min_setpoint_hold_minutes'])
        elif production_model.enabled(s):due,earliest=production_model.timing(s,fid,target)
        else:earliest=math.ceil(earliest/(15*f['batch_ticks']))*(15*f['batch_ticks'])
        o=dict(id='O'+str(s['next_order']),firm=fid,kind=kind,target=target,status='notified',created=s['tick'],created_minutes=now,
            ack_due=due/15,ack_due_minutes=due,earliest=earliest/15,earliest_minutes=earliest,ack_at=None,effective_at=None,
            ack_at_minutes=None,effective_at_minutes=None)
        s['next_order']+=1;s['orders'].append(o);made.append(o)
        log(s,'notice','awaiting_ack',firm=fid,order_id=o['id'],value=target,order_kind=kind)
    return made


def assess_action(view,rates,production=None):
    """Shared admission, including a public-only ordinary close/restore proof.

    required_notices counts immediate actual orders. reserved_notices counts
    new future restore tokens; neither reservations nor projections are orders.
    """
    gate=assess_bundle(view,rates);reasons=list(gate['reasons'])
    reasons+=disposition.rate_reasons(view,rates)
    result=dict(gate,reserved_notices=len(restart_guard.new_closures(view,rates)),restart_projections=[])
    firms=view.get('firms',[]);firms=firms if isinstance(firms,dict) else {f['id']:f for f in firms}
    if production is not None and not isinstance(production,dict):reasons.append('production_must_be_object')
    else:
        for fid,target in (production or {}).items():
            if fid not in firms:reasons.append('unknown_firm:'+str(fid));continue
            if not isinstance(target,bool):reasons.append('production_target_must_be_boolean:'+fid);continue
            reasons+=production_model.admission(view,fid,target)
            if target==firms[fid]['production_enabled']:continue
            pending=[o for o in view.get('orders',[]) if o['firm']==fid and o['kind']=='production' and o['status'] in PENDING]
            if pending:
                if pending[-1]['target']!=target:reasons.append('pending_order:'+fid)
            else:result['required_notices']+=1
    remaining=view.get('notice_budget',{}).get('remaining')
    if remaining is not None and result['required_notices']+restart_guard.reserved(view)+result['reserved_notices']>remaining:
        reasons.append('notice_budget')
    closures=restart_guard.new_closures(view,rates)
    if not reasons and closures:
        status=receiving_status(view.get('recovery',{}),view['minute'])
        until=restart_guard.deadline(view)
        if not status['known']:reasons.append('restart_unconfirmed:'+status['reason'])
        elif profile_status(view)['unknown_fields']:reasons.append('restart_capability_unknown')
        elif not _observation_known(view):reasons.append('restart_observation_unknown')
        elif until is None or until<=view['minute']:reasons.append('restart_deadline_unknown_or_empty')
        elif view.get('auto_ack') is not True:reasons.append('restart_ack_timing_unconfirmed')
        else:
            s=from_public(view)
            width=view['notice_budget']['window_minutes'];bucket_end=(view['minute']//width+1)*width
            projections=[dict(firm=fid,target=firms[fid]['rate'],deadline_minutes=until,
                bucket_end_minutes=bucket_end) for fid in closures]
            made=_publish_basic(s,rates,production)
            restart_guard.register(s,made,projections,log)
            ids={c['id'] for c in s['restart_commitments'] if c['closing_order_id'] in {o['id'] for o in made}}
            while s['minute']+5<=until:
                _substep(s)
                # Maintain the public clock convention inside partial-tick proof.
                s['tick']=s['minute']//15
                active=[c for c in s['restart_commitments'] if c['id'] in ids]
                if all(c['milestone_minutes'] is not None or c['status'] in ('failed','missed') for c in active):break
            for c in s['restart_commitments']:
                if c['id'] not in ids:continue
                if c['restore_dispatched_minutes'] is None or c['restore_dispatched_minutes']>=bucket_end:
                    reasons.append('restart_notice_bucket:'+c['firm'])
                if c['milestone_minutes'] is None:reasons.append('restart_not_physically_ready_by_deadline:'+c['firm'])
                if 'new_firm_overflow_before_restore' in c['reasons']:reasons.append('restart_firm_overflow:'+c['firm'])
                projection=next(p for p in projections if p['firm']==c['firm'])
                projection.update(projected_milestone_minutes=c['milestone_minutes'],projected_milestone_kind=c['milestone_kind'])
            result['restart_projections']=projections
    result.update(allowed=not reasons,reasons=sorted(set(reasons)))
    return result


def feedback(s,order_id,decision):
    matches=[o for o in s['orders'] if o['id']==order_id]
    if matches and decision=='cancel' and matches[0]['kind']=='production' and matches[0]['status'] in PENDING:
        matches[0]['status']='cancelled';log(s,'production_cancelled','explicit_cancel',firm=matches[0]['firm'],order_id=order_id);return
    if not matches or decision not in ('ack','reject'):raise ValueError('invalid feedback')
    o=matches[0]
    if o['status']!='notified':raise ValueError('terminal or already acknowledged order cannot be revived')
    if decision=='reject':
        o['status']='rejected';log(s,'reject','operator_rejected',firm=o['firm'],order_id=order_id)
        restart_guard.rejection(s,o,log);return
    if s['minute']<o['ack_due_minutes']:raise ValueError('acknowledgement is too early')
    o['status']='acknowledged';o['ack_at']=s['minute']/15;o['ack_at_minutes']=s['minute']
    f=s['firms'][o['firm']];o['earliest_minutes']=max(o['earliest_minutes'],s['minute']+f['switch_delay']*15)
    if o['kind']=='production' and production_model.enabled(s):
        _,o['earliest_minutes']=production_model.timing(s,o['firm'],o['target'],ack_at=s['minute'])
    elif o['kind']=='production':o['earliest_minutes']=math.ceil(o['earliest_minutes']/(15*f['batch_ticks']))*(15*f['batch_ticks'])
    o['earliest']=o['earliest_minutes']/15
    log(s,'ack','waiting_execution',firm=o['firm'],order_id=order_id)


def cancel_restart(s,commitment_id,reason):
    """Explicit ledger cancellation; ordinary pending commands stay pending."""
    if not isinstance(reason,str) or not reason.strip():raise ValueError('cancellation reason required')
    matches=[c for c in restart_guard.outstanding(s) if c['id']==commitment_id]
    if len(matches)!=1:raise ValueError('unknown or completed restart commitment')
    c=matches[0];c.update(status='cancelled',reservation_active=False,route_reserved=False)
    restart_guard.note(s,c,'restart_cancelled','explicit_cancel:'+reason.strip(),log)


def inject(s,kind,**params):
    value=params.get('value')
    if kind=='production_confirmation':
        production_model.update(s,params['firm'],params['record'])
        log(s,kind,'synthetic_owner_report',firm=params['firm'],**deepcopy(params['record']))
        return
    if kind=='maintenance_notice':
        report=_maintenance_plan(params.get('maintenance_plan',params),s['minute'],s.get('maintenance_plan'))
        previous=s.get('maintenance_plan',{}).get('expected_restore_minutes')
        s['maintenance_plan']=report
        log(s,kind,'estimate_only_not_receiving_confirmation',previous_expected_restore_minutes=previous,**deepcopy(report))
        return report
    if kind=='acceptance':
        return disposition.confirm(s,params['destination'],params['known'],params['valid_for_minutes'],params.get('allowed_types'))
    if kind=='disposition_feedback':
        return disposition.feedback(s,params['order_id'],params['decision'])
    if kind=='capacity':
        number(value,0,100000,'physical receiving capacity');s['_physical_capacity']=value
        s['world_events'].append(dict(tick=s['minute']/15,minute=s['minute'],kind=kind,**deepcopy(params)));return
    if kind=='capacity_confirmation':
        report=deepcopy(params.get('capacity_confirmation',params))
        validate_confirmation(report,s['minute'],s['recovery'].get('capacity_confirmation'))
        s['recovery']['capacity_confirmation']=report
        log(s,kind,'reported_receiving_capacity',**deepcopy(report));return
    if kind=='maintenance':
        updates=params.get('statuses',params)
        if any(k not in STATUS_FIELDS or (v is not None and not isinstance(v,bool)) for k,v in updates.items()):raise ValueError('invalid maintenance statuses')
        s['recovery'].update(deepcopy(updates));log(s,kind,'independent_status_update',**deepcopy(updates));return
    if kind not in ('response','production'):raise ValueError('unknown event kind')
    fid=params.get('firm')
    if fid not in s['firms']:raise ValueError('unknown firm')
    number(value,0,100000,kind)
    f=s['firms'][fid]
    if kind=='response':
        if int(value)!=value:raise ValueError('response delay must be whole ticks')
        f['ack_delay']=value
        for o in s['orders']:
            if o['firm']==fid and o['status']=='notified':
                o['ack_due_minutes']=max(o['ack_due_minutes'],s['minute']+value*15)
                o['earliest_minutes']=max(o['earliest_minutes'],o['ack_due_minutes']+f['switch_delay']*15)
                o['ack_due']=o['ack_due_minutes']/15;o['earliest']=o['earliest_minutes']/15
    else:f['production_multiplier']=value
    log(s,kind,'observed_'+kind,firm=fid,value=value)


def _execute(s):
    for o in tuple(s['orders']):
        if o['status']=='notified' and s['auto_ack'] and s['minute']>=o['ack_due_minutes']:feedback(s,o['id'],'ack')
        if o['status']!='acknowledged' or s['minute']<o['earliest_minutes']:continue
        f=s['firms'][o['firm']]
        if o['kind']=='rate':
            compatibility=disposition.rate_reasons(s,{o['firm']:o['target']})
            if compatibility:
                log(s,'deferred',';'.join(compatibility),firm=o['firm'],order_id=o['id']);continue
            hold=s['capabilities'][f['id']].get('min_setpoint_hold_minutes')
            if hold is None or s['minute']<f['actuator']['last_setpoint_minutes']+hold:continue
            old=f['rate'];f['rate']=o['target'];f['last_switch']=s['minute']/15;f['actuator']['last_setpoint_minutes']=s['minute']
            log(s,'setpoint','acknowledged_target_effective',firm=f['id'],order_id=o['id'],value=f['rate'],previous=old)
        else:
            if production_model.enabled(s):
                if not production_model.execute(s,o,log):continue
            else:
                if s['minute']%(15*f['batch_ticks']):continue
                f['production_enabled']=o['target'];log(s,'production_control','batch_boundary',firm=f['id'],value=o['target'],order_id=o['id'])
        o['status']='effective';o['effective_at']=s['minute']/15;o['effective_at_minutes']=s['minute']


def _stop(s,f,reason,minute):
    a=f['actuator']
    if a['running']:
        a['running']=False;a['physical_rate']=0.;a['last_stop_minutes']=minute;a['stops']+=1
        log(s,'stop',reason,minute=minute,firm=f['id'],value=0.)


def _defer(s,f,reason):
    # Retain every constrained interval for audit; no hidden relaxation of protections.
    log(s,'deferred',reason,firm=f['id'],value=f['rate'])


def _release(s,f,route_available):
    p=s['capabilities'].get(f['id'],{});a=f['actuator'];now=s['minute'];target=f['rate']
    # Incomplete records block planning, but do not erase known local protections.
    # An already running actuator may hold its observed flow; missing parameters
    # never grant a new start, ramp transition, or stop permission.
    mode=p.get('mode','unknown');window=True;old=a['physical_rate'] if a['running'] else 0.
    if mode=='window':
        if p.get('windows_minutes') is not None:
            window=any(start<=now<end for start,end in p['windows_minutes'])
        else:
            target=old;_defer(s,f,'unknown_window_hold_observed_flow')
    if not window:
        _stop(s,f,'outside_window',now);return 0.
    if f['stock']<=EPS:
        _stop(s,f,'dry_protection',now);return 0.
    if mode not in ('on_off','window','variable'):
        target=old;_defer(s,f,'unknown_mode_hold_observed_flow')
    if mode in ('on_off','window') and p.get('duty_rate') is None:
        target=old;_defer(s,f,'unknown_duty_hold_observed_flow')
    if target==0 and a['running']:
        minimum_on=p.get('min_on_minutes')
        if minimum_on is None:
            target=old;_defer(s,f,'unknown_minimum_on_hold_observed_flow')
        elif now<a['last_start_minutes']+minimum_on:
            target=old;_defer(s,f,'minimum_on')
    if target>0 and not a['running']:
        # Synthetic local start-permission interlock. A surviving old target
        # is not fresh permission after dry/window protection has stopped flow.
        acceptance=disposition.acceptance_reasons(s,'plant',f['id'])
        if acceptance:
            _defer(s,f,'plant_start_permission:'+','.join(acceptance));return 0.
        if any(p.get(k) is None for k in ('min_on_minutes','min_off_minutes','max_starts_per_hour')):
            _defer(s,f,'unknown_restart_protection');return 0.
        if mode=='window' and (p.get('windows_minutes') is None or not any(start<=now and now+p['min_on_minutes']<=end for start,end in p['windows_minutes'])):
            _defer(s,f,'insufficient_window_for_minimum_on');return 0.
        if now<a['last_stop_minutes']+p['min_off_minutes']:_defer(s,f,'minimum_off');return 0.
        recent=[t for t in a['start_minutes'] if t>now-60]
        if len(recent)>=p['max_starts_per_hour']:_defer(s,f,'max_starts_per_hour');return 0.
    if mode=='variable':
        speed=p.get('ramp_up_m3_h_per_minute') if target>old else p.get('ramp_down_m3_h_per_minute')
        if target!=old and speed is None:
            target=old;_defer(s,f,'unknown_ramp_hold_observed_flow')
        if target==old:new=old;duration=0.
        else:
            new=min(target,old+speed*5) if target>old else max(target,old-speed*5)
            duration=abs(new-old)/speed
        volume=((old+new)/2*duration+new*(5-duration))/60
        peak=max(old,new)
    else:
        new=target;duration=0.;volume=new*5/60;peak=new
    if peak>route_available+EPS:
        # Whole-device local interlock. Never smear or proportionally scale fixed duty.
        _defer(s,f,'route_peak_interlock')
        _stop(s,f,'route_peak_interlock',now);return 0.
    if new<=EPS and old<=EPS:return 0.
    if not a['running']:
        a['running']=True;a['last_start_minutes']=now;a['start_minutes'].append(now);a['starts']+=1
        log(s,'start','local_permission',firm=f['id'],value=new)
    released=min(f['stock'],volume);runtime=5.
    if released+EPS<volume:
        # Solve volume integral for exact emptying time within the piecewise linear ramp.
        if mode=='variable' and duration:
            slope=(new-old)/duration;v_ramp=(old+new)/2*duration/60
            if released<=v_ramp+EPS:
                disc=max(0.,old*old+2*slope*released*60)
                runtime=2*released*60/(old+math.sqrt(disc)) if old+math.sqrt(disc)>EPS else 0.
            else:runtime=duration+(released-v_ramp)*60/new
        else:runtime=released*60/new if new else 0.
        a['runtime_minutes']+=runtime;_stop(s,f,'dry_protection',now+runtime)
    else:
        if new<=EPS:
            runtime=duration; a['runtime_minutes']+=runtime;_stop(s,f,'requested_off',now+duration)
        else:a['runtime_minutes']+=runtime;a['physical_rate']=new
    f['stock']-=released;f['last_release']+=released
    if released>EPS:s['transit'].append(dict(route=f['route'],firm=f['id'],volume=released,due_minutes=now+5+s['routes'][f['route']]['delay']*15,due=(now+5)/15+s['routes'][f['route']]['delay']))
    return peak


def _substep(s):
    now=s['minute']
    for e in s['scenario'].get('disturbances',[]):
        if e.get('minute',e.get('tick',0)*15)==now:inject(s,e['kind'],**{k:v for k,v in e.items() if k not in ('tick','minute','kind')})
    disposition.begin_cell(s)
    _execute(s)
    restart_guard.after_execution(s,_publish_basic,log)
    arrivals=[x for x in s['transit'] if x['due_minutes']<=now]
    s['transit']=[x for x in s['transit'] if x['due_minutes']>now]
    for x in arrivals:
        s['routes'][x['route']]['stock']+=x['volume']
        disposition.route_arrival(s,x)
    receiving=s['_physical_capacity']*5/60
    for r in sorted(s['routes'].values(),key=lambda r:(-r['stock']/r['buffer'],r['id'])):
        got=min(receiving,r['stock']);r['stock']-=got;receiving-=got;r['last_received']+=got;s['totals']['received']+=got
        spill=max(0.,r['stock']-r['buffer']);r['stock']-=spill;r['uncontained']+=spill;s['totals']['uncontained']+=spill
        if spill>EPS:log(s,'overflow','route_buffer',minute=now+5,route=r['id'],volume=spill)
    for f in s['firms'].values():
        potential=f['production_rate']*f['production_multiplier']*5/60 if now<s['production_end']*15 else 0.
        generated=production_model.cell(s,f,potential)
        f['stock']+=generated;s['totals']['generated']+=generated;s['totals']['prevented']+=potential-generated
    cell_released={i:f['last_release'] for i,f in s['firms'].items()} if restart_guard.enabled(s) else {}
    disposition.store_departures(s)
    route_available={};direct_peak=0.
    for rid,r in s['routes'].items():
        available=r['capacity']
        for f in sorted((f for f in s['firms'].values() if f['route']==rid),key=lambda f:(not f['actuator']['running'],f['id'])):
            used=_release(s,f,available);available-=used
            spill=max(0.,f['stock']-f['capacity']);f['stock']-=spill;f['uncontained']+=spill;s['totals']['uncontained']+=spill
            if spill>EPS:log(s,'overflow','firm_buffer',minute=now+5,firm=f['id'],volume=spill)
        route_available[rid]=available;direct_peak+=r['capacity']-available
    disposition.return_departures(s,route_available,direct_peak)
    if restart_guard.enabled(s):
        restart_guard.after_physics(s,log,{i:f['last_release']-cell_released[i] for i,f in s['firms'].items()})
    s['events'].sort(key=lambda event:event['minute'])
    s['minute']+=5
    if abs(mass_balance(s)['error'])>1e-6:raise AssertionError('volume accounting does not conserve mass')


def advance(s,steps=1):
    if isinstance(steps,bool) or not isinstance(steps,int) or not 1<=steps<=100000:raise ValueError('steps must be positive integer')
    for _ in range(min(steps,max(0,s['end_tick']-s['tick']))):
        for f in s['firms'].values():f['last_release']=0.
        for r in s['routes'].values():r['last_received']=0.
        for _sub in range(3):_substep(s)
        s['tick']+=1;s['history'].append(history_entry(s))
    return s


FIRM_KEYS=('id','name','route','stock','capacity','production_rate','rate','max_rate','ack_delay','switch_delay','min_hold','batch_ticks','production_enabled','production_multiplier','last_switch','last_release','uncontained','actuator')
ROUTE_KEYS=('id','name','capacity','buffer','stock','delay','last_received','uncontained','in_transit')
CAP_KEYS=('mode','duty_rate','flow_levels','windows_minutes','min_on_minutes','min_off_minutes','min_setpoint_hold_minutes','max_starts_per_hour','ramp_up_m3_h_per_minute','ramp_down_m3_h_per_minute','basis','source')
ACTUATOR_KEYS=('running','physical_rate','last_start_minutes','last_stop_minutes','last_setpoint_minutes','start_minutes','runtime_minutes','starts','stops')


def _only(record,keys):
    return {k:deepcopy(record[k]) for k in keys if k in record}


def _observation_known(view):
    try:
        for f in view['firms']:
            if not isinstance(f.get('production_enabled'),bool):return False
            if any(not finite(f.get(k)) for k in ('stock','capacity','rate','max_rate','production_rate','production_multiplier','ack_delay','switch_delay','batch_ticks')):return False
            a=f.get('actuator',{})
            if not isinstance(a.get('running'),bool) or any(not finite(a.get(k)) for k in ACTUATOR_KEYS if k not in ('running','start_minutes')):return False
            if not isinstance(a.get('start_minutes'),list) or any(not finite(t) for t in a['start_minutes']):return False
        for r in view['routes']:
            if any(not finite(r.get(k)) for k in ('stock','capacity','buffer','delay')):return False
    except (KeyError,TypeError):return False
    return True


def from_public(view):
    # Explicit allowlist prevents poisoned private/future keys from entering forecasts.
    keys=('schema','name','tick','minute','end_tick','production_end','step_minutes','substep_minutes','start_hour','mode','auto_ack','transit','orders','totals','events','assumptions','capabilities','recovery','notice_budget')
    s={k:deepcopy(view[k]) for k in keys};s['firms']={f['id']:deepcopy(f) for f in view['firms']};s['routes']={r['id']:deepcopy(r) for r in view['routes']}
    s['restart_guard']={'enabled':restart_guard.enabled(view)}
    s['restart_commitments']=restart_guard.public_records(view.get('restart_commitments',[]))
    s['firms']={i:_only(f,FIRM_KEYS) for i,f in s['firms'].items()}
    for f in s['firms'].values():f['actuator']=_only(f['actuator'],ACTUATOR_KEYS)
    s['routes']={i:_only(r,ROUTE_KEYS) for i,r in s['routes'].items()}
    s['capabilities']={i:_only(p,CAP_KEYS) for i,p in s['capabilities'].items()}
    s['totals']=_only(s['totals'],('generated','received','uncontained','prevented'))
    s['notice_budget']=_only(s['notice_budget'],('limit','window_minutes','used','remaining','reserved','available'))
    s['recovery']=_only(s['recovery'],STATUS_FIELDS+('capacity_confirmation',))
    report=s['recovery'].get('capacity_confirmation')
    if isinstance(report,dict):s['recovery']['capacity_confirmation']=_only(report,('value','confirmed_at_minutes','valid_until_minutes','revision','basis','source'))
    order_keys=('id','firm','kind','target','status','created','ack_due','earliest','ack_at','effective_at',
        'created_minutes','ack_due_minutes','earliest_minutes','ack_at_minutes','effective_at_minutes','terminal_reason')
    s['orders']=[_only(o,order_keys) for o in s['orders']]
    s['transit']=[_only(x,('route','firm','volume','due','due_minutes','disposition_order_id')) for x in s['transit']]
    s['initial']=view['balance']['initial'];s['scenario']={'disturbances':[]};s['_physical_capacity']=receiving_status(s['recovery'],s['minute'])['capacity_m3_h']
    s['world_events']=[];s['next_order']=max([int(o['id'][1:]) for o in s['orders']]+[0])+1
    if disposition.enabled(view):s['disposition']=disposition.restore(view['disposition'])
    if production_model.enabled(view):s['production_control']=production_model.restore(view['production_control'])
    if 'receiving_ceiling' in view:s['receiving_ceiling']=_only(view['receiving_ceiling'],('value','valid_until_minutes','basis','source'))
    if 'production_floor' in view:s['production_floor']=_only(view['production_floor'],('rates_m3_h','valid_until_minutes','basis','source'))
    if 'maintenance_plan' in view:s['maintenance_plan']=_only(view['maintenance_plan'],MAINTENANCE_PLAN_KEYS)
    s['history']=[history_entry(s)];s['events']=[]
    return s


def conditional_run(view,rates=None,production=None,horizon=16):
    if isinstance(horizon,bool) or not isinstance(horizon,int) or not 1<=horizon<=256:raise ValueError('horizon must be 1..256 ticks')
    status=receiving_status(view.get('recovery',{}),view['minute'])
    stop=min(view['end_tick'],view['tick']+horizon)*15
    report=view.get('recovery',{}).get('capacity_confirmation') or {}
    reason=None
    if not status['known']:reason=status['reason']
    elif report['valid_until_minutes']<stop:reason='capacity_expires_within_horizon'
    elif profile_status(view)['unknown_fields']:reason='capability_unknown'
    elif not _observation_known(view):reason='observation_unknown'
    if reason:return dict(status='unknown',reason=reason,history=None,totals=None,balance=None,predicted_objective=None)
    s=from_public(view);before=len(s['orders'])
    if rates is not None or production:publish(s,rates or {},production)
    advance(s,horizon)
    return dict(status='conditional_simulation',reason='valid_report_held_constant',history=s['history'],totals=s['totals'],balance=mass_balance(s),
        events=s['events'],orders=s['orders'],notices=len(s['orders'])-before,
        setpoint_magnitude=sum(abs(e['value']-e['previous']) for e in s['events'] if e['kind']=='setpoint'),final_state=public_state(s),
        executable_in_field=False)

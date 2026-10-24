"""Shared advice and competent, equal-rights dynamic-task adapters."""
from copy import deepcopy
from . import engine, production, controllers
from ..wastewater_workbench_v5 import disposition
from ..wastewater_workbench_v3.recovery import receiving_status

ADVERSE = ('uncontained_m3','terminal_all_stored_m3','terminal_return_debt_m3',
    'tail_remaining_m3','unmet_production_units','interrupted_enterprise_minutes',
    'notices','restart_count','restart_flush_m3','storage_occupancy_m3_h','transfer_pump_runtime_minutes')


def observation(view):
    # Both workflows can inspect complete history in the UI. This common decision
    # contract uses current public records only; no opaque future/config fields.
    out=engine.public_state(engine.from_public(view))
    out['history']=[];out['events']=[]
    return out


def rights(view):
    return dict(rate_options={f['id']:engine.action_options(view,f['id']) for f in view['firms']},
        storage_enabled=view['disposition']['enabled'],
        production_profiles=production.restore(view['production_control'])['profiles'])


def execute(sim, bundle):
    # A complete transaction: every component succeeds on a clone before any
    # live mutation. Both adapters use this same physical/admission contract.
    trial=deepcopy(sim)
    engine.publish(trial,bundle['rates'],bundle.get('production',{}))
    for t in bundle['transfers']:
        disposition.request(trial,**t)
    sim.clear();sim.update(trial)


def forecast(view,bundle,horizon=16):
    sim=engine.from_public(view)
    execute(sim,bundle)
    engine.advance(sim,min(horizon,view['end_tick']-view['tick']))
    return engine.public_state(sim)


def deficit_bound(view):
    ceiling=view.get('receiving_ceiling');floor=view.get('production_floor')
    if not ceiling or not floor or ceiling.get('value')!=0:
        return dict(known=False,reason='no_declared_zero_receiving_and_production_floor')
    until=min(ceiling['valid_until_minutes'],floor['valid_until_minutes'])
    hours=max(0,until-view['minute'])/60
    # Generous independent upper bound for direct and return in-transit volume.
    # Doubling route transit and including both pump directions cannot make the
    # lower bound falsely stronger than the declared physical action space.
    transit_bound=2*sum(r['capacity']*r['delay']/4 for r in view['routes'])
    d=view['disposition']
    if d['enabled']:transit_bound+=2*d['pump_rate_m3_h']*d['travel_minutes']/60
    capacity=sum(f['capacity'] for f in view['firms'])+sum(r['buffer'] for r in view['routes'])
    if d['enabled']:capacity+=d['storage_capacity_m3']
    incoming=sum(floor['rates_m3_h'].values())*hours
    lower=max(0,view['balance']['stored']+incoming-capacity-transit_bound)
    return dict(known=True,until_minutes=until,lower_bound_uncontained_m3=lower,
        uncontrollable_input_floor_m3=incoming,contained_upper_bound_m3=capacity+transit_bound,
        scope='Only the explicitly declared zero-receiving/fixed-production-floor task; not global industrial infeasibility.')


def recommend(public_view):
    view=observation(public_view)
    common=controllers.recommend(view,'simple')
    if not receiving_status(view.get('recovery',{}),view['minute'])['known']:
        return dict(bundle=dict(rates={},transfers=[],production={}),
            status='needs_confirmation',method='shared_strong_simple',
            reasons=common.get('reasons',[])+['receiving_unconfirmed'],
            deficit_bound=deficit_bound(view),predicted_uncontained_m3=None,
            label='Receiving capacity unknown; no quantitative forecast or new action advice.')
    bundle=dict(rates=common.get('rates',{}),transfers=common.get('transfers',[]),production={})
    reasons=[];prediction=None
    try:
        prediction=forecast(view,bundle)
    except ValueError as exc:
        reasons.append(str(exc))
    profiles=view.get('production_control',{}).get('profiles',{})
    for fid in sorted(profiles):
        firm=next(f for f in view['firms'] if f['id']==fid)
        pending=[o for o in view['orders'] if o['kind']=='production' and o['firm']==fid and o['status'] in production.PENDING]
        if pending:continue
        target=not firm['production_enabled']
        gate=production.admission(view,fid,target)
        if gate:
            reasons.extend(gate);continue
        if target:
            candidate=deepcopy(bundle);candidate['production'][fid]=True
            try:
                resumed=forecast(view,candidate)
                if resumed['totals']['uncontained']<=view['totals']['uncontained']+1e-6:
                    bundle=candidate;prediction=resumed
            except ValueError as exc:reasons.append(str(exc))
        elif prediction and prediction['totals']['uncontained']>view['totals']['uncontained']+1e-6:
            # Request one eligible source deferral, without global search or a
            # free shutdown assumption. Its output debt is reported explicitly.
            candidate=deepcopy(bundle);candidate['production'][fid]=False
            try:
                paused=forecast(view,candidate)
                if paused['totals']['uncontained']<prediction['totals']['uncontained']-1e-6:
                    bundle=candidate;prediction=paused
            except ValueError as exc:reasons.append(str(exc))
    bound=deficit_bound(view)
    return dict(bundle=bundle,status='conditional_simulation',method='shared_strong_simple',
        reasons=sorted(set(reasons)),deficit_bound=bound,
        predicted_uncontained_m3=None if prediction is None else prediction['totals']['uncontained']-view['totals']['uncontained'],
        label='Same bounded 4h advice for both workflows; production debt/tail/clearance retained. Not an optimal or field-safe plan.')


def materialize(mode,view,advice):
    if mode not in ('status_table','linked_workbench'):
        raise ValueError('unknown workflow')
    bundle=advice['bundle']
    if mode=='linked_workbench':
        return deepcopy(bundle)
    # Competent status table supports bulk transfer of recommended rows. Do not
    # force transcription errors, extra approvals or artificially slow entry.
    rows=[('rate',fid,value) for fid,value in bundle['rates'].items()]
    rows += [('production',fid,value) for fid,value in bundle['production'].items()]
    out=dict(rates={},production={},transfers=deepcopy(bundle['transfers']))
    for kind,fid,value in rows:
        out['rates' if kind=='rate' else 'production'][fid]=value
    return out


def metrics(sim):
    view=engine.public_state(sim);d=view['disposition'];p=view['production_control']
    return dict(uncontained_m3=view['totals']['uncontained'],generated_m3=view['totals']['generated'],
        received_m3=view['totals']['received'],terminal_all_stored_m3=view['balance']['stored'],
        terminal_return_debt_m3=sum(d['inventory'].values())+sum(x['volume'] for x in d['storage_transit'])+
            sum(x['volume'] for x in view['transit'] if x.get('disposition_order_id')),
        tail_remaining_m3=p['tail_remaining_m3'],**deepcopy(p['metrics']),
        notices=len(view['orders'])+len(d['orders']),
        storage_occupancy_m3_h=d['metrics']['occupancy_m3_minutes']/60,
        transfer_pump_runtime_minutes=d['metrics']['pump_runtime_minutes'],
        open_production_orders=sum(o['kind']=='production' and o['status'] in production.PENDING for o in view['orders']),
        production_refusals=sum(e['kind']=='production_refused' for e in view['events']),
        balance_error=view['balance']['error'])


def adverse(reference,candidate):
    return {k:candidate[k]-reference[k] for k in ADVERSE if k in reference and k in candidate and candidate[k]-reference[k]>1e-6}

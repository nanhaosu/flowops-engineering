"""Assumed owner-confirmed manufacturing deferral; never a water-saving model."""
from copy import deepcopy
from ..wastewater_workbench_v3.capabilities import finite
from ..wastewater_workbench_v3.recovery import receiving_status
from ..wastewater_workbench_v5 import disposition

PROFILE = ('basis','source','owner','authorized','authorized_until_minutes',
    'authority_revision','ack_minutes','stop_lead_minutes','restart_lead_minutes',
    'batch_minutes','min_pause_minutes','residual_minutes','residual_rate_m3_h',
    'restart_flush_m3','planned_units_per_hour','restart_ready',
    'restart_valid_until_minutes','clearance_revision')
STATE = ('paused_at_minutes','tail_until_minutes','tail_rate_m3_h')
METRICS = ('interrupted_enterprise_minutes','planned_units','completed_units',
    'unmet_production_units','tail_generated_m3','restart_flush_m3','restart_count')
PENDING = ('notified','acknowledged')


def enabled(s):
    return isinstance(s.get('production_control'), dict)


def select(record, keys):
    return {k:deepcopy(record[k]) for k in keys if k in record}


def initialize(s, cfg):
    if cfg is None:
        return
    if not isinstance(cfg,dict) or not isinstance(cfg.get('profiles'),dict):
        raise ValueError('production profiles must be an object')
    profiles = {}
    for fid, raw in cfg['profiles'].items():
        if fid not in s['firms'] or not isinstance(raw,dict):
            raise ValueError('invalid production firm/profile')
        p = select(raw, PROFILE)
        for key in PROFILE:
            p.setdefault(key,None)
        if p['basis'] not in ('assumed','verified') or not isinstance(p['source'],str) or not p['source'].strip():
            raise ValueError('production profile provenance required')
        if not isinstance(p['owner'],str) or not p['owner'].strip():
            raise ValueError('production owner required')
        for key in ('authorized','restart_ready'):
            if p[key] is not None and not isinstance(p[key],bool):
                raise ValueError('invalid '+key)
        for key in PROFILE:
            if key in ('basis','source','owner','authorized','restart_ready') or p[key] is None:
                continue
            if not finite(p[key]) or not 0<=p[key]<=100000:
                raise ValueError('invalid production '+key)
            if key.endswith('minutes') and p[key]%5:
                raise ValueError(key+' must use 5-minute grid')
        if p['batch_minutes'] == 0:
            raise ValueError('production batch must be positive')
        if not s['firms'][fid]['production_enabled']:
            raise ValueError('initial paused production requires observed residual/pause state; unsupported configuration')
        if not finite(p['planned_units_per_hour']):
            raise ValueError('known manufacturing rate required for cost accounting')
        profiles[fid] = p
    s['production_control'] = dict(profiles=profiles,
        states={fid:dict(paused_at_minutes=None,tail_until_minutes=0,tail_rate_m3_h=0.) for fid in profiles},
        metrics={k:0. for k in METRICS})


def firms(s):
    values = s['firms']
    return values if isinstance(values,dict) else {f['id']:f for f in values}


def admission(s, fid, target, execution=False):
    if not enabled(s):
        return []
    prefix = 'production:'+str(fid)+':'
    p = s['production_control']['profiles'].get(fid)
    if p is None:
        return [prefix+'no_action_right']
    now = s['minute']; f = firms(s)[fid]
    reasons = []
    if p.get('authorized') is not True:
        reasons.append('authority_unknown_or_denied')
    expiry = p.get('authorized_until_minutes')
    if not finite(expiry) or expiry<=now:
        reasons.append('authority_expired')
    required = ('ack_minutes','batch_minutes','planned_units_per_hour',
        'restart_lead_minutes','min_pause_minutes','restart_flush_m3') if target else (
        'ack_minutes','batch_minutes','stop_lead_minutes','residual_minutes','residual_rate_m3_h','planned_units_per_hour')
    for key in required:
        if not finite(p.get(key)):
            reasons.append('unknown_'+key)
    if target:
        state = s['production_control']['states'][fid]
        paused = state['paused_at_minutes']
        if paused is not None and finite(p.get('min_pause_minutes')) and now<paused+p['min_pause_minutes']:
            reasons.append('minimum_pause')
        if now<state['tail_until_minutes']:
            reasons.append('residual_process_not_complete')
        if p.get('restart_ready') is not True:
            reasons.append('restart_clearance_unknown_or_denied')
        expiry = p.get('restart_valid_until_minutes')
        if not finite(expiry) or expiry<=now:
            reasons.append('restart_clearance_expired')
        receiving = receiving_status(s.get('recovery',{}),now)
        if not receiving['known'] or receiving['capacity_m3_h']<=0:
            reasons.append('receiving_unconfirmed_or_zero')
        if disposition.acceptance_reasons(s,'plant',fid):
            reasons.append('plant_class_acceptance')
        if finite(p.get('restart_flush_m3')) and f['capacity']-f['stock']+1e-7<p['restart_flush_m3']:
            reasons.append('restart_flush_space')
    if not execution:
        pending=[o for o in s.get('orders',[]) if o['firm']==fid and o['kind']=='production' and o['status'] in PENDING]
        if pending and pending[-1]['target'] != target:
            reasons.append('opposite_pending_order')
    return [prefix+r for r in reasons]


def timing(s, fid, target, ack_at=None):
    p=s['production_control']['profiles'][fid]
    due=s['minute']+p['ack_minutes'] if ack_at is None else ack_at
    lead=p['restart_lead_minutes'] if target else p['stop_lead_minutes']
    earliest=due+lead
    batch=p['batch_minutes']
    return due, ((earliest+batch-1)//batch)*batch


def update(s, fid, record):
    if not enabled(s) or fid not in s['production_control']['profiles'] or not isinstance(record,dict):
        raise ValueError('unknown production confirmation')
    old=s['production_control']['profiles'][fid]
    if record.get('owner')!=old['owner']:
        raise ValueError('production owner mismatch')
    allowed={'owner','authorized','authorized_until_minutes','authority_revision',
        'restart_ready','restart_valid_until_minutes','clearance_revision'}
    if set(record)-allowed:
        raise ValueError('unsupported production confirmation field')
    new=deepcopy(old)
    for flag,until,revision in (('authorized','authorized_until_minutes','authority_revision'),
                              ('restart_ready','restart_valid_until_minutes','clearance_revision')):
        if flag not in record:
            if until in record or revision in record:
                raise ValueError('incomplete production confirmation')
            continue
        if record[flag] is not None and not isinstance(record[flag],bool):
            raise ValueError('production confirmation must be bool or unknown')
        if not finite(record.get(until)) or record[until]<=s['minute']:
            raise ValueError('production confirmation expiry invalid')
        rev=record.get(revision)
        if not finite(rev) or int(rev)!=rev or rev<=old[revision]:
            raise ValueError('stale production confirmation')
        new.update({flag:record[flag],until:record[until],revision:rev})
    # A permission gap permanently invalidates the old instruction. Renewal is
    # evidence for a NEW explicit request, never retroactive approval of one
    # that passed through denial/expiry while waiting for its batch boundary.
    for order in s.get('orders',[]):
        if order['firm']!=fid or order['kind']!='production' or order['status'] not in PENDING:
            continue
        reasons=[]
        checks=[('authorized','authorized_until_minutes','authority')]
        if order['target']:
            checks.append(('restart_ready','restart_valid_until_minutes','restart_clearance'))
        for flag,until,label in checks:
            if old.get(flag) is not True or new.get(flag) is not True:
                reasons.append(label+'_unknown_or_denied')
            if not finite(old.get(until)) or old[until]<=s['minute'] or not finite(new.get(until)) or new[until]<=s['minute']:
                reasons.append(label+'_expired')
        if reasons:
            reason=';'.join('production:'+fid+':'+r for r in reasons)
            order.update(status='rejected',terminal_reason=reason)
            s['events'].append(dict(tick=s['minute']/15,minute=s['minute'],
                kind='production_refused',reason=reason,firm=fid,order_id=order['id']))
    s['production_control']['profiles'][fid]=new


def execute(s, order, logger):
    fid=order['firm'];target=order['target'];now=s['minute']
    reasons=admission(s,fid,target,execution=True)
    if reasons:
        order.update(status='rejected',terminal_reason=';'.join(reasons))
        logger(s,'production_refused',order['terminal_reason'],firm=fid,order_id=order['id'])
        return False
    p=s['production_control']['profiles'][fid]
    if now%p['batch_minutes']:
        return False
    f=s['firms'][fid];state=s['production_control']['states'][fid]
    if not target:
        state.update(paused_at_minutes=now,tail_until_minutes=now+p['residual_minutes'],
                     tail_rate_m3_h=p['residual_rate_m3_h'])
    else:
        flush=p['restart_flush_m3']
        f['stock']+=flush;s['totals']['generated']+=flush
        s['production_control']['metrics']['restart_flush_m3']+=flush
        s['production_control']['metrics']['restart_count']+=1
        state.update(paused_at_minutes=None,tail_until_minutes=0,tail_rate_m3_h=0.)
        logger(s,'production_restart_water','assumed_cleaning_volume',firm=fid,volume=flush)
    f['production_enabled']=target
    logger(s,'production_control','owner_confirmed_batch_boundary',firm=fid,value=target,order_id=order['id'])
    return True


def cell(s, f, potential):
    fid=f['id'];now=s['minute']
    if not enabled(s) or fid not in s['production_control']['profiles']:
        return potential if f['production_enabled'] else 0.
    p=s['production_control']['profiles'][fid];st=s['production_control']['states'][fid]
    metrics=s['production_control']['metrics']
    units=p.get('planned_units_per_hour')
    scheduled = (units*f['production_multiplier']*5/60 if finite(units) and now<s['production_end']*15 else 0.)
    metrics['planned_units']+=scheduled
    if f['production_enabled']:
        metrics['completed_units']+=scheduled
        return potential
    metrics['interrupted_enterprise_minutes']+=5
    metrics['unmet_production_units']+=scheduled
    duration=max(0,min(5,st['tail_until_minutes']-now))
    tail=st['tail_rate_m3_h']*duration/60
    metrics['tail_generated_m3']+=tail
    return tail


def restore(record):
    return dict(profiles={fid:select(p,PROFILE) for fid,p in record['profiles'].items()},
        states={fid:select(p,STATE) for fid,p in record['states'].items()},
        metrics=select(record['metrics'],METRICS))


def public(s):
    out=restore(s['production_control'])
    out['tail_remaining_m3']=sum(max(0,st['tail_until_minutes']-s['minute'])*st['tail_rate_m3_h']/60 for st in out['states'].values())
    out['claim_boundary']='Synthetic production units, owner roles and tail curves; no money, water savings or field control claim.'
    return out

"""Frozen 48h resource/strategy attribution; preserves every outcome and trace.

python -m xylem.wastewater_workbench_v5.comparison --output DIR
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
from time import perf_counter
from . import engine,disposition,controllers,planning
from .scenarios import configuration,matrix_cases

HERE=Path(__file__).resolve().parent
ARMS={'A':dict(resource_enabled=False,method='simple'),
      'B':dict(resource_enabled=True,method='simple'),
      'C':dict(resource_enabled=False,method='coordinated'),
      'D':dict(resource_enabled=True,method='coordinated')}
EPS=1e-6


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)


def source_hashes():
    paths=[HERE/name for name in ('engine.py','disposition.py','planning.py','controllers.py','scenarios.py','comparison.py')]
    paths += [HERE.parent/'wastewater_workbench_v3'/name for name in ('capabilities.py','recovery.py','restart_guard.py','scenarios.py')]
    return {str(p.relative_to(HERE.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def freeze(output):
    output=Path(output)
    if (output/'protocol.json').exists():
        saved=json.loads((output/'protocol.json').read_text(encoding='utf-8'))
        if saved['source_sha256']!=source_hashes():raise RuntimeError('frozen sources changed; preserve results and use a new revision')
        if saved['protocol_sha256']!=digest(saved['protocol']):raise RuntimeError('protocol hash mismatch')
        return saved
    protocol=dict(schema=5,evaluation_revision=output.name,cases=matrix_cases(),arms=ARMS,end_minutes=2880,
        observations_minutes=15,physics_minutes=5,decision_minutes=60,forecast_minutes=240,
        configurations={r['case_id']+'/'+r['variant']:configuration(r['case_id'],True,r['variant']) for r in matrix_cases()},
        baseline='Same v3 strong finite simple rate selector in every arm; urgency forecast storage or oldest debt return; its sole selected transfer is compared with wait using the same complete objective, and omitted without predicted improvement.',
        coordinated='Same direct rates; evaluate simple transfer, wait, each feasible one-pump-hour store/return under identical 4h executor.',
        objective=['forecast new uncontained','forecast all stored volume','forecast extra return debt','created notices'],
        information='Public allowlisted state only; private future disturbance schedule never enters the selector; decisions before same-cell events.',
        fairness='A vs B and C vs D change only extra-storage availability. A/C acceptance metadata and guards remain identical. B/D same physical resources and opportunities.',
        effects={'resource':'B minus A','strategy':'D minus B','no_resource_invariance':'C minus A'},
        negative_policy='Retain every primary or secondary increase; r1 preserved as diagnosed weak-baseline comparison, r2 adds the predeclared one-candidate versus wait guard to both transfer directions; no numerical tuning or outcome-based scenario changes; no need for coordinated method to win.',
        heldout_policy='Originally predeclared numeric combinations are reused after correctness fixes. No parameter tuning; these are sensitivity cases, not blind tests or independent sites.',
        correctness_fix_notes=['r3 restores disposition_order_id through planner public transit allowlist so returned-water arrival and return debt remain correct.',
            'r3 applies the common simulated plant-acceptance gate at physical restart after dry/window stops, not only at new target admission. Existing running observed flow remains; field interlock capability is unverified.'],
        limitation='All numeric data synthetic. Receiving is not treatment. No pollution-removal, compliance, monetary, human-efficiency or field-benefit claim.')
    frozen=dict(frozen_at_utc=datetime.now(timezone.utc).isoformat(),source_sha256=source_hashes(),protocol_sha256=digest(protocol),protocol=protocol)
    write_json(output/'protocol.json',frozen)
    return frozen


def compact(view):
    d=view.get('disposition',{})
    return dict(minute=view['minute'],balance=deepcopy(view['balance']),totals=deepcopy(view['totals']),
        firm_stocks={f['id']:f['stock'] for f in view['firms']},
        firm_production={f['id']:f['production_rate']*f['production_multiplier'] for f in view['firms']},
        route_stocks={r['id']:r['stock'] for r in view['routes']},
        transit=sum(p['volume'] for p in view['transit']),
        storage_m3=d.get('in_storage_m3',0.),transfer_m3=d.get('in_transfer_m3',0.),
        notice_budget=deepcopy(view['notice_budget']))


def terminal_metrics(sim,states,decisions):
    v=engine.public_state(sim);d=v.get('disposition',{});dm=d.get('metrics',{})
    orders=v['orders']+d.get('orders',[])
    statuses=Counter(c.get('status','unknown') for c in v.get('restart_commitments',[]))
    area=sum((a['balance']['stored']+b['balance']['stored'])*.5*(b['minute']-a['minute'])/60 for a,b in zip(states,states[1:]))
    return_transit=sum(x['volume'] for x in v['transit'] if x.get('disposition_order_id'))
    return dict(uncontained_m3=v['totals']['uncontained'],received_m3=v['totals']['received'],
        generated_m3=v['totals']['generated'],terminal_all_stored_m3=v['balance']['stored'],
        terminal_storage_m3=d.get('in_storage_m3',0.),terminal_storage_transit_m3=d.get('in_transfer_m3',0.),
        terminal_route_transit_m3=sum(x['volume'] for x in v['transit']),
        terminal_return_transit_m3=return_transit,
        terminal_return_debt_m3=d.get('in_storage_m3',0.)+d.get('in_transfer_m3',0.)+return_transit,
        pending_transfer_m3=d.get('pending_volume_m3',0.),open_transfer_orders=d.get('open_orders',0),
        stored_to_resource_m3=dm.get('arrived_storage_m3',0.),returned_to_route_m3=dm.get('arrived_return_route_m3',0.),
        notices=len(orders),rate_notices=len(v['orders']),transfer_notices=len(d.get('orders',[])),
        storage_occupancy_m3_h=dm.get('occupancy_m3_minutes',0)/60,
        total_inventory_m3_h=area,transfer_pump_runtime_minutes=dm.get('pump_runtime_minutes',0),
        blocked_transfer_cells=dm.get('blocked_departure_cells',0),
        restart_failed=sum(n for name,n in statuses.items() if name in ('failed','late','missed')),
        refused_decisions=sum(not r['admitted'] for r in decisions),
        unknown_decisions=sum(r['action']['status']=='needs_confirmation' for r in decisions),
        max_balance_error=max([abs(r['balance']['error']) for r in states]+[abs(v['balance']['error'])]),
        compute_ms=sum(r['compute_ms'] for r in decisions))


def run_controller(config,method):
    sim=engine.new_simulation(config);states=[compact(engine.public_state(sim))];decisions=[]
    while sim['tick']<sim['end_tick']:
        if sim['tick']%4==0:
            view=planning._public_copy(engine.public_state(sim))
            started=perf_counter();action=controllers.recommend(view,method);elapsed=(perf_counter()-started)*1000
            if action.get('production'):raise AssertionError('production must remain exogenous and matched')
            clone=deepcopy(sim);admitted=True;reason=''
            try:
                if action['rates']:engine.publish(clone,action['rates'])
                for transfer in action['transfers']:disposition.request(clone,**transfer)
            except ValueError as err:
                admitted=False;reason=str(err)
            if admitted:sim=clone
            decisions.append(dict(minute=sim['minute'],input=view,input_sha256=digest(view),
                action=action,admitted=admitted,reason=reason,compute_ms=elapsed))
        engine.advance(sim,1)
        states.append(compact(engine.public_state(sim)))
    metrics=terminal_metrics(sim,states,decisions)
    if metrics['max_balance_error']>EPS:raise AssertionError('water balance violation')
    return dict(configuration_sha256=digest(config),method=method,states=states,decisions=decisions,
        metrics=metrics,final=engine.public_state(sim),world_events=sim['world_events'])


def attribute(rows):
    groups={}
    for row in rows:groups.setdefault((row['case_id'],row['variant']),{})[row['arm']]=row['metrics']
    result=[]
    for (case,variant),arms in groups.items():
        if set(arms)!=set(ARMS):raise ValueError('attribution needs all four matched arms')
        delta=lambda newer,older:{k:arms[newer][k]-arms[older][k] for k in arms[older] if isinstance(arms[older][k],(int,float))}
        result.append(dict(case_id=case,variant=variant,resource_effect_B_minus_A=delta('B','A'),
            strategy_effect_D_minus_B=delta('D','B'),no_resource_strategy_C_minus_A=delta('C','A')))
    return result


def report(rows,pairs):
    lines=['v5 有限暂存与恢复回送：48小时冻结对照','全部数值为合成；接纳量不是达标处理量。','',
        'A 既有资源+强简单；B 新增暂存+强简单；C 既有资源+有限协同；D 新增暂存+有限协同。',
        '资源收益=B-A；策略收益=D-B。负数对未容纳、库存、通知、占用量代表较低，不能混成货币收益。','',
        '案例 / 参数 | A未容纳 | B未容纳 | D未容纳 | 资源差 | 策略差 | B/D末端回送责任']
    for pair in pairs:
        rr={r['arm']:r['metrics'] for r in rows if r['case_id']==pair['case_id'] and r['variant']==pair['variant']}
        lines.append(f"{pair['case_id']} / {pair['variant']} | {rr['A']['uncontained_m3']:.3f} | {rr['B']['uncontained_m3']:.3f} | {rr['D']['uncontained_m3']:.3f} | {pair['resource_effect_B_minus_A']['uncontained_m3']:+.3f} | {pair['strategy_effect_D_minus_B']['uncontained_m3']:+.3f} | {rr['B']['terminal_return_debt_m3']:.3f}/{rr['D']['terminal_return_debt_m3']:.3f}")
    lines += ['', '负结果包含资源新增后因通知/转移过程而变差，以及有限协同相对强简单的退步。详细逐项见NEGATIVE_RESULTS.json。',
        '末端库存、转移在途和未履行命令完整保留；持续产水48h，末端非零库存不等同全部失败，也不应抹去。',
        '真人操作收益尚未验证；本轮只是物理流程/资源与策略分离的合成验证。',
        '公开依据和假设映射见../evidence/README.txt。源码与协议先冻结，运行后不按结果调参。']
    return '\n'.join(lines)+'\n'


def run(output=HERE/'artifacts/comparison/v5_r1'):
    output=Path(output);frozen=freeze(output)
    if (output/'results.json').exists():raise FileExistsError('completed result preserved; choose new directory')
    rows=[];initials={};production={};unknown_violations=[]
    for case in frozen['protocol']['cases']:
        key=case['case_id']+'/'+case['variant']
        for arm,assignment in ARMS.items():
            if source_hashes()!=frozen['source_sha256']:raise RuntimeError('source changed during frozen run')
            cfg=deepcopy(frozen['protocol']['configurations'][key]);cfg['disposition']['enabled']=assignment['resource_enabled']
            trace=run_controller(cfg,assignment['method'])
            file=f"{case['case_id']}--{case['variant']}--{arm}.json.gz"
            target=output/'traces'/file;target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as f:
                f.write(gzip.compress(canonical(trace),mtime=0))
            rows.append(dict(**case,arm=arm,**assignment,metrics=trace['metrics'],trace='traces/'+file))
            initial=trace['states'][0]
            physical={k:initial[k] for k in ('balance','firm_stocks','firm_production','route_stocks','transit')}
            initials.setdefault(key,physical)
            if initials[key]!=physical:raise AssertionError('unmatched starting water')
            observed=[(s['minute'],s['firm_production'],s['totals']['generated']) for s in trace['states']]
            production.setdefault(key,observed)
            if production[key]!=observed:raise AssertionError('unmatched exogenous production')
            if case['case_id']=='unknown_compatibility' and trace['metrics']['transfer_notices']:
                unknown_violations.append(dict(case=key,arm=arm))
            print(f"completed {len(rows)}/40 {key} {arm}",flush=True)
    pairs=attribute(rows)
    for pair in pairs:
        for key,value in pair['no_resource_strategy_C_minus_A'].items():
            if key!='compute_ms' and abs(value)>EPS:raise AssertionError('resource absent strategy changed behavior: '+key)
    if unknown_violations:raise AssertionError('unknown compatibility permitted transfer')
    adverse=('uncontained_m3','terminal_all_stored_m3','terminal_return_debt_m3','notices',
             'storage_occupancy_m3_h','transfer_pump_runtime_minutes','blocked_transfer_cells','restart_failed')
    negatives=[dict(case_id=p['case_id'],variant=p['variant'],comparison=effect,
                    increased={k:p[effect][k] for k in adverse if p[effect][k]>EPS})
               for p in pairs for effect in ('resource_effect_B_minus_A','strategy_effect_D_minus_B')
               if any(p[effect][k]>EPS for k in adverse)]
    result=dict(schema=5,protocol_sha256=frozen['protocol_sha256'],rows=rows,pairs=pairs,
        limitations=frozen['protocol']['limitation'])
    write_json(output/'results.json',result);write_json(output/'NEGATIVE_RESULTS.json',negatives)
    write_json(output/'audit.json',dict(runs=len(rows),complete_48h=True,matched_inputs=len(initials),
        matched_production=True,no_resource_strategy_invariant=True,unknown_transfers=0,
        max_balance_error=max(r['metrics']['max_balance_error'] for r in rows),
        source_sha256=source_hashes()))
    (output/'REPORT.txt').write_text(report(rows,pairs),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=HERE/'artifacts/comparison/v5_r1')
    args=parser.parse_args();run(args.output)

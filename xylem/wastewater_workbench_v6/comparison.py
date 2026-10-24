"""Frozen same-advice dynamic-task comparison; no fabricated human benefit."""
import argparse
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import zipfile
from . import engine, scenarios, tasks

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent.parent


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def freeze(out):
    if (out/'results.json').exists() or (out/'protocol.json').exists():
        raise ValueError('Refusing to overwrite frozen comparison; use a new directory')
    sources=[HERE/n for n in ('comparison.py','tasks.py','engine.py','production.py','scenarios.py','planning.py','controllers.py')]
    sources += [ROOT/'xylem'/n for n in ('wastewater_workbench_v5/disposition.py',
        'wastewater_workbench_v5/scenarios.py','wastewater_workbench_v3/scenarios.py',
        'wastewater_workbench_v3/capabilities.py','wastewater_workbench_v3/recovery.py',
        'wastewater_workbench_v3/restart_guard.py','wastewater_workbench/artifacts/review_v2/comparison_protocol.json')]
    configs=[dict(case_id=c['id'],variant=v,configuration=scenarios.configuration(c['id'],True,v))
        for c in scenarios.catalog() for v in scenarios.VARIANTS]
    protocol=dict(schema=6,scope='12 related synthetic tasks/profile combinations, 48h; no field/human outcome claim',
        workflows=['status_table','linked_workbench'],diagnostic='keep_producing: same rights and shared advice, intentionally declines production deferral; not the product baseline',
        decision_minutes=60,observation_minutes=15,simultaneous_information=True,
        advice='tasks.recommend, shared strong-simple 4h public-only advice',
        baseline='competent status table supports bulk advice; no forced omissions, typing errors or extra approval burden',
        user_time='unmeasured; participants=0; no authored action count treated as human saving',
        stop_rule='retain all ties, adverse operational/manufacturing costs and unresolved tasks; no parameter tuning after freeze',
        configurations=configs,
        source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    write(out/'protocol.json',protocol)
    with zipfile.ZipFile(out/'source_snapshot.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for p in sources:archive.write(p,p.relative_to(ROOT).as_posix())
    return protocol


def state_record(sim):
    view=engine.public_state(sim)
    view['history']=[];view['events']=[]
    return view


def run_group(spec,out):
    modes=('status_table','linked_workbench','keep_producing')
    sims={mode:engine.new_simulation(spec['configuration']) for mode in modes}
    traces={mode:dict(observations=[state_record(sim)],decisions=[]) for mode,sim in sims.items()}
    for tick in range(192):
        if tick%4==0:
            av=tasks.observation(engine.public_state(sims['status_table']))
            bv=tasks.observation(engine.public_state(sims['linked_workbench']))
            if av!=bv:raise AssertionError('presentation workflows diverged before decision')
            common=tasks.recommend(av)
            for mode,sim in sims.items():
                view=tasks.observation(engine.public_state(sim))
                advice=common if mode!='keep_producing' else tasks.recommend(view)
                bundle=tasks.materialize('status_table' if mode=='status_table' else 'linked_workbench',view,advice)
                if mode=='keep_producing':bundle['production']={}
                decision=dict(minute=sim['minute'],public_input=view,input_sha256=digest(view),
                    advice=advice,advice_sha256=digest(advice),rights_sha256=digest(tasks.rights(view)),bundle=bundle)
                try:
                    tasks.execute(sim,bundle);decision['result']='published_or_unchanged'
                except ValueError as exc:
                    decision.update(result='refused',reason=str(exc))
                    engine.log(sim,'plan_refused',str(exc))
                traces[mode]['decisions'].append(decision)
        for mode,sim in sims.items():
            engine.advance(sim)
            traces[mode]['observations'].append(state_record(sim))
    rows=[]
    for mode,sim in sims.items():
        trace=traces[mode];trace['events']=deepcopy(sim['events']);trace['metrics']=tasks.metrics(sim)
        name=spec['case_id']+'--'+spec['variant']+'--'+mode+'.json.gz'
        with gzip.open(out/'traces'/name,'wt',encoding='utf-8') as stream:
            json.dump(trace,stream,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        rows.append(dict(case_id=spec['case_id'],variant=spec['variant'],workflow=mode,
            metrics=trace['metrics'],trace='traces/'+name,final_state_sha256=digest(state_record(sim)),
            max_balance_error=max(abs(v['balance']['error']) for v in trace['observations']),
            refused_plan_transactions=sum(d['result']=='refused' for d in trace['decisions'])))
    for da,db in zip(traces['status_table']['decisions'],traces['linked_workbench']['decisions']):
        if any(da[k]!=db[k] for k in ('input_sha256','advice_sha256','rights_sha256','bundle','result')):
            raise AssertionError('unfair decision transaction')
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=HERE/'artifacts/comparison/v6_r1')
    args=parser.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    protocol=freeze(out);(out/'traces').mkdir()
    rows=[];pairs=[];negative=[]
    for spec in protocol['configurations']:
        group=run_group(spec,out);rows+=group
        by={r['workflow']:r for r in group}
        a,b,c=(by[k] for k in ('status_table','linked_workbench','keep_producing'))
        delta={k:b['metrics'][k]-a['metrics'][k] for k in a['metrics']}
        diagnostic={k:b['metrics'][k]-c['metrics'][k] for k in c['metrics']}
        pairs.append(dict(case_id=spec['case_id'],variant=spec['variant'],
            workflow_delta_B_minus_A=delta,physical_parity=a['final_state_sha256']==b['final_state_sha256'],
            action_diagnostic_B_minus_keep_producing=diagnostic))
        for label,reference,candidate in (('workflow_B_minus_A',a,b),('deferral_action_minus_keep_producing',c,b)):
            worse=tasks.adverse(reference['metrics'],candidate['metrics'])
            if worse:negative.append(dict(case_id=spec['case_id'],variant=spec['variant'],comparison=label,increased=worse))
        print(spec['case_id'],spec['variant'],'A/B uncontained',round(a['metrics']['uncontained_m3'],3),
              round(b['metrics']['uncontained_m3'],3),'unmet units',round(b['metrics']['unmet_production_units'],3),flush=True)
    results=dict(schema=6,protocol_sha256=digest(protocol),rows=rows,pairs=pairs,negative_results=negative,
        summary=dict(trajectories=len(rows),paired_tasks=len(pairs),physical_ties=sum(p['physical_parity'] for p in pairs),
            participants=0,human_time_saving=None,human_error_reduction=None,
            max_balance_error=max(r['max_balance_error'] for r in rows)),
        limitations=['One assumed device/production profile family, not independent sites.',
            'A/B share policy and action rights; ties establish execution parity, not platform or algorithm superiority.',
            'The keep-producing diagnostic changes chosen actions; its water reduction includes output loss and is not water saved.',
            'Author replay does not establish human usability, labour saving, field benefit or chemical compliance.'],
        source_directory=out.relative_to(HERE).as_posix() if out.is_relative_to(HERE) else str(out))
    write(out/'results.json',results)
    current={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in protocol['source_sha256']}
    if current!=protocol['source_sha256']:raise AssertionError('computation source changed during frozen run')
    write(out/'RUN_AUDIT.json',dict(status='pass',completed_rows=len(rows),all_finish_at_minutes=2880,
        protocol_sha256=digest(protocol),source_unchanged=True,all_pair_input_advice_rights_and_bundles_identical=True,
        all_metrics_finite=True,max_balance_error=results['summary']['max_balance_error']))
    write(HERE/'artifacts/comparison/results.json',results)
    print('Completed',len(rows),'trajectories;',len(pairs),'paired tasks; physical ties',results['summary']['physical_ties'],flush=True)


if __name__=='__main__':main()

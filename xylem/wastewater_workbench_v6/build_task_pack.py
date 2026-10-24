"""Derive task materials and independent saved-trace checks without new runs."""
import csv
import gzip
import hashlib
import json
from pathlib import Path
from . import comparison, tasks
from .service import cost_preview

HERE=Path(__file__).resolve().parent


def source_path(value):
    path=Path(value)
    if path.is_absolute():return path
    local=HERE/path
    return local if local.is_dir() else comparison.ROOT/path


def main():
    index=json.loads((HERE/'artifacts/comparison/results.json').read_text(encoding='utf-8'))
    source=source_path(index['source_directory']);target=HERE/'artifacts/tasks';target.mkdir(parents=True,exist_ok=True)
    protocol=json.loads((source/'protocol.json').read_text(encoding='utf-8'))
    traces={};checks=[];summaries=[]
    for row in index['rows']:
        with gzip.open(source/row['trace'],'rt',encoding='utf-8') as stream:trace=json.load(stream)
        key=(row['case_id'],row['variant'],row['workflow']);traces[key]=trace
        observations=trace['observations'];final=observations[-1];m=row['metrics']
        assert len(observations)==193 and final['minute']==2880
        assert all(abs(s['balance']['error'])<1e-6 for s in observations)
        assert abs(m['unmet_production_units']-(m['planned_units']-m['completed_units']))<1e-6
        contained=sum(f['stock'] for f in final['firms'])+sum(r['stock'] for r in final['routes'])
        contained+=sum(p['volume'] for p in final['transit'])+sum(final['disposition']['inventory'].values())
        contained+=sum(p['volume'] for p in final['disposition']['storage_transit'])
        assert abs(contained-m['terminal_all_stored_m3'])<1e-6
        noncontrolled=sum(sum(f['production_rate']*f['production_multiplier']*.25 for f in s['firms'] if f['id']!='B') for s in observations[1:])
        base_b=next(f for f in final['firms'] if f['id']=='B')['production_rate']
        units=final['production_control']['profiles']['B']['planned_units_per_hour']
        generated=noncontrolled+m['completed_units']*base_b/units+m['tail_generated_m3']+m['restart_flush_m3']
        assert abs(generated-m['generated_m3'])<1e-6,(key,generated,m['generated_m3'])
        if row['variant']=='unknown_tail':
            assert m['interrupted_enterprise_minutes']==0 and m['unmet_production_units']==0
        if row['case_id']=='unresolvable':
            bound=tasks.deficit_bound(observations[0])
            actual=next(s for s in observations if s['minute']==1440)['totals']['uncontained']
            assert bound['known'] and actual+1e-6>=bound['lower_bound_uncontained_m3']>0
        checks.append(dict(case_id=key[0],variant=key[1],workflow=key[2],passed=True,
            generated_recomputed_m3=generated,contained_recomputed_m3=contained,
            trace_sha256=hashlib.sha256((source/row['trace']).read_bytes()).hexdigest()))
    negative=[]
    for pair in index['pairs']:
        case,variant=pair['case_id'],pair['variant']
        a,b,c=(traces[(case,variant,w)] for w in ('status_table','linked_workbench','keep_producing'))
        assert a['observations']==b['observations'] and a['events']==b['events']
        for da,db in zip(a['decisions'],b['decisions']):
            assert all(da[k]==db[k] for k in ('public_input','input_sha256','advice','advice_sha256','rights_sha256','bundle','result'))
            assert comparison.digest(da['public_input'])==da['input_sha256']
            assert comparison.digest(da['advice'])==da['advice_sha256']
        for name,reference,candidate in (('workflow_B_minus_A',a,b),('deferral_action_minus_keep_producing',c,b)):
            increased=tasks.adverse(reference['metrics'],candidate['metrics'])
            if increased:negative.append(dict(case_id=case,variant=variant,comparison=name,increased=increased))
        if variant!='base':continue
        folder=target/case;folder.mkdir(exist_ok=True)
        with (folder/'A_STATUS_TABLE.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream)
            writer.writerow(['minute','receiving_m3_h','B_production_enabled','firm_inventory_m3','all_contained_m3',
                'uncontained_m3','unmet_B_units','public_input_sha256','same_advice_json','same_conditional_4h_json','exact_bundle_json','transaction_result'])
            for i,d in enumerate(a['decisions']):
                v=d['public_input'];f=next(f for f in v['firms'] if f['id']=='B')
                preview=cost_preview(v,d['bundle'])
                writer.writerow([d['minute'],v['downstream_capacity'],f['production_enabled'],f['stock'],v['balance']['stored'],
                    v['totals']['uncontained'],v['production_control']['metrics']['unmet_production_units'],d['input_sha256'],
                    json.dumps(d['advice'],ensure_ascii=False),json.dumps(preview,ensure_ascii=False),json.dumps(d['bundle'],ensure_ascii=False),d['result']])
                comparison.write(folder/('step_'+str(i).zfill(2)+'.json'),dict(
                    scope='Give only the current step to a participant; future steps are facilitator material.',
                    public_input=d['public_input'],same_advice=d['advice'],same_action_rights=tasks.rights(v),
                    same_conditional_4h_preview=preview,
                    A_exact_transaction=d['bundle'],B_exact_transaction=b['decisions'][i]['bundle'],
                    A_result=d['result'],B_result=b['decisions'][i]['result']))
        orders=[o for o in b['observations'][-1]['orders'] if o['kind']=='production']
        summaries.append(dict(case_id=case,production_orders=orders,metrics=b['metrics'],
            action_diagnostic=pair['action_diagnostic_B_minus_keep_producing'],
            deficit_bound=tasks.deficit_bound(a['observations'][0]),
            task_status='unavoidable_declared_deficit' if case=='unresolvable' else 'completed_replay_with_costs_and_remaining_inventory'))
        comparison.write(folder/'AUTHOR_COMPLETION.json',summaries[-1])
    assert negative==index['negative_results']
    actual={n:hashlib.sha256((comparison.ROOT/n).read_bytes()).hexdigest() for n in protocol['source_sha256']}
    assert actual==protocol['source_sha256']
    comparison.write(target/'VALIDATION.json',dict(status='pass',saved_traces_checked=len(checks),
        paired_full_state_checks=len(index['pairs']),generation_recomputed=True,negative_records_checked=len(negative),
        source_unchanged=True,participants=0,checks=checks,
        scope='Read-only arithmetic and matched-transaction check, not a human or field trial.'))
    comparison.write(target/'AUTHOR_COMPLETION.json',dict(participants=0,mode='scripted execution by shared advice, not independent operator behaviour',tasks=summaries))
    comparison.write(target/'RESPONSE_FORM.json',dict(participants=0,records=[],fields=['participant_id','condition',
        'task','decision','evidence_reference','prompting','unresolved_responsibility','actual_elapsed_seconds','notes']))
    print('Checked',len(checks),'traces; all 12 A/B states/transactions equal; generated water recomputed; 8 adverse records retained.')


if __name__=='__main__':main()

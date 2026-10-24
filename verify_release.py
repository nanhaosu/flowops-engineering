"""Check exact source provenance and all distributed saved trajectories."""
from pathlib import Path
import hashlib,json,gzip,math,zipfile
ROOT=Path(__file__).resolve().parent
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    originals=read(ROOT/'evidence/original_file_hashes.json')
    assert all(sha(ROOT/n)==h for n,h in originals.items()),'original-file hash mismatch'
    base=ROOT/'xylem/wastewater_workbench_v6/artifacts/comparison/v6_r2'
    protocol=read(base/'protocol.json');results=read(base/'results.json')
    assert all(sha(ROOT/n)==h for n,h in protocol['source_sha256'].items())
    for version in ('v6_r1','v6_r2'):
        folder=base.parent/version;p=read(folder/'protocol.json')
        with zipfile.ZipFile(folder/'source_snapshot.zip') as z:
            assert z.testzip() is None
            assert all(hashlib.sha256(z.read(n)).hexdigest()==h for n,h in p['source_sha256'].items())
    traces={};max_error=0.
    for row in results['rows']:
        with gzip.open(base/row['trace'],'rt',encoding='utf-8') as f:t=json.load(f)
        assert row['metrics']==t['metrics'],'summary/trace metric mismatch'
        obs=t['observations'];assert len(obs)==193 and obs[-1]['minute']==2880
        assert len(t['decisions'])==48
        assert all(math.isfinite(x) for x in row['metrics'].values())
        assert all(abs(v['balance']['error'])<1e-6 for v in obs)
        max_error=max(max_error,max(abs(v['balance']['error']) for v in obs))
        m=t['metrics'];assert abs(m['planned_units']-m['completed_units']-m['unmet_production_units'])<1e-6
        traces[(row['case_id'],row['variant'],row['workflow'])]=t
    for pair in results['pairs']:
        key=pair['case_id'],pair['variant']
        a=traces[(*key,'status_table')];b=traces[(*key,'linked_workbench')]
        assert a['observations']==b['observations'] and a['events']==b['events']
        assert a['decisions']==b['decisions']
    assert len(traces)==36 and len(results['pairs'])==12 and len(results['negative_results'])==8
    assert results['summary']['participants']==0
    print(json.dumps(dict(status='pass',original_files=len(originals),trajectories=len(traces),paired_tasks=12,
        physical_ties=12,adverse_records=8,participants=0,max_water_balance_error=max_error)))
if __name__=='__main__':main()

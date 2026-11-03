"""Update only a not-yet-training queued cycle, retaining its prepared data."""
import hashlib,json,os,shutil,signal,subprocess,time
from pathlib import Path

ROOT=Path(__file__).resolve().parent
def main():
    pid=int((ROOT/'cycle2.pid').read_text());deadline=time.monotonic()+900
    while True:
        status=json.loads((ROOT/'cycles/v2/status.json').read_text())
        if status['phase']=='waiting_for_v1_gpu_release':break
        if status['phase']!='preparing_stricter_dataset':raise RuntimeError('Do not alter a cycle that has already started training: '+status['phase'])
        if time.monotonic()>deadline:raise RuntimeError('Data preparation timeout')
        time.sleep(3)
    if b'run_cycle2.py' not in Path(f'/proc/{pid}/cmdline').read_bytes():raise RuntimeError('Queued process identity changed')
    os.kill(pid,signal.SIGTERM)
    for _ in range(30):
        if not Path(f'/proc/{pid}').exists():break
        time.sleep(.2)
    shutil.copy2(ROOT/'benchmark_v2.staged.py',ROOT/'benchmark_v2.py')
    protocol_path=ROOT/'cycles/v2/protocol.json';protocol=json.loads(protocol_path.read_text())
    protocol['amendment_before_model_evaluation']={'timestamp_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'change':'Supervise only the true/false decision token; dataset, selection metrics and locked holdout unchanged.'}
    protocol['configs'][0]['loss']='boolean decision token only; no prompt, image or JSON scaffold loss';protocol_path.write_text(json.dumps(protocol,indent=2))
    py=str(ROOT.parent/'.venv/bin/python')
    subprocess.run([py,str(ROOT/'test_decision_mask.py')],cwd=ROOT,stdout=(ROOT/'decision-mask-tests.log').open('w'),stderr=subprocess.STDOUT,check=True)
    run=ROOT/'cycles/v2/runs/v2';run.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/'benchmark_v2.py',run/'benchmark.executed.py')
    (run/'source_sha256.json').write_text(json.dumps({'benchmark_sha256':hashlib.sha256((ROOT/'benchmark_v2.py').read_bytes()).hexdigest(),'protocol_sha256':hashlib.sha256(protocol_path.read_bytes()).hexdigest()},indent=2))
    log=(ROOT/'cycle2.log').open('a');process=subprocess.Popen([py,'-u',str(ROOT/'run_cycle2.py')],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True);(ROOT/'cycle2.pid').write_text(str(process.pid));print('QUEUED_CYCLE_REFRESHED',process.pid,flush=True)

if __name__=='__main__':main()

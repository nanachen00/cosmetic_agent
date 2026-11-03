"""Prepare the stricter cycle now, then run it after v1 releases the GPU."""
import hashlib,json,os,shutil,subprocess,time,traceback
from pathlib import Path
import benchmark_v2 as b

def main():
    root=Path(__file__).resolve().parent
    b.ROOT.mkdir(parents=True,exist_ok=True)
    (b.ROOT/'data').mkdir(exist_ok=True)
    raw=b.ROOT/'data/raw'
    if not raw.exists():raw.symlink_to(root/'data/raw',target_is_directory=True)
    b.status('preparing_stricter_dataset');b.prepare()
    subprocess.run([str(root.parent/'.venv/bin/python'),str(root/'test_decision_mask.py')],cwd=root,stdout=(root/'decision-mask-tests.log').open('w'),stderr=subprocess.STDOUT,check=True)
    run=b.ROOT/'runs/v2';run.mkdir(parents=True,exist_ok=True)
    shutil.copy2(root/'benchmark_v2.py',run/'benchmark.executed.py')
    (run/'source_sha256.json').write_text(json.dumps({'benchmark_sha256':hashlib.sha256((root/'benchmark_v2.py').read_bytes()).hexdigest(),'protocol_sha256':hashlib.sha256((b.ROOT/'protocol.json').read_bytes()).hexdigest(),'negative_color_screen':'mean RGB evaluated from an eighth-resolution image; threshold45; not validated skin segmentation'},indent=2))
    pid=int((root/'benchmark.pid').read_text())
    b.status('waiting_for_v1_gpu_release')
    deadline=time.monotonic()+1800
    while Path(f'/proc/{pid}').exists():
        if time.monotonic()>deadline:raise RuntimeError('v1 did not finish within 30-minute chain budget')
        proc_status=Path(f'/proc/{pid}/status').read_text()
        if '\nState:\tZ' in proc_status:break
        time.sleep(5)
    first=json.loads((root/'status.json').read_text())
    if first['phase']!='complete':raise RuntimeError('v1 failed; do not start a concurrent or unreviewed GPU job')
    b.main()

if __name__=='__main__':
    try:main()
    except Exception as e:b.status('failed',error_type=type(e).__name__,error=str(e));traceback.print_exc();raise

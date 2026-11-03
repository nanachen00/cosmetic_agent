import json,subprocess,time
from pathlib import Path

ROOT=Path(__file__).resolve().parent
deadline=time.monotonic()+2400
while time.monotonic()<deadline:
    p=ROOT/'cycles/v2/status.json'
    state=json.loads(p.read_text()) if p.exists() else {}
    if state.get('phase')=='failed':raise RuntimeError('Second cycle failed; inspect its log')
    if state.get('phase')=='complete':
        pid=int((ROOT/'cycle2.pid').read_text())
        proc=Path(f'/proc/{pid}')
        if not proc.exists() or '\nState:\tZ' in (proc/'status').read_text():break
    time.sleep(5)
else:raise RuntimeError('Experiment chain timeout')
py=str(ROOT.parent/'.venv/bin/python')
subprocess.run([py,'-u',str(ROOT/'compatibility_probe.py')],cwd=ROOT,check=True)
subprocess.run([py,str(ROOT/'publish_comparison.py')],cwd=ROOT,check=True)

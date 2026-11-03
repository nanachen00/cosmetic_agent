#!/usr/bin/env python3
import argparse,json
from pathlib import Path
ap=argparse.ArgumentParser(); ap.add_argument('blue'); ap.add_argument('green'); a=ap.parse_args(); b=json.loads(Path(a.blue).read_text()); g=json.loads(Path(a.green).read_text()); bm={x['name']:x for x in b['results']}; gm={x['name']:x for x in g['results']}; bad=False
print('| Case | Blue | Green | Blue latency | Green latency | Exact text |'); print('|---|---:|---:|---:|---:|---:|')
for n in sorted(set(bm)|set(gm)):
    x=bm.get(n); y=gm.get(n)
    if not x or not y: bad=True; print(f'| {n} | missing | missing | - | - | - |'); continue
    bad=bad or not x.get('ok') or not y.get('ok'); print(f"| {n} | {'PASS' if x.get('ok') else 'FAIL'} | {'PASS' if y.get('ok') else 'FAIL'} | {x.get('latency_s')} | {y.get('latency_s')} | {'yes' if x.get('content')==y.get('content') else 'no'} |")
raise SystemExit(1 if bad else 0)

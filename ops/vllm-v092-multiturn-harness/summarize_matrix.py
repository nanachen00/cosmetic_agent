#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, json
from pathlib import Path

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--output-prefix', default='matrix_summary')
    args=ap.parse_args()
    root=Path(args.root)
    rows=[]
    sources=[]
    for p in sorted(root.glob('*/sweep_summary.json')):
        obj=json.loads(p.read_text(encoding='utf-8'))
        sources.append(str(p.relative_to(root)))
        scenario=obj.get('scenario') or p.parent.name
        for s in obj.get('results', []):
            t=s.get('telemetry',{}); cpu=t.get('cpu',{}); gpu=t.get('gpu',{}); vr=gpu.get('vram_used_gb',{})
            rows.append({
                'scenario': scenario,
                'vus': s.get('vus'),
                'conversations_per_vu': s.get('conversations_per_vu'),
                'planned_requests': s.get('planned_requests'),
                'http_success_rate': s.get('http_success_rate'),
                'strict_validation_pass_rate': s.get('strict_validation_pass_rate'),
                'semantic_validation_pass_rate': s.get('semantic_validation_pass_rate'),
                'request_rps': s.get('request_rps'),
                'completion_tokens_per_s': s.get('completion_tokens_per_s'),
                'latency_p50_s': s.get('latency_s',{}).get('p50'),
                'latency_p95_s': s.get('latency_s',{}).get('p95'),
                'latency_p99_s': s.get('latency_s',{}).get('p99'),
                'cpu_mean_pct': cpu.get('util_pct',{}).get('mean'),
                'gpu_gfx_mean_pct': gpu.get('gfx_util_pct',{}).get('mean'),
                'gpu_gfx_p95_pct': gpu.get('gfx_util_pct',{}).get('p95'),
                'gpu_power_mean_w': gpu.get('power_w',{}).get('mean'),
                'vram_start_gb': vr.get('start'),
                'vram_end_gb': vr.get('end'),
                'vram_max_gb': vr.get('max'),
                'vram_delta_gb': vr.get('delta_end_minus_start'),
            })
    if not rows:
        raise SystemExit(f'no */sweep_summary.json found under {root}')
    out_json=root/f'{args.output_prefix}.json'
    out_csv=root/f'{args.output_prefix}.csv'
    out_json.write_text(json.dumps({'sources':sources,'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    with out_csv.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(out_json)
    print(out_csv)

if __name__=='__main__':
    main()

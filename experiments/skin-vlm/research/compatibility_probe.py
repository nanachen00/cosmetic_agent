"""Characterize full-report compatibility after selection; not skin accuracy."""
import json,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent))

def main():
    import pyarrow.parquet as pq
    from peft import PeftModel
    from skin_vlm import SkinVLM
    first=json.loads((ROOT/'summary.json').read_text());second=json.loads((ROOT/'cycles/v2/summary.json').read_text())
    selected=[]
    for title,s in [('cycle1',first),('cycle2',second)]:
        selected.append((title,next(r['adapter'] for r in s['candidates'] if r['name']==s['selected_by_validation'])))
    rows=[json.loads(x) for x in (ROOT/'cycles/v2/data/frozen-v2/test.jsonl').read_text().splitlines()]
    wanted=list(dict.fromkeys(r['source_file'] for r in rows))[:2];found={};output=ROOT/'data/compatibility-probe';output.mkdir(exist_ok=True)
    for p in sorted((ROOT/'data/raw').glob('*.parquet')):
        for batch in pq.ParquetFile(p).iter_batches(batch_size=32):
            for row in batch.to_pylist():
                if row['file_name'] in wanted:
                    path=output/row['file_name'];path.write_bytes(row['image']['bytes']);found[row['file_name']]=path
            if len(found)==len(wanted):break
        if len(found)==len(wanted):break
    if len(found)!=2:raise RuntimeError('Missing author test images')
    cases=[('ccby_public_example',ROOT.parent/'data/public_acne_ccby4.jpg','skin'),('non_skin_control',ROOT.parent/'data/non_skin_control.png','non_skin'),('low_contrast_control',ROOT.parent/'data/low_contrast_control.png','low_contrast')]+[(f'author_heldout_face_{i}',found[name],'skin') for i,name in enumerate(wanted)]
    engine=SkinVLM(ROOT.parent/'model');results=[];stats=[]
    for version,adapter in [('original',None),*selected]:
        if adapter:engine.model=PeftModel.from_pretrained(engine.model,adapter,is_trainable=False).eval()
        reports=[]
        for case,path,role in cases:
            start=time.perf_counter();report=engine.analyze(path)
            entry={'version':version,'case':case,'expected_input_role':role,'wall_seconds':time.perf_counter()-start,'report':report};results.append(entry);reports.append(entry)
            (ROOT/'compatibility-probe.json').write_text(json.dumps({'status':'running','note':'Full-report schema/refusal characterization only. No ground-truth labels for the four natural-language skin tags; this is not accuracy. Low contrast is rejected by a heuristic before the model. Selected models are already frozen; probe results do not select a checkpoint.','results':results},ensure_ascii=False,indent=2))
        normal=[r for r in reports if r['expected_input_role']!='low_contrast']
        stats.append({'version':version,'model_calls':len(normal),'valid_full_report':sum(r['report'].get('status')!='invalid_model_output' for r in normal),'non_skin_refused':next(r['report'].get('observation',{}).get('image_scope')=='non_skin' and not r['report'].get('observation',{}).get('observations') for r in reports if r['expected_input_role']=='non_skin'),'low_contrast_heuristic_refused':next(r['report'].get('status')=='needs_retake' for r in reports if r['expected_input_role']=='low_contrast')})
        if adapter:engine.model=engine.model.unload().eval()
    final={'status':'complete','note':'Compatibility and refusal smoke only; not four-tag accuracy or clinical validation. Models selected before this probe; no checkpoint tuning uses probe results.','stats':stats,'results':results}
    (ROOT/'compatibility-probe.json').write_text(json.dumps(final,ensure_ascii=False,indent=2));print(json.dumps({'status':'COMPATIBILITY_PROBE_COMPLETE','stats':stats}),flush=True)

if __name__=='__main__':main()

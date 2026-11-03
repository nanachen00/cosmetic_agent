"""Frozen-split visual-tag evaluation; unknown/invalid results are not successes."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from core import TAGS,load_rows
from skin_vlm import SkinVLM

def score(rows,predictions):
    if not rows or len(rows)!=len(predictions): raise ValueError('Unequal/empty inputs')
    result={'images':len(rows),'valid_json_rate':sum(p.get('status') in {'needs_confirmation','needs_retake'} for p in predictions)/len(rows),'by_tag':{}}
    for tag in sorted(TAGS):
        tp=fp=fn=0
        for row,pred in zip(rows,predictions):
            actual=tag in {x['tag'] for x in row['target']['observations']}
            found=tag in {x['tag'] for x in pred.get('observation',{}).get('observations',[])}
            tp+=actual and found;fp+=not actual and found;fn+=actual and not found
        result['by_tag'][tag]={'tp':tp,'fp':fp,'fn':fn,'precision':tp/(tp+fp) if tp+fp else None,
            'recall':tp/(tp+fn) if tp+fn else None,'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None}
    negatives=[p for r,p in zip(rows,predictions) if r['target']['image_scope']=='non_skin']
    result['non_skin_correct_rejection_rate']=sum(p.get('observation',{}).get('image_scope')=='non_skin' for p in negatives)/len(negatives) if negatives else None
    result['scope']='Visual-tag presence only; no disease, count, or localization accuracy claims.'
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('manifest');p.add_argument('--model',default='model')
    p.add_argument('--adapter');p.add_argument('--output',default='results/evaluation.json');a=p.parse_args()
    rows=load_rows(a.manifest)
    if any(r['label_origin']!='human_verified' for r in rows): raise ValueError('Independently reviewed labels required')
    engine=SkinVLM(a.model,a.adapter);predictions=[engine.analyze(r['image'],r.get('note','')) for r in rows]
    report={'metrics':score(rows,predictions),'predictions':[{'id':r['id'],'prediction':p} for r,p in zip(rows,predictions)]}
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report['metrics'],ensure_ascii=False,indent=2))

if __name__=='__main__': main()

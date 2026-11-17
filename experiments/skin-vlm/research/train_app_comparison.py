"""Compare bounded four-tag app SFT candidates on original expert research data.

Uses validation for selection; opens the locked test only for the selected
candidate and its base reference. Never deploys or treats source concepts as
a clinical or phone-selfie validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core import TAGS,load_rows
from data_review import require_reviewed_labels
from research.v3.train_cycle3 import backup_checkpoint,require_persistent_mount

GRID=[
    {'name':'app-r8-lr5e-5','rank':8,'lr':5e-5},
    {'name':'app-r8-lr1e-4','rank':8,'lr':1e-4},
    {'name':'app-r16-lr5e-5','rank':16,'lr':5e-5},
]

def candidate_grid(study):
    if study=='visual-merger':
        return [dict(name='app-visual-r8-lr5e-5-epoch'+str(epochs),rank=8,lr=5e-5,
                     epochs=epochs,modules='language_attention_mlp_visual_merger') for epochs in (1,2)]
    if study!='standard':raise ValueError('Unknown comparison study')
    return [dict(config,epochs=2,modules='language_attention_mlp') for config in GRID]


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2));temporary.replace(path)


def summarize(metrics):
    tags=metrics['by_tag'];f1s=[value['f1'] for value in tags.values()]
    if any(value is None for value in f1s):raise ValueError('All four validation labels need positive/negative coverage')
    failures=[]
    if metrics['valid_json_rate']<.98:failures.append('valid_json_below_0.98')
    for name,values in tags.items():
        if values['precision'] is None or values['precision']<.8:failures.append(name+':precision_below_0.80')
        if values['recall'] is None or values['recall']<.8:failures.append(name+':recall_below_0.80')
    macro=sum(f1s)/len(f1s)
    if macro<.8:failures.append('macro_f1_below_0.80')
    return {'macro_f1':macro,'valid_json_rate':metrics['valid_json_rate'],'failures':failures,'eligible':not failures}


def main():
    parser=argparse.ArgumentParser()
    for name in ('data','model','output','report','backup-root'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--study',choices=('standard','visual-merger'),default='standard')
    args=parser.parse_args();require_persistent_mount([args.data,args.model,args.output,args.report,args.backup_root])
    grid=candidate_grid(args.study)
    metadata=json.loads((args.data/'split_info.json').read_text())
    coverage={}
    for split in ('train','val'):
        path=args.data/(split+'.jsonl')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=metadata['manifest_sha256'][split]:
            raise ValueError('Frozen '+split+' manifest changed')
        rows=load_rows(path);require_reviewed_labels(rows,True)
        coverage[split]={tag:sum(tag in {item['tag'] for item in row['target']['observations']} for row in rows) for tag in TAGS}
        if any(not 0<count<len(rows) for count in coverage[split].values()):
            raise ValueError('Each training/validation tag needs positive and negative examples')
    artifact=json.loads((args.model/'artifact-metadata.json').read_text())
    if artifact.get('verified') is not True:raise ValueError('Official model verification missing')
    source_files=('core.py','skin_vlm.py','rocm_patch_projection.py','data_review.py','train_lora.py','evaluate.py',
                  'research/train_app_comparison.py','research/download_skincon.py')
    protocol={'candidate_grid':grid,'study':args.study,'accumulation':8,'seed':20261115,
              'dataset':metadata,'coverage':coverage,'model':artifact,
              'code_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_files},
              'selection':'validation only; JSON>=0.98; each tag precision/recall>=0.80; macro-F1>=0.80; ties prefer fewer parameters',
              'research_only':True,'automatic_deployment':False}
    fingerprint=hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest()
    args.output.mkdir(parents=True,exist_ok=True);args.report.mkdir(parents=True,exist_ok=True)
    record=args.report/'protocol.json'
    if record.exists() and json.loads(record.read_text()).get('fingerprint')!=fingerprint:
        raise ValueError('Report belongs to different code/data/model; use a fresh run ID')
    write(record,dict(protocol,fingerprint=fingerprint))
    def run(command):
        command=list(map(str,command))
        print('RUN',json.dumps(command),flush=True)
        subprocess.run([sys.executable,'-u',*command],cwd=ROOT,check=True)
    def evaluate(name,adapter,split):
        target=args.report/(name+'-'+split+'.json')
        if not target.exists():
            command=[ROOT/'evaluate.py',args.data/(split+'.jsonl'),'--model',args.model,
                     '--output',target,'--allow-source-expert']
            if adapter:command+=['--adapter',adapter]
            run(command)
        return json.loads(target.read_text())['metrics']
    results=[{'name':'base','adapter':None,'parameters':0,'validation':evaluate('base',None,'val')}]
    for config in grid:
        out=args.output/config['name']
        if not (out/'training_summary.json').exists():
            run([ROOT/'train_lora.py','--train',args.data/'train.jsonl','--val',args.data/'val.jsonl',
                 '--model',args.model,'--out',out,'--epochs',str(config['epochs']),'--accumulation','8','--seed','20261115',
                 '--rank',str(config['rank']),'--lr',str(config['lr']),'--modules',config['modules'],
                 '--allow-source-expert','--backup-root',args.backup_root])
        training=json.loads((out/'training_summary.json').read_text())
        backup_checkpoint(out,args.backup_root/out.name,training)
        result={'name':config['name'],'adapter':str(out),'parameters':training['trainable_parameters'],
                'validation':evaluate(config['name'],out,'val'),'training':training}
        results.append(result);write(args.report/'progress.json',{'completed_candidates':len(results)-1,'total_candidates':len(grid),'results':results})
    for result in results:result['gate']=summarize(result['validation'])
    eligible=[result for result in results if result['gate']['eligible']]
    winner=max(eligible,key=lambda result:(result['gate']['macro_f1'],-result['parameters'])) if eligible else None
    summary={'status':'validation_selection_complete','selected':winner['name'] if winner else None,
             'candidates':results,'fingerprint':fingerprint,'dataset':metadata,'test_evaluated':False,
             'research_only':True,'clinical_validation':False,'phone_photo_evaluation':False,'automatic_deployment':False}
    # No arbitrary top-score fallback when the predeclared gates fail.
    if winner:
        test=args.data/'test.jsonl'
        if hashlib.sha256(test.read_bytes()).hexdigest()!=metadata['manifest_sha256']['test']:
            raise ValueError('Frozen test manifest changed')
        require_reviewed_labels(load_rows(test),True)
        summary['test']={'base':evaluate('base',None,'test')}
        if winner['adapter']:summary['test'][winner['name']]=evaluate(winner['name'],winner['adapter'],'test')
        summary['test_evaluated']=True
    write(args.report/'selection.json',summary)
    print(json.dumps({'selected':summary['selected'],'test_evaluated':summary['test_evaluated'],'automatic_deployment':False}),flush=True)


if __name__=='__main__':main()

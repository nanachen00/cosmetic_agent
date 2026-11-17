"""Read actual training artifacts; do not infer completion from lower loss."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def tail(path):
    if not path.exists():return []
    with path.open('rb') as stream:
        stream.seek(max(0,path.stat().st_size-65536))
        return stream.read().decode(errors='replace').splitlines()


def read(path):return json.loads(path.read_text()) if path.exists() else None


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path('/mnt/goodagents'))
    args=parser.parse_args();root=args.root;result={}
    for name,task,total,checkpoint_parent in (
        ('qwen-expanded-20261115-r2','box_derived_lesion_presence',6,'skin-vlm'),
        ('qwen-app-expert-20261115-r2','four_tag_app_research',3,'skin-app-sft'),
        ('qwen-app-visual-20261115-r1','four_tag_app_visual_merger',2,'skin-app-sft'),
    ):
        reports=root/'outputs/skin-vlm'/name;checkpoints=root/'checkpoints'/checkpoint_parent/name
        job=read(reports/'job.json')
        if not job:result[name]={'status':'not_started'};continue
        process=Path('/proc')/str(job['pid'])/'cmdline'
        running=process.exists() and name.encode() in process.read_bytes()
        selection=read(reports/'selection.json') or read(checkpoints/'selection.json')
        gpu=read(reports/'gpu.summary.json');lines=tail(reports/'training.log')
        state={'task':task,'running':running,'total_candidates':total,'results':[],
               'selection_complete':selection is not None,'research_only':True,'automatic_deployment':False}
        if gpu:state['returncode']=gpu['returncode']
        if task=='box_derived_lesion_presence':
            baseline=read(checkpoints/'baseline-validation.json')
            if baseline:state['base_validation_macro_f1']=baseline['macro_f1']
            for path in checkpoints.glob('*/result.json'):
                data=read(path);m=data['metrics']
                state['results'].append({'name':data['name'],'validation_macro_f1':m['macro_f1'],
                    'precision':m['positive_precision'],'recall':m['positive_recall'],
                    'backup_verified':(root/'checkpoint-backups/skin-vlm'/name/data['name']/'checkpoint-sha256.json').exists()})
            for line in reversed(lines):
                try:entry=json.loads(line)
                except (ValueError,TypeError):continue
                if isinstance(entry,dict) and 'candidate' in entry and 'step' in entry:
                    state['latest_step']=entry;break
        else:
            for path in sorted(reports.glob('*-val.json')):
                data=read(path);metrics=data['metrics'];name_=path.stem[:-4]
                f1s=[values['f1'] for values in metrics['by_tag'].values()]
                state['results'].append({'name':name_,'validation_macro_f1':sum(f1s)/len(f1s) if all(v is not None for v in f1s) else None,
                    'valid_json_rate':metrics['valid_json_rate'],'by_tag':metrics['by_tag']})
            for i in range(len(lines)-1,-1,-1):
                if lines[i].startswith('RUN '):
                    command=json.loads(lines[i][4:]);state['current_command']=Path(command[0]).name
                    for line in reversed(lines[i+1:]):
                        try:entry=json.loads(line)
                        except (ValueError,TypeError):continue
                        if isinstance(entry,dict) and 'step' in entry:state['latest_step']=entry;break
                    break
        state['completed_candidates']=len([row for row in state['results'] if row['name']!='base'])
        if selection:state['selection']=selection.get('selection',selection.get('selected'))
        if not running and not selection:
            state['error_tail']=[line for line in lines if 'Error:' in line or 'Traceback' in line][-4:]
        result[name]=state
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

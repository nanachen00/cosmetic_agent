"""Read the actual cleaning/training artifacts without touching the GPU."""
import argparse,json
from pathlib import Path


def read(path):return json.loads(path.read_text()) if path.exists() else None


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,default=Path('/mnt/goodagents/outputs/skin-vlm/qwen-clean-20261116-r1'));args=p.parse_args()
    run=args.run;results=run/'results';job=read(run/'training-job.json');selection=read(results/'selection.json');progress=read(results/'progress.json') or {}
    running=False
    if job:
        proc=Path('/proc')/str(job['pid'])/'cmdline'
        running=proc.exists() and run.name.encode() in proc.read_bytes()
    completed=progress.get('completed',0);state=dict(running=running,completed_trials=completed,total_trials=6,
        bar='['+'█'*completed+'░'*(6-completed)+']',selection_complete=selection is not None,phase='starting')
    base=read(results/'base-val.json')
    if base:state['baseline_validation']=base['metrics']
    state['trials']=[dict(name=row['name'],round=row['round'],macro_f1=row['validation']['macro_f1'],valid_json_rate=row['validation']['valid_json_rate'],gate=row['gate']) for row in progress.get('results',[])]
    path=run/'training.log'
    if path.exists():
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-65536));lines=stream.read().decode(errors='replace').splitlines()
        for line in reversed(lines):
            if line.startswith('EVAL_PROGRESS '):
                parts=line.split();state.update(phase='evaluation',evaluation=dict(file=parts[1],completed=int(parts[2]),total=int(parts[3])));break
            try:item=json.loads(line)
            except (ValueError,TypeError):continue
            if isinstance(item,dict) and 'step' in item and 'candidate' in item:state.update(phase='training',latest_step=item);break
        state['error_tail']=[line for line in lines if 'Traceback' in line or 'Error:' in line][-4:]
    if selection:
        state.update(phase='complete',chosen_recipe=selection['chosen_recipe'],replication_macro_f1=selection['replication_macro_f1'],
                     all_three_seeds_pass=selection['all_three_seeds_pass'],selected=selection['selected_research_adapter'],test_evaluated=selection['test_evaluated'])
    gpu=read(run/'gpu.summary.json')
    if gpu:state['returncode']=gpu['returncode']
    print(json.dumps(state,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

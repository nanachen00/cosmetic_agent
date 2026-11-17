"""Four predeclared decision-loss trials, then two independent seed repeats.

Only expert/source-object validation selects a recipe. The locked test opens
only if the chosen recipe passes the research gates on all three seeds.
"""
from __future__ import annotations
import argparse,hashlib,json,sys,time
import statistics
from importlib.metadata import version
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core import load_rows
from data_review import require_reviewed_labels
from research.clean_expansion import object_validator
from research.appearance_decisions import SkinVLM,evaluate,gate,train_candidate,unload,score
from research.v3.train_cycle3 import require_persistent_mount
from download_model import REPO,REVISION,WEIGHTS,file_hash

GRID=[
    dict(name='r8-balanced-lr3e5',rank=8,lr=3e-5,visual_lr=3e-6,balanced=True,modules='language_attention_mlp'),
    dict(name='r8-balanced-lr1e4',rank=8,lr=1e-4,visual_lr=1e-5,balanced=True,modules='language_attention_mlp'),
    dict(name='r8-uniform-lr3e5',rank=8,lr=3e-5,visual_lr=3e-6,balanced=False,modules='language_attention_mlp'),
    dict(name='r8-balanced-slow-visual',rank=8,lr=3e-5,visual_lr=3e-6,balanced=True,modules='language_attention_mlp_visual_merger'),
]


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix(path.suffix+'.partial')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2));temp.replace(path)


def load_split(data,metadata,name):
    path=data/(name+'.jsonl')
    if file_hash(path)!=metadata['manifest_sha256'][name]:raise ValueError('Frozen '+name+' manifest changed')
    rows=load_rows(path);require_reviewed_labels([row for row in rows if row['label_origin']=='source_expert_derived'],True)
    cache={}
    for row in rows:
        image=Path(row['image'])
        if not image.resolve().is_relative_to(Path('/mnt/goodagents')):raise ValueError('Input image escapes NFS')
        if file_hash(image)!=row['image_sha256']:raise ValueError('Image bytes changed')
        if row['label_origin']=='source_object_derived':object_validator(row,image,cache)
        elif row['label_origin']!='source_expert_derived':raise ValueError('AI/pending labels cannot enter this expert comparison')
        if row.get('subject_id_verified') is not False:raise ValueError('Do not promote source-image clusters to verified people')
    return rows


def bootstrap(rows,base,final,n=1000):
    import random
    groups={row['subject_id']:[i for i,item in enumerate(rows) if item['subject_id']==row['subject_id']] for row in rows}
    names=sorted(groups);rng=random.Random(20261116);deltas=[]
    for _ in range(n):
        indices=[i for name in rng.choices(names,k=len(names)) for i in groups[name]]
        subset=[rows[i] for i in indices]
        deltas.append(score(subset,[final[i] for i in indices])['macro_f1']-score(subset,[base[i] for i in indices])['macro_f1'])
    deltas.sort();return dict(method='paired source-image-cluster bootstrap',resamples=n,lower_95=deltas[int(.025*n)],upper_95=deltas[int(.975*n)])


def main():
    p=argparse.ArgumentParser()
    for name in ('data','model','output','report','backup-root'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args();mount=require_persistent_mount([args.data,args.model,args.output,args.report,args.backup_root])
    metadata=json.loads((args.data/'split_info.json').read_text());train=load_split(args.data,metadata,'train');val=load_split(args.data,metadata,'val')
    if {row['subject_id'] for row in train}&{row['subject_id'] for row in val}:raise ValueError('Training/validation group leakage')
    artifact=json.loads((args.model/'artifact-metadata.json').read_text())
    if artifact.get('repo_id')!=REPO or artifact.get('revision')!=REVISION or not artifact.get('verified'):raise ValueError('Pinned official weights missing')
    for name,expected in WEIGHTS.items():
        if file_hash(args.model/name)!=expected:raise ValueError('Official shard checksum mismatch')
    config=[dict(row,epochs=2,seed=20261116) for row in GRID]
    files=['core.py','skin_vlm.py','rocm_patch_projection.py','data_review.py','research/download_skincon.py',
           'download_model.py','research/download_coco_controls.py','research/clean_expansion.py','research/appearance_decisions.py','research/train_clean_comparison.py',
           'research/v3/train_cycle3.py','research/v3/error_analysis.py','research/v3/facial_research.py',
           'requirements-photo-linux-py311.lock','requirements-photo-native-py311.lock']
    protocol=dict(model=artifact,dataset=metadata,round1_grid=config,replication_seeds=[20261117,20261118],
        optimizer='AdamW; decision-token-only CE; class weights clipped 0.5..2; grad accumulation 8; clipping 1; warmup 5%/cosine; decay 0.01',
        visual_learning_rate='0.1 times language learning rate; visual merger only',
        augmentation='train-only random horizontal mirror; no colour changes or val/test transforms',
        runtime={name:version(name) for name in ('torch','transformers','peft','pillow','numpy','safetensors')},
        code_sha256={name:file_hash(ROOT/name) for name in files},
        selection='Round 1 best validation macro-F1 then fewer parameters; repeat same recipe on two additional seeds. No test unless all three pass every research gate, none regress below base, and median validation gain >=0.02. Test evaluates base and original chosen seed only.',
        research_gate='JSON>=0.98; four-tag macro-F1>=0.80; each tag precision/recall>=0.80; object rejection>=0.90',
        output_contract='Five booleans; not the existing descriptive website JSON; no automatic deployment',
        pending_or_AI_labels_used_for_training=False,research_only=True,mount=mount)
    fingerprint=hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest()
    if args.report.exists() and any(args.report.iterdir()):raise FileExistsError('Fresh comparison report required; no silent partial reuse')
    if args.output.exists() and any(args.output.iterdir()):raise FileExistsError('Fresh checkpoint run required')
    args.report.mkdir(parents=True,exist_ok=True);args.output.mkdir(parents=True,exist_ok=True)
    write(args.report/'protocol.json',dict(protocol,fingerprint=fingerprint))
    engine=SkinVLM(str(args.model));import torch
    if not torch.version.hip or 'MI300X' not in torch.cuda.get_device_name(0):raise RuntimeError('School MI300X required')
    base,_=evaluate(engine,val,args.report/'base-val.json')
    results=[]
    def trial(config,round_):
        lineage=dict(protocol_fingerprint=fingerprint,dataset_manifest_sha256=metadata['manifest_sha256'],base_model=REPO,
                     base_revision=REVISION,code_sha256=protocol['code_sha256'],runtime=protocol['runtime'])
        summary=train_candidate(engine,train,config,args.output/config['name'],args.backup_root,lineage)
        validation,_=evaluate(engine,val,args.report/(config['name']+'-val.json'))
        result=dict(name=config['name'],round=round_,config=config,validation=validation,gate=gate(validation),
                    parameters=summary['trainable_parameters'],adapter=str(args.output/config['name']))
        results.append(result);write(args.report/'progress.json',dict(completed=len(results),total=6,results=results))
        unload(engine);return result
    for config_ in config:trial(config_,1)
    first=max(results,key=lambda row:(row['validation']['macro_f1'],-row['parameters']))
    repeated=[first]
    for seed in (20261117,20261118):
        config_=dict(first['config'],seed=seed,name=first['name']+'-seed'+str(seed));repeated.append(trial(config_,2))
    all_pass=all(row['gate']['eligible'] for row in repeated)
    gains=[row['validation']['macro_f1']-base['macro_f1'] for row in repeated]
    improved=min(gains)>=0 and statistics.median(gains)>=.02
    eligible=all_pass and improved
    summary=dict(status='six_trials_complete',fingerprint=fingerprint,baseline_validation=base,candidates=results,
        chosen_recipe=first['name'],replication_macro_f1=[row['validation']['macro_f1'] for row in repeated],
        all_three_seeds_pass=all_pass,median_validation_gain=statistics.median(gains),all_seeds_nonregressing=min(gains)>=0,
        selected_research_adapter=first['adapter'] if eligible else None,
        test_evaluated=False,research_only=True,clinical_validation=False,phone_photo_evaluation=False,automatic_deployment=False)
    if eligible:
        test=load_split(args.data,metadata,'test')
        used={row['subject_id'] for row in train+val}
        if used&{row['subject_id'] for row in test}:raise ValueError('Locked-test group leaked')
        bm,bp=evaluate(engine,test,args.report/'base-test.json')
        from peft import PeftModel
        engine.model=PeftModel.from_pretrained(engine.model,first['adapter'],is_trainable=False,local_files_only=True)
        fm,fp=evaluate(engine,test,args.report/'selected-test.json')
        summary.update(test_evaluated=True,test=dict(base=bm,selected=fm,gate=gate(fm),macro_f1_gain=fm['macro_f1']-bm['macro_f1'],paired_bootstrap=bootstrap(test,bp,fp)))
    write(args.report/'selection.json',summary)
    print(json.dumps({'status':summary['status'],'selected':summary['selected_research_adapter'],'test_evaluated':summary['test_evaluated']},indent=2),flush=True)


if __name__=='__main__':main()

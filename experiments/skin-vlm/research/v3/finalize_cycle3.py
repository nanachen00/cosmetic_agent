"""Evaluate only the validation-selected binary adapter on the locked test.

This is the end of a frozen research cycle, not another tuning entry point.
The four-tag application needs its own validation and is never deployed here.
"""
from __future__ import annotations
import argparse,hashlib,json,sys
from importlib.metadata import version
from pathlib import Path


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def locked_rows(data,metadata):
    path=data/'test.jsonl'
    if digest(path)!=metadata['counts']['test']['sha256']:
        raise ValueError('Locked test manifest changed')
    rows=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    used=[]
    for split in ('train','validation'):
        used.extend(json.loads(line) for line in (data/(split+'.jsonl')).read_text().splitlines() if line.strip())
    groups={row['group_id'] for row in used}|set(metadata.get('forbidden_locked_test_groups',[]))
    ids={row['id'] for row in used};images={row['sha256'] for row in used}
    for row in rows:
        if row['group_id'] in groups or row['id'] in ids or row['sha256'] in images:
            raise ValueError('Locked test overlaps training, validation or historical holdout')
        image=Path(row['image']);image=image if image.is_absolute() else data/image
        if not image.resolve().is_relative_to(data.resolve()):raise ValueError('Test image escapes dataset')
        if digest(image)!=row['sha256']:raise ValueError('Locked test image changed')
        if type(row['lesion_present']) is not bool:raise ValueError('Invalid test label')
        row['image']=str(image.resolve());ids.add(row['id']);images.add(row['sha256'])
    if not rows:raise ValueError('Empty locked test')
    return rows


def main():
    p=argparse.ArgumentParser()
    for name in ('source','data','model','training','report'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args();sys.path.insert(0,str(args.source))
    from research.v3.train_cycle3 import require_persistent_mount,validate_data
    from research.v3.error_analysis import choose_validation_candidate
    from research.v3.facial_research import write_json
    require_persistent_mount([args.source,args.data,args.model,args.training,args.report])
    bundle=validate_data(args.data)
    selection_path=args.training/'selection.json'
    selection=json.loads(selection_path.read_text())
    if selection['status']!='validation_selection_complete':raise ValueError('Validation comparison is not complete')
    candidates=[dict(name='qwen-base',split='validation',metrics=selection['baseline_validation'],trainable_parameters=0)]+selection['candidates']
    selected=choose_validation_candidate(candidates)['selected']
    if selected!=selection['selection']['selected']:raise ValueError('Validation selection changed')
    if selected is None:raise ValueError('No eligible validation candidate; locked test remains closed')
    candidate=next(row for row in candidates if row['name']==selected)
    rows=locked_rows(args.data,bundle['meta'])
    artifact=json.loads((args.model/'artifact-metadata.json').read_text())
    if not artifact.get('verified'):raise ValueError('Verified official model metadata required')
    run=json.loads((args.training/'run.json').read_text())
    if run.get('attention_backend')!='eager':raise ValueError('Finalization requires the validated eager runtime')
    adapter=Path(candidate['adapter']) if selected!='qwen-base' else None
    adapter_hashes={}
    if adapter:
        if not adapter.resolve().is_relative_to(args.training.resolve()):raise ValueError('Adapter escapes frozen training run')
        adapter_hashes=json.loads((adapter/'checkpoint-sha256.json').read_text())
        for relative,expected in adapter_hashes.items():
            path=(adapter/relative).resolve()
            if not path.is_relative_to(adapter.resolve()) or digest(path)!=expected:raise ValueError('Adapter backup manifest mismatch')
    protocol={'selected':selected,'selection_sha256':digest(selection_path),'run_fingerprint':run['fingerprint'],
              'test_manifest_sha256':bundle['meta']['counts']['test']['sha256'],'model':artifact,'adapter_files':adapter_hashes,
              'source_sha256':{name:digest(args.source/name) for name in ('research/benchmark_v2.py','rocm_patch_projection.py')},
              'environment':{name:version(name) for name in ('torch','transformers','peft','pillow','safetensors')},
              'finalizer_sha256':digest(Path(__file__)),
              'research_gate':{'macro_f1':.8,'positive_precision':.85,'macro_f1_gain':.03,'valid_json_rate':.98},
              'automatic_deployment':False}
    fingerprint=hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest()
    args.report.mkdir(parents=True,exist_ok=True);record=args.report/'protocol.json'
    if record.exists():
        if json.loads(record.read_text())['fingerprint']!=fingerprint:raise ValueError('Finalization protocol differs; use a fresh report')
    elif any(args.report.iterdir()):raise ValueError('Unidentified partial test report')
    write_json(record,dict(protocol,fingerprint=fingerprint))
    import torch,transformers
    from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
    from rocm_patch_projection import configure_rocm_patch_projection
    from research.benchmark_v2 import evaluate,bootstrap
    if not torch.cuda.is_available() or not torch.version.hip or 'MI300X' not in torch.cuda.get_device_name(0):
        raise RuntimeError('Actual ROCm MI300X required')
    processor=AutoProcessor.from_pretrained(args.model,local_files_only=True,trust_remote_code=False)
    processor.image_processor.size={'shortest_edge':128*32*32,'longest_edge':384*32*32}
    model=Qwen3VLForConditionalGeneration.from_pretrained(args.model,dtype=torch.bfloat16,attn_implementation='eager',
        trust_remote_code=False,use_safetensors=True,local_files_only=True)
    projection=configure_rocm_patch_projection(model);model.to('cuda:0').eval()
    baseline,base_predictions=evaluate(model,processor,rows,args.report/'base-test.jsonl')
    if adapter:
        from peft import PeftModel
        model=PeftModel.from_pretrained(model,str(adapter),is_trainable=False,local_files_only=True)
    final,final_predictions=evaluate(model,processor,rows,args.report/'selected-test.jsonl')
    gain=final['macro_f1']-baseline['macro_f1']
    result={'status':'locked_test_complete','selected':selected,'fingerprint':fingerprint,'baseline':baseline,'selected_test':final,
            'macro_f1_gain':gain,'paired_bootstrap':bootstrap(rows,base_predictions,final_predictions),
            'research_gate_passed':final['macro_f1']>=.8 and final['positive_precision']>=.85 and gain>=.03 and final['valid_json_rate']>=.98,
            'runtime':{'torch':torch.__version__,'transformers':transformers.__version__,'gpu':torch.cuda.get_device_name(0),'projection':projection},
            'automatic_deployment':False,'clinical_validation':False,'phone_photo_evaluation':False,
            'scope':'Author-box-derived patch presence only; no four-tag, clinical or subject-disjoint accuracy claim.'}
    write_json(args.report/'summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()

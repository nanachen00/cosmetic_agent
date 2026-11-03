#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,platform,sys,time
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--model-path',default='/var/lib/jenkins/tomod/taide-12b'); ap.add_argument('--cases',default=str(Path(__file__).with_name('taide_generate_cases.json'))); ap.add_argument('--yes-load-model',action='store_true'); ap.add_argument('--allow-non-rocm',action='store_true'); ap.add_argument('--output'); a=ap.parse_args()
    mp=Path(a.model_path).resolve(); cfg=json.loads((mp/'config.json').read_text()); tc=cfg.get('text_config') or {}
    print('===== MODEL CONFIG ====='); print('path=',mp,sep=''); print('model_type=',cfg.get('model_type'),sep=''); print('architectures=',cfg.get('architectures'),sep=''); print('text_config.model_type=',tc.get('model_type'),sep=''); print('text_config.max_position_embeddings=',tc.get('max_position_embeddings'),sep='')
    import torch,transformers
    print('\n===== RUNTIME ====='); print('python_executable=',sys.executable,sep=''); print('python=',platform.python_version(),sep=''); print('torch=',torch.__version__,sep=''); print('transformers=',transformers.__version__,sep=''); print('torch.cuda.is_available=',torch.cuda.is_available(),sep=''); print('torch.version.cuda=',torch.version.cuda,sep=''); print('torch.version.hip=',torch.version.hip,sep='')
    if not a.allow_non_rocm:
        if not torch.cuda.is_available(): raise SystemExit('[ERROR] accelerator unavailable; refusing CPU generation')
        if torch.version.hip is None: raise SystemExit('[ERROR] ROCm required; refusing CUDA/CPU runtime')
    if torch.cuda.is_available(): print('device_name=',torch.cuda.get_device_name(0),sep='')
    if not a.yes_load_model: print('\n[SAFE EXIT] metadata only; add --yes-load-model'); return 0
    from transformers import AutoModelForCausalLM,AutoTokenizer
    cases=json.loads(Path(a.cases).read_text(encoding='utf-8')); t=time.perf_counter(); tok=AutoTokenizer.from_pretrained(str(mp),local_files_only=True); tls=time.perf_counter()-t; print(f'\nTokenizer load: {tls:.3f}s')
    t=time.perf_counter(); model=AutoModelForCausalLM.from_pretrained(str(mp),torch_dtype=torch.bfloat16,device_map='auto',local_files_only=True); model.eval(); mls=time.perf_counter()-t
    print('Model class:',model.__class__.__name__); print(f'Model load: {mls:.3f}s'); print('model_device=',getattr(model,'device','unknown'),sep=''); print('hf_device_map=',getattr(model,'hf_device_map',None),sep='')
    dm=getattr(model,'hf_device_map',None)
    if isinstance(dm,dict):
        bad={str(v) for v in dm.values() if str(v)=='cpu' or str(v).startswith('disk')}
        if bad: raise SystemExit(f'[ERROR] CPU/disk offload detected: {sorted(bad)}')
    rows=[]
    for case in cases:
        rendered=tok.apply_chat_template(case['messages'],tokenize=False,add_generation_prompt=True); inp=tok(rendered,return_tensors='pt').to(model.device); temp=float(case.get('temperature',0.0)); kw={'input_ids':inp.input_ids,'attention_mask':inp.attention_mask,'max_new_tokens':int(case.get('max_new_tokens',64)),'do_sample':temp>0};
        if temp>0: kw['temperature']=temp
        torch.cuda.synchronize(); s=time.perf_counter();
        with torch.inference_mode(): out=model.generate(**kw)
        torch.cuda.synchronize(); e=time.perf_counter()-s; pt=int(inp.input_ids.shape[1]); ct=int(out.shape[1]-pt); content=tok.decode(out[0][pt:],skip_special_tokens=True).strip(); row={'name':case['name'],'prompt_tokens':pt,'completion_tokens':ct,'generation_s':round(e,4),'completion_tokens_per_s':round(ct/e,4),'content':content}; rows.append(row); print('\n'+json.dumps(row,ensure_ascii=False,indent=2))
    if a.output: Path(a.output).write_text(json.dumps({'engine':'hf-direct-rocm','results':rows},ensure_ascii=False,indent=2)); print(f'\n[OK] report={a.output}')
    return 0
if __name__=='__main__': raise SystemExit(main())

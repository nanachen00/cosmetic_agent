"""Optional new vLLM process on a free private port. Never touches :8200.
No package install, no freeing other processes' memory, no automatic fallback.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
from common import Refuse, atomic_json, read_json, probe, sha_file, utc
import process_control as pc

MEM_CODE=r'''
import torch,json
if not torch.cuda.is_available():raise SystemExit('GPU unavailable')
if torch.cuda.device_count()!=1:raise SystemExit('Owned model plan requires one visible GPU')
free,total=torch.cuda.mem_get_info(0)
print(json.dumps({'free_bytes':free,'total_bytes':total,'device':torch.cuda.get_device_name(0),'hip':torch.version.hip,'visible_devices':torch.cuda.device_count()}))
'''


def memory_gate(original:dict,fraction:float,margin_gib:float, *, query=None) -> dict:
    if query is None:
        r=subprocess.run([original['exe'],'-I','-B','-c',MEM_CODE],capture_output=True,text=True,
                         timeout=30,check=False,env=original['env'],cwd=original['cwd'])
        if r.returncode:raise Refuse('Owned-model GPU memory probe failed; no model started')
        try:raw=json.loads(r.stdout.splitlines()[-1])
        except (ValueError,IndexError):raise Refuse('Invalid GPU memory probe output') from None
    else:raw=query()
    if raw.get('visible_devices')!=1:raise Refuse('One visible GPU required for owned model')
    free,total=int(raw['free_bytes']),int(raw['total_bytes'])
    if not (0<total and 0<=free<=total):raise Refuse('Invalid GPU memory accounting')
    budget=int(total*fraction);margin=int(margin_gib*1024**3)
    return {**raw,'allocation_fraction':fraction,'budget_bytes':budget,'margin_bytes':margin,
            'required_free_bytes':budget+margin,'pass':free>=budget+margin,
            'reservation':False,'scope':'point-in-time conservative admission estimate, NOT a reservation or OOM guarantee'}


def build_argv(original:dict,port:int,seqs:int,fraction:float):
    argv=list(original['argv'])
    if not any('vllm' in Path(a).name for a in argv[:2]) or 'serve' not in argv:
        raise Refuse('Owned clone supports vLLM serve CLI only')
    if port in pc.PROTECTED or not 1024<=port<=65535:raise Refuse('Reserved owned-model port')
    for flag in ('--tensor-parallel-size','--pipeline-parallel-size','--data-parallel-size'):
        if pc.flag_value(argv,flag) not in (None,'1'):raise Refuse('Owned model only supports single-GPU TP/PP/DP=1')
    i=argv.index('serve')
    if i+1>=len(argv) or not Path(argv[i+1]).is_dir():raise Refuse('Owned model requires an existing local model directory')
    for flag in ('--ssl-keyfile','--ssl-certfile','--api-key','--uds'):
        if pc.flag_value(argv,flag):raise Refuse('Unsupported inherited vLLM transport/auth flag: '+flag)
    for flag,value in [('--port',str(port)),('--host','127.0.0.1'),('--max-num-seqs',str(seqs)),('--gpu-memory-utilization',str(fraction))]:
        argv=pc.set_flag(argv,flag,value)
    return argv


def model_files(original: dict) -> dict:
    """Record small config hashes and weight file metadata, not weight content.
    This is a drift observation, not an immutable model snapshot.
    """
    argv=original['argv'];i=argv.index('serve');root=Path(argv[i+1]).resolve(strict=True)
    rows={}
    for p in sorted(root.rglob('*')):
        if not p.is_file() or p.suffix not in {'.json','.jinja','.safetensors','.bin','.model'}:continue
        st=p.stat();rel=p.relative_to(root).as_posix()
        rows[rel]={'size':st.st_size,'mtime_ns':st.st_mtime_ns,'ctime_ns':st.st_ctime_ns,'inode':st.st_ino,'resolved_path':str(p.resolve())}
        if p.suffix in {'.json','.jinja'} and st.st_size<=16*1024*1024:
            rows[rel]['sha256']=sha_file(p)
    if not rows:raise Refuse('Owned model has no observable local model assets')
    return {'root':str(root),'files':rows,'scope':'small config content hashes and weight metadata; not full weight hashes'}


def start(original,cfg,state,journal,journal_file,run):
    port=cfg['owned_vllm_port']
    pc.free_port(port)
    gate=memory_gate(original,cfg['vllm_gpu_memory_utilization'],cfg['gpu_margin_gib'])
    atomic_json(run/'owned-vllm-memory-gate.json',gate)
    if not gate['pass']:
        raise Refuse('OWNED_GPU_HEADROOM_INSUFFICIENT: no shared model stopped, no ratio silently reduced; use shared mode or dedicated GPU')
    argv=build_argv(original,port,cfg['vllm_max_num_seqs'],cfg['vllm_gpu_memory_utilization'])
    before=model_files(original);atomic_json(state/'owned-model-assets.json',before)
    env=dict(original['env'])
    env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',VLLM_NO_USAGE_STATS='1',DO_NOT_TRACK='1',
               PYTHONDONTWRITEBYTECODE='1',VLLM_CACHE_ROOT=str(state/'model-cache'),
               TORCHINDUCTOR_CACHE_DIR=str(state/'model-cache'/'inductor'),TRITON_CACHE_DIR=str(state/'model-cache'/'triton'))
    p=pc.start_owned(argv,Path(original['cwd']),env,state/'raw-logs'/'owned-vllm.log',port,journal,journal_file,'owned-vllm')
    probe(f'http://127.0.0.1:{port}/v1/models',cfg['ready_timeout_seconds'],require_key='data',process=p)
    after=model_files(original)
    atomic_json(run/'owned-model-assets-observation.json',{'unchanged_during_start':before==after,'files':len(before['files']),'full_weight_hashes':False})
    if before!=after:raise Refuse('OWNED_MODEL_ASSETS_CHANGED_DURING_START: owned cleanup follows, no shared rollback')
    rec=pc.capture(port)
    if rec['pid']!=p.pid or rec['argv']!=argv:raise Refuse('Owned vLLM listener/argv mismatch')
    journal['owned']['owned-vllm']['descendants']=pc.descendants(p.pid);atomic_json(journal_file,journal)
    evidence={'mode':'owned','port':port,'pass':True,'pid':p.pid,'max_num_seqs':pc.flag_value(argv,'--max-num-seqs'),
              'gpu_memory_utilization':pc.flag_value(argv,'--gpu-memory-utilization'),
              'shared_8200_actions':[], 'model_weights_copied':False,'model_runtime_is_shared_installation':True}
    atomic_json(run/'model-mode-proof.json',evidence)
    return p

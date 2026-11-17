"""One real MI300X backward pass on an explicitly synthetic control.

Checks kernels, masking and visual-merger gradients; produces no accuracy
measurement or deployable checkpoint. The full comparison starts from clean
official base weights in a separate process.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import time

from train_cycle3 import lora_targets, require_persistent_mount


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--attention',choices=('eager','sdpa'),default='eager')
    args=parser.parse_args();require_persistent_mount([args.model,args.output])
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    artifact=json.loads((args.model/'artifact-metadata.json').read_text())
    if artifact.get('verified') is not True:raise ValueError('Model not verified')
    import torch
    from PIL import Image,ImageDraw
    from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
    from peft import LoraConfig,get_peft_model
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import benchmark_v2 as legacy
    if not torch.version.hip or not torch.cuda.is_available() or 'MI300X' not in torch.cuda.get_device_name(0):
        raise RuntimeError('MI300X ROCm GPU required')
    args.output.mkdir(parents=True,exist_ok=True)
    image=Image.new('RGB',(384,384),(175,137,115));ImageDraw.Draw(image).ellipse((120,120,145,145),fill=(200,90,80))
    path=args.output/'synthetic-control.png';image.save(path)
    processor=AutoProcessor.from_pretrained(args.model,local_files_only=True,trust_remote_code=False)
    processor.image_processor.size={'shortest_edge':128*32*32,'longest_edge':384*32*32}
    print('LOAD_OFFICIAL_BASE_CPU',flush=True)
    base=Qwen3VLForConditionalGeneration.from_pretrained(args.model,dtype=torch.bfloat16,
        attn_implementation=args.attention,local_files_only=True,trust_remote_code=False,use_safetensors=True)
    print('MOVE_BASE_TO_MI300X',flush=True)
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from rocm_patch_projection import configure_rocm_patch_projection
    patch=configure_rocm_patch_projection(base)
    print('ROCM_PATCH_PROJECTION',json.dumps(patch),flush=True)
    base=base.to('cuda:0')
    print('ATTACH_LORA',flush=True)
    names=lora_targets([name for name,_ in base.named_modules()],'language_attention_mlp_visual_merger')
    model=get_peft_model(base,LoraConfig(r=8,lora_alpha=16,lora_dropout=.05,target_modules=names,bias='none',task_type='CAUSAL_LM'))
    model.config.use_cache=False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
    model.enable_input_require_grads();model.train()
    params=[p for p in model.parameters() if p.requires_grad]
    optimizer=torch.optim.AdamW(params,lr=5e-5)
    print('ENCODE_CONTROL',flush=True)
    batch=legacy.encode(processor,{'image':str(path),'lesion_present':True},True)
    batch={key:value.to('cuda:0') for key,value in batch.items()}
    torch.cuda.reset_peak_memory_stats();start=time.monotonic()
    print('GPU_FORWARD',flush=True)
    loss=model(**batch).loss
    if not torch.isfinite(loss):raise RuntimeError('Nonfinite loss')
    print('GPU_BACKWARD',flush=True)
    loss.backward();norm=torch.nn.utils.clip_grad_norm_(params,1.)
    visual_grads=[float(p.grad.float().norm()) for name,p in model.named_parameters()
                  if '.visual.' in name and p.requires_grad and p.grad is not None]
    if not torch.isfinite(norm) or float(norm)<=0 or not any(value>0 for value in visual_grads):
        raise RuntimeError('Missing/nonfinite language or visual merger gradients')
    optimizer.step();torch.cuda.synchronize()
    result={'engineering_smoke_only':True,'accuracy_evaluated':False,'checkpoint_saved':False,
            'gpu':torch.cuda.get_device_name(0),'hip':torch.version.hip,'loss':float(loss.detach()),
            'gradient_norm':float(norm),'visual_merger_gradients_nonzero':True,
            'trainable_parameters':sum(p.numel() for p in params),'seconds':time.monotonic()-start,
            'peak_allocated_bytes':torch.cuda.max_memory_allocated(),'source_revision':artifact['revision'],
            'rocm_patch_projection':patch,'attention':args.attention}
    (args.output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))


if __name__=='__main__':main()

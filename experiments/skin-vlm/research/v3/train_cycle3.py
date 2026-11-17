"""Train and select cycle-3 LoRA candidates on a newly frozen validation set.

This command intentionally does not accept or evaluate a test manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

try:
    from .error_analysis import choose_validation_candidate
    from .facial_research import write_json
except ImportError:
    from error_analysis import choose_validation_candidate
    from facial_research import write_json

ROOT=Path(__file__).resolve().parent
SEED=20261101


def read_jsonl(path: Path) -> List[Dict[str,Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def require_persistent_mount(paths: Sequence[Path]) -> Dict[str,Any]:
    root=Path("/mnt/goodagents")
    mount=json.loads(subprocess.check_output(["findmnt","-J","-T",str(root)],text=True))["filesystems"][0]
    if mount["target"]!=str(root) or mount["fstype"] not in {"nfs","nfs4"}:
        raise RuntimeError("/mnt/goodagents is not the expected persistent NFS mount")
    if any(not path.resolve().is_relative_to(root) for path in paths):
        raise ValueError("Model, data, checkpoint and backup paths must remain on /mnt/goodagents")
    return mount


def backup_checkpoint(source: Path, destination: Path, lineage: Mapping[str,Any]) -> None:
    def hashes(folder):
        return {str(path.relative_to(folder)):hashlib.sha256(path.read_bytes()).hexdigest()
                for path in folder.rglob("*") if path.is_file() and path.name!="checkpoint-sha256.json"}
    if not list(source.glob("*.safetensors")): raise ValueError("No adapter weights to back up")
    write_json(source/"training-lineage.json",dict(lineage))
    checksums=hashes(source);write_json(source/"checkpoint-sha256.json",checksums)
    if destination.exists():
        if hashes(destination)!=checksums: raise ValueError("Existing checkpoint backup differs")
        return
    partial=destination.with_name(destination.name+".partial")
    if partial.exists(): raise ValueError("Incomplete backup exists: "+str(partial))
    partial.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(source,partial)
    if hashes(partial)!=checksums: raise RuntimeError("Checkpoint backup checksum mismatch")
    partial.rename(destination)


def validate_data(data: Path) -> Dict[str,Any]:
    meta=json.loads((data/"dataset.json").read_text(encoding="utf-8"))
    train=read_jsonl(data/"train.jsonl");validation=read_jsonl(data/"validation.jsonl")
    train_groups={x["group_id"] for x in train};val_groups={x["group_id"] for x in validation}
    if train_groups & val_groups: raise ValueError("train/validation group leakage")
    forbidden=set(meta.get("forbidden_locked_test_groups",[]))
    overlap=(train_groups|val_groups)&forbidden
    if overlap: raise ValueError("historical locked-test group reused: "+next(iter(overlap)))
    ids=set();images=set()
    for split,rows in (("train",train),("validation",validation)):
        if not rows: raise ValueError(split+" is empty")
        actual=hashlib.sha256((data/(split+".jsonl")).read_bytes()).hexdigest()
        if meta.get("counts",{}).get(split,{}).get("sha256")!=actual:
            raise ValueError(split+" manifest changed after freeze")
        for row in rows:
            if not row.get("id") or not row.get("group_id"): raise ValueError("missing ID/group")
            if row["id"] in ids: raise ValueError("duplicate example ID")
            ids.add(row["id"])
            if type(row.get("lesion_present")) is not bool: raise ValueError("invalid label")
            image=Path(row["image"])
            if not image.is_absolute(): image=data/image
            if not image.is_file(): raise ValueError("missing image: "+str(image))
            row["image"]=str(image.resolve())
            image_hash=hashlib.sha256(image.read_bytes()).hexdigest()
            if image_hash!=row.get("sha256"): raise ValueError("image checksum mismatch: "+row["id"])
            if image_hash in images: raise ValueError("duplicate image across training/validation examples")
            images.add(image_hash)
            if row.get("hard_positive") and row.get("hard_positive_origin")!="training_cross_validation":
                raise ValueError("hard positives must be mined only from training cross-validation")
    return {"meta":meta,"train":train,"validation":validation}


def lora_targets(names: Sequence[str], mode: str) -> List[str]:
    attention=("q_proj","k_proj","v_proj","o_proj")
    if mode not in {"language_attention","language_attention_mlp","language_attention_mlp_visual_merger"}:
        raise ValueError("unknown LoRA targets: "+mode)
    suffixes=attention if mode=="language_attention" else attention+("gate_proj","up_proj","down_proj")
    selected=[name for name in names if "language_model" in name and name.endswith(suffixes)]
    if not selected: raise RuntimeError("no approved language LoRA targets found")
    if mode.endswith("visual_merger"):
        visual=[name for name in names if ".visual." in name and "merger" in name
                and name.endswith(("linear_fc1","linear_fc2"))]
        if not visual: raise RuntimeError("Qwen visual merger targets missing; refusing language-only fallback")
        selected.extend(visual)
    return selected


def expanded_training_rows(rows: Sequence[Mapping[str,Any]], config: Mapping[str,Any]) -> List[Dict[str,Any]]:
    result=[dict(x) for x in rows]
    if config.get("hard_positive_sampling"):
        if not any(x.get("hard_positive") for x in rows):
            raise ValueError("hard-positive candidate has no training cross-validation labels")
        result.extend(dict(x) for x in rows if x.get("hard_positive") is True)
    return result


def train_one(base: Any, processor: Any, train: Sequence[Mapping[str,Any]], validation: Sequence[Mapping[str,Any]],
              config: Mapping[str,Any], output: Path) -> Any:
    import torch
    from peft import LoraConfig,get_peft_model
    try:
        import research.benchmark_v2 as legacy
    except ImportError:
        import sys
        sys.path.insert(0,str(ROOT.parent))
        import benchmark_v2 as legacy

    torch.manual_seed(SEED);rng=random.Random(SEED)
    torch.cuda.reset_peak_memory_stats()
    mode=config.get("target_modules","language_attention_mlp")
    names=lora_targets([n for n,_ in base.named_modules()],mode)
    model=get_peft_model(base,LoraConfig(r=config["rank"],lora_alpha=config["alpha"],lora_dropout=config.get("dropout",.05),
        target_modules=names,bias="none",task_type="CAUSAL_LM"))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant":False});model.enable_input_require_grads()
    params=[value for _,value in model.named_parameters() if value.requires_grad]
    if any("visual" in name and (not mode.endswith("visual_merger") or "merger" not in name or "lora_" not in name)
           for name,value in model.named_parameters() if value.requires_grad):
        raise RuntimeError("unapproved visual parameter became trainable")
    optimizer=torch.optim.AdamW(params,lr=config["learning_rate"],weight_decay=.01)
    rows=expanded_training_rows(train,config);accum=config.get("accumulation",4)
    if accum<1: raise ValueError("invalid accumulation")
    total_steps=math.ceil(len(rows)/accum)*config["epochs_max"];warmup=max(1,math.ceil(total_steps*.05))
    scheduler=torch.optim.lr_scheduler.LambdaLR(optimizer,lambda step:min(1.,(step+1)/warmup)*(
        .5+.5*math.cos(math.pi*max(0,step-warmup)/max(1,total_steps-warmup))))
    output.mkdir(parents=True,exist_ok=True);history=[];best=-1.;global_step=0
    for epoch in range(config["epochs_max"]):
        order=list(rows);rng.shuffle(order);model.train();model.config.use_cache=False;losses=[]
        for offset in range(0,len(order),accum):
            micro=order[offset:offset+accum];optimizer.zero_grad(set_to_none=True)
            weights=[config.get("positive_example_weight",1.) if x["lesion_present"] else 1. for x in micro]
            denom=sum(weights)
            for row,weight in zip(micro,weights):
                batch=legacy.encode(processor,dict(row,_flip=rng.random()<.5),True)
                loss=model(**{key:value.to(model.device) for key,value in batch.items()}).loss
                if not torch.isfinite(loss): raise RuntimeError("nonfinite loss")
                (loss*weight/denom).backward();losses.append(float(loss.detach()))
            norm=torch.nn.utils.clip_grad_norm_(params,1.)
            if not torch.isfinite(norm) or float(norm)==0: raise RuntimeError("nonfinite/zero gradient")
            optimizer.step();scheduler.step();global_step+=1
            if global_step==1 or global_step%10==0:
                print(json.dumps({"candidate":config["name"],"epoch":epoch+1,"step":global_step,
                                  "total_steps":total_steps,"loss":sum(losses[-len(micro):])/len(micro),
                                  "gpu_peak_allocated_bytes":torch.cuda.max_memory_allocated()}),flush=True)
        metrics,_=legacy.evaluate(model,processor,list(validation),output/("validation-epoch%d.jsonl"%(epoch+1)))
        entry={"epoch":epoch+1,"step":global_step,"mean_loss":sum(losses)/len(losses),"metrics":metrics}
        history.append(entry);write_json(output/"history.json",history)
        if metrics["macro_f1"]>best:
            best=metrics["macro_f1"];model.save_pretrained(output/"best",safe_serialization=True)
            processor.save_pretrained(output/"best");write_json(output/"best-epoch.json",entry)
        else: break
    best_entry=json.loads((output/"best-epoch.json").read_text(encoding="utf-8"))
    from safetensors import safe_open
    with safe_open(output/"best"/"adapter_model.safetensors",framework="pt",device="cpu") as tensors:
        trainable=sum(tensors.get_tensor(key).numel() for key in tensors.keys())
    model.gradient_checkpointing_disable();base=model.unload();delattr(base,"peft_config") if hasattr(base,"peft_config") else None;torch.cuda.empty_cache()
    result={"name":config["name"],"split":"validation","metrics":best_entry["metrics"],
            "best_epoch":best_entry["epoch"],"trainable_parameters":trainable,
            "adapter":str(output/"best"),"config":dict(config),"history":history,
            "lora_target_modules":names,"visual_merger_adapted":mode.endswith("visual_merger")}
    write_json(output/"result.json",result);return base,result


def main() -> None:
    p=argparse.ArgumentParser();p.add_argument("--data",type=Path,required=True)
    p.add_argument("--model",type=Path,default=ROOT.parent.parent/"model")
    p.add_argument("--output",type=Path,default=ROOT/"records"/"training")
    p.add_argument("--plan",type=Path,default=ROOT/"tuning_plan.json")
    p.add_argument("--backup-root",type=Path,default=Path("/mnt/goodagents/checkpoint-backups/skin-vlm"))
    p.add_argument("--attention",choices=("eager","sdpa"))
    a=p.parse_args();mount=require_persistent_mount([a.data,a.model,a.output,a.backup_root]);bundle=validate_data(a.data)
    if not a.model.is_dir(): raise ValueError("local model directory missing: "+str(a.model))
    import torch
    from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
    if not torch.cuda.is_available() or not torch.version.hip or "MI300X" not in torch.cuda.get_device_name(0):
        raise RuntimeError("This training run requires the school's ROCm MI300X GPU")
    plan=json.loads(a.plan.read_text());results=[]
    attention=a.attention or plan.get("attention_backend","eager")
    if attention not in {"eager","sdpa"}:raise ValueError("Unknown attention backend")
    artifact_path=a.model/"artifact-metadata.json"
    artifact=json.loads(artifact_path.read_text()) if artifact_path.exists() else {}
    if plan.get("base_revision") and (artifact.get("revision")!=plan["base_revision"] or artifact.get("verified") is not True):
        raise ValueError("Pinned official model verification metadata missing or mismatched")
    fingerprint=hashlib.sha256(json.dumps({"plan":plan,"dataset":bundle["meta"],
        "model_artifact":artifact,
        "attention_backend":attention,
        "model_config_sha256":hashlib.sha256((a.model/"config.json").read_bytes()).hexdigest(),
        "encoder_sha256":hashlib.sha256((ROOT.parent/"benchmark_v2.py").read_bytes()).hexdigest(),
        "rocm_projection_sha256":hashlib.sha256((ROOT.parent.parent/"rocm_patch_projection.py").read_bytes()).hexdigest(),
        "trainer_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},sort_keys=True).encode()).hexdigest()
    a.output.mkdir(parents=True,exist_ok=True)
    run_file=a.output/"run.json"
    if run_file.exists():
        if json.loads(run_file.read_text()).get("fingerprint")!=fingerprint:
            raise ValueError("Existing run belongs to another model/data/plan/trainer; choose a fresh output")
    elif any(a.output.iterdir()): raise ValueError("Unidentified nonempty training output")
    else: write_json(run_file,{"fingerprint":fingerprint,"plan":plan,"dataset":bundle["meta"],
                              "model":str(a.model.resolve()),"gpu":torch.cuda.get_device_name(0),"hip":torch.version.hip,
                              "mount":mount,"backup_root":str(a.backup_root),"attention_backend":attention})
    processor=AutoProcessor.from_pretrained(a.model,trust_remote_code=False,local_files_only=True)
    processor.image_processor.size={"shortest_edge":128*32*32,"longest_edge":384*32*32}
    base=Qwen3VLForConditionalGeneration.from_pretrained(a.model,dtype=torch.bfloat16,
        attn_implementation=attention,trust_remote_code=False,use_safetensors=True,local_files_only=True)
    import sys
    sys.path.insert(0,str(ROOT.parent.parent))
    from rocm_patch_projection import configure_rocm_patch_projection
    patch=configure_rocm_patch_projection(base);base=base.to("cuda:0").eval()
    write_json(a.output/"rocm-projection.json",patch)
    try:
        import research.benchmark_v2 as legacy
    except ImportError:
        import sys
        sys.path.insert(0,str(ROOT.parent))
        import benchmark_v2 as legacy
    baseline,_=legacy.evaluate(base,processor,bundle["validation"],a.output/"baseline-validation.jsonl")
    write_json(a.output/"baseline-validation.json",baseline)
    for config in plan["candidate_grid"]:
        destination=a.output/config["name"]
        if (destination/"result.json").exists(): result=json.loads((destination/"result.json").read_text())
        else:
            if destination.exists() and any(destination.iterdir()):
                raise ValueError("Incomplete candidate output; use a fresh run to avoid mixing predictions: "+str(destination))
            base,result=train_one(base,processor,bundle["train"],bundle["validation"],config,destination)
        backup=a.backup_root/a.output.name/config["name"]
        backup_checkpoint(destination/"best",backup,{"fingerprint":fingerprint,"base_model":artifact,
                                                   "config":config,"dataset":bundle["meta"],"result":result})
        result["backup"]=str(backup)
        results.append(result)
    selection=choose_validation_candidate([{"name":"qwen-base","split":"validation","metrics":baseline,
                                            "trainable_parameters":0}]+results)
    summary={"status":"validation_selection_complete","selection":selection,"candidates":results,
             "baseline_validation":baseline,"fingerprint":fingerprint,
             "dataset":bundle["meta"],"test_evaluated":False,"automatic_deployment":False,
             "completed_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    write_json(a.output/"selection.json",summary);print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=="__main__":main()


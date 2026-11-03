"""Train and select cycle-3 LoRA candidates on a newly frozen validation set.

This command intentionally does not accept or evaluate a test manifest.
"""
from __future__ import annotations

import argparse
import json
import math
import random
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


def validate_data(data: Path) -> Dict[str,Any]:
    meta=json.loads((data/"dataset.json").read_text(encoding="utf-8"))
    train=read_jsonl(data/"train.jsonl");validation=read_jsonl(data/"validation.jsonl")
    train_groups={x["group_id"] for x in train};val_groups={x["group_id"] for x in validation}
    if train_groups & val_groups: raise ValueError("train/validation group leakage")
    forbidden=set(meta.get("forbidden_locked_test_groups",[]))
    overlap=(train_groups|val_groups)&forbidden
    if overlap: raise ValueError("historical locked-test group reused: "+next(iter(overlap)))
    for split,rows in (("train",train),("validation",validation)):
        if not rows: raise ValueError(split+" is empty")
        for row in rows:
            if type(row.get("lesion_present")) is not bool: raise ValueError("invalid label")
            if not Path(row["image"]).is_file(): raise ValueError("missing image: "+row["image"])
            if row.get("hard_positive") and row.get("hard_positive_origin")!="training_cross_validation":
                raise ValueError("hard positives must be mined only from training cross-validation")
    return {"meta":meta,"train":train,"validation":validation}


def expanded_training_rows(rows: Sequence[Mapping[str,Any]], config: Mapping[str,Any]) -> List[Dict[str,Any]]:
    result=[dict(x) for x in rows]
    if config.get("hard_positive_sampling"):
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
    names=[n for n,_ in base.named_modules() if "language_model" in n and n.endswith(
        ("q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"))]
    if not names: raise RuntimeError("no approved language LoRA targets found")
    model=get_peft_model(base,LoraConfig(r=config["rank"],lora_alpha=config["alpha"],lora_dropout=.05,
        target_modules=names,bias="none",task_type="CAUSAL_LM"))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant":False});model.enable_input_require_grads()
    params=[value for _,value in model.named_parameters() if value.requires_grad]
    if any("visual" in name for name,value in model.named_parameters() if value.requires_grad):
        raise RuntimeError("cycle-3 language LoRA must keep vision frozen")
    optimizer=torch.optim.AdamW(params,lr=config["learning_rate"],weight_decay=.01)
    rows=expanded_training_rows(train,config);accum=4
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
            if not torch.isfinite(norm): raise RuntimeError("nonfinite gradient")
            optimizer.step();scheduler.step();global_step+=1
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
            "adapter":str(output/"best"),"config":dict(config),"history":history}
    write_json(output/"result.json",result);return base,result


def main() -> None:
    p=argparse.ArgumentParser();p.add_argument("--data",type=Path,required=True)
    p.add_argument("--model",type=Path,default=ROOT.parent.parent/"model")
    p.add_argument("--output",type=Path,default=ROOT/"records"/"training")
    a=p.parse_args();bundle=validate_data(a.data)
    if not a.model.is_dir(): raise ValueError("local model directory missing: "+str(a.model))
    import torch
    from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
    processor=AutoProcessor.from_pretrained(a.model,trust_remote_code=False,local_files_only=True)
    processor.image_processor.size={"shortest_edge":128*32*32,"longest_edge":384*32*32}
    base=Qwen3VLForConditionalGeneration.from_pretrained(a.model,torch_dtype=torch.bfloat16,
        attn_implementation="sdpa",trust_remote_code=False,use_safetensors=True,local_files_only=True).to("cuda:0").eval()
    plan=json.loads((ROOT/"tuning_plan.json").read_text(encoding="utf-8"));results=[]
    for config in plan["candidate_grid"]:
        destination=a.output/config["name"]
        if (destination/"result.json").exists(): result=json.loads((destination/"result.json").read_text())
        else: base,result=train_one(base,processor,bundle["train"],bundle["validation"],config,destination)
        results.append(result)
    selection=choose_validation_candidate(results)
    summary={"status":"validation_selection_complete","selection":selection,"candidates":results,
             "dataset":bundle["meta"],"test_evaluated":False,"automatic_deployment":False,
             "completed_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    write_json(a.output/"selection.json",summary);print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=="__main__":main()


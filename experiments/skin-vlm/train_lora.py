"""BF16 LoRA on language attention; explicit assistant-only label masking."""
from __future__ import annotations
import argparse,hashlib,json,math,random
from pathlib import Path
from core import load_rows
from prepare_data import assert_no_leakage
from skin_vlm import SkinVLM,messages_for,open_image

def encode_example(processor,row):
    import torch
    image,_=open_image(row['image']);prompt=messages_for(image,row.get('note',''))
    full=prompt+[{'role':'assistant','content':[{'type':'text','text':json.dumps(row['target'],ensure_ascii=False,separators=(',',':'))}]}]
    prefix=processor.apply_chat_template(prompt,tokenize=True,add_generation_prompt=True,return_dict=True,return_tensors='pt')
    batch=processor.apply_chat_template(full,tokenize=True,add_generation_prompt=False,return_dict=True,return_tensors='pt')
    n=prefix['input_ids'].shape[1]
    if not torch.equal(prefix['input_ids'],batch['input_ids'][:,:n]): raise ValueError('Chat template prefix mismatch')
    if batch['input_ids'].shape[1]>4096: raise ValueError('Example too long; do not truncate image tokens')
    labels=batch['input_ids'].clone();labels[:,:n]=-100;labels[batch['attention_mask']==0]=-100
    for tok in ('<|image_pad|>','<|vision_start|>','<|vision_end|>','<|video_pad|>'):
        idx=processor.tokenizer.convert_tokens_to_ids(tok)
        if idx is not None: labels[batch['input_ids']==idx]=-100
    if not (labels!=-100).any(): raise ValueError('No supervised assistant tokens')
    batch['labels']=labels;return batch

def main():
    p=argparse.ArgumentParser();p.add_argument('--train',required=True);p.add_argument('--val')
    p.add_argument('--model',default='model');p.add_argument('--out',default='adapters/skin-lora')
    p.add_argument('--steps',type=int,default=100);p.add_argument('--accumulation',type=int,default=8)
    p.add_argument('--lr',type=float,default=1e-4);p.add_argument('--seed',type=int,default=42)
    p.add_argument('--rank',type=int,default=8)
    p.add_argument('--alpha',type=int,default=None,help='Defaults to twice the rank')
    p.add_argument('--dropout',type=float,default=.05)
    p.add_argument('--epochs',type=float,help='Optional full-data passes; overrides --steps')
    p.add_argument('--smoke',action='store_true');a=p.parse_args();rows=load_rows(a.train)
    val=load_rows(a.val) if a.val else []
    if a.accumulation<1:raise ValueError('Accumulation must be positive')
    if a.epochs is not None:
        if a.epochs<=0:raise ValueError('Epochs must be positive')
        a.steps=math.ceil(len(rows)*a.epochs/a.accumulation)
    a.alpha=a.alpha if a.alpha is not None else 2*a.rank
    if a.steps<1 or a.accumulation<1 or a.lr<=0 or a.rank<1 or a.alpha<1 or not 0<=a.dropout<1: raise ValueError('Invalid hyperparameters')
    if a.smoke:
        if a.steps!=1 or any(r['label_origin']!='synthetic_control' for r in rows): raise ValueError('Smoke needs one step and synthetic controls')
    else:
        if not val or any(r['label_origin']!='human_verified' for r in rows+val): raise ValueError('Reviewed train/validation data required')
        assert_no_leakage(rows,val)
    out=Path(a.out)
    if out.exists() and any(out.iterdir()): raise ValueError('Choose a fresh adapter directory')
    import torch
    from peft import LoraConfig,get_peft_model
    torch.manual_seed(a.seed);random.seed(a.seed);engine=SkinVLM(a.model)
    names=[n for n,_ in engine.model.named_modules() if 'language_model' in n and n.endswith(('q_proj','k_proj','v_proj','o_proj'))]
    if not names: raise ValueError('Language-attention modules not found')
    model=get_peft_model(engine.model,LoraConfig(r=a.rank,lora_alpha=a.alpha,lora_dropout=a.dropout,target_modules=names,bias='none',task_type='CAUSAL_LM'))
    model.config.use_cache=False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
    model.enable_input_require_grads();params=[(n,v) for n,v in model.named_parameters() if v.requires_grad]
    if any('visual' in n for n,_ in params): raise ValueError('Visual encoder unexpectedly trainable')
    optimizer=torch.optim.AdamW([v for _,v in params],lr=a.lr);model.train();log=[]
    rng=random.Random(a.seed);order=[]
    def next_row():
        nonlocal order
        if not order:
            order=list(range(len(rows)));rng.shuffle(order)
        return rows[order.pop()]
    for step in range(a.steps):
        optimizer.zero_grad(set_to_none=True);losses=[]
        for micro in range(a.accumulation):
            batch=encode_example(engine.processor,next_row())
            loss=model(**{k:v.to(model.device) for k,v in batch.items()}).loss
            if not torch.isfinite(loss): raise RuntimeError('Non-finite loss')
            (loss/a.accumulation).backward();losses.append(float(loss.detach()))
        norm=torch.nn.utils.clip_grad_norm_([v for _,v in params],1.)
        if not torch.isfinite(norm) or float(norm)==0: raise RuntimeError('Invalid/zero adapter gradient')
        optimizer.step();entry={'step':step+1,'train_loss':sum(losses)/len(losses),'grad_norm':float(norm)}
        if val and ((step+1)%20==0 or step+1==a.steps):
            model.eval();vl=[]
            with torch.no_grad():
                for row in val:
                    batch=encode_example(engine.processor,row)
                    vl.append(float(model(**{k:v.to(model.device) for k,v in batch.items()}).loss))
            entry['validation_loss']=sum(vl)/len(vl);model.train()
        log.append(entry);print(json.dumps(entry),flush=True)
    out.mkdir(parents=True,exist_ok=True);model.save_pretrained(out,safe_serialization=True)
    engine.processor.save_pretrained(out)
    summary={'engineering_smoke_only':a.smoke,'clinical_validation':False,'steps':a.steps,'accumulation':a.accumulation,
             'rank':a.rank,'alpha':a.alpha,'dropout':a.dropout,'requested_epochs':a.epochs,
             'sample_exposures':a.steps*a.accumulation,'learning_rate':a.lr,'seed':a.seed,'train_images':len(rows),'validation_images':len(val),
             'trainable_parameters':sum(v.numel() for _,v in params),'metrics':log,
             'train_manifest_sha256':hashlib.sha256(Path(a.train).read_bytes()).hexdigest(),
             'base_model':str(Path(a.model).resolve()),'warning':'Successful training/lower loss is not skin-recognition accuracy.'}
    (out/'training_summary.json').write_text(json.dumps(summary,indent=2));print('ADAPTER_SAVED',out)

if __name__=='__main__': main()

"""Qwen appearance-decision research: supervise decisions, not template prose.

This compact five-boolean contract is a separate research task. Adapters are
not drop-in replacements for the website's descriptive JSON model.
"""
from __future__ import annotations
import hashlib,json,math,random,time
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from skin_vlm import SkinVLM,open_image
from research.v3.train_cycle3 import lora_targets,backup_checkpoint

KEYS=('skin_visible','acne_like_bumps','redness','dark_marks','flaking')
SYSTEM='''Observe visible appearance only. Do not diagnose diseases, count lesions, infer a person's identity, or give treatment.
Return ONLY one JSON object with exactly five boolean values: skin_visible, acne_like_bumps, redness, dark_marks, flaking.
skin_visible means the main subject is human skin suitable for an appearance diary. Objects, plants, scenes and animal fur are not skin observations.
acne_like_bumps means visibly small raised bumps, not scars or a disease name. redness means visible red/pink areas relative to adjacent skin, not just warm lighting.
dark_marks means localized darker spots or patches, not naturally darker skin or shadows. flaking means actual visible flakes/scales, not just wrinkles or shine.
When skin_visible is false, all four appearance features must be false. Use JSON true/false, no descriptions or additional keys.'''


def expected(row):
    tags={item['tag'] for item in row['target']['observations']}
    return {key:row['target']['image_scope'] in {'face','skin_closeup'} if key=='skin_visible' else key in tags for key in KEYS}


def parse(text):
    text=text.strip()
    if text.startswith('```json') and text.endswith('```'):text=text[7:-3].strip()
    result=json.loads(text)
    if not isinstance(result,dict) or set(result)!=set(KEYS) or any(type(result[key]) is not bool for key in KEYS):raise ValueError('Invalid decision schema')
    if not result['skin_visible'] and any(result[key] for key in KEYS[1:]):raise ValueError('Non-skin decision has skin features')
    return result


def messages(row,flip=False):
    from PIL import ImageOps
    image,quality=open_image(row['image'])
    if quality['reasons']:raise ValueError('Cleaned image failed inference quality checks: '+row['id'])
    if flip:image=ImageOps.mirror(image)
    return [{'role':'system','content':[{'type':'text','text':SYSTEM}]},
            {'role':'user','content':[{'type':'image','image':image},{'type':'text','text':'Observe the image and return the five decisions.'}]}]


def encode(processor,row,flip=False):
    import torch
    prompt=messages(row,flip)
    full=prompt+[{'role':'assistant','content':[{'type':'text','text':json.dumps(expected(row),separators=(',',':'))}]}]
    prefix=processor.apply_chat_template(prompt,tokenize=True,add_generation_prompt=True,return_dict=True,return_tensors='pt')
    batch=processor.apply_chat_template(full,tokenize=True,add_generation_prompt=False,return_dict=True,return_tensors='pt')
    batch.pop('token_type_ids',None);length=prefix['input_ids'].shape[1]
    if not torch.equal(prefix['input_ids'],batch['input_ids'][:,:length]):raise ValueError('Assistant prefix mismatch')
    boolean_ids=[]
    for value in ('true','false'):
        token_ids=processor.tokenizer.encode(value,add_special_tokens=False)
        if len(token_ids)!=1:raise ValueError('Boolean tokenizer changed')
        boolean_ids.extend(token_ids)
    mask=torch.zeros_like(batch['input_ids'],dtype=torch.bool)
    for token_id in boolean_ids:mask|=batch['input_ids']==token_id
    mask[:,:length]=False;positions=mask[0].nonzero().flatten()
    if len(positions)!=len(KEYS):raise ValueError('Expected exactly five supervised decision tokens')
    targets=batch['input_ids'][0,positions]
    if batch['input_ids'].shape[1]>4096:raise ValueError('Image/text too long; no silent truncation')
    return batch,positions,targets


def score(rows,predictions):
    if not rows or len(rows)!=len(predictions):raise ValueError('Empty/unequal evaluation inputs')
    by_tag={}
    for key in KEYS:
        tp=fp=tn=fn=0
        for row,pred in zip(rows,predictions):
            truth=expected(row)[key];valid=pred.get('decision') is not None
            value=pred['decision'][key] if valid else not truth
            if truth and value:tp+=1
            elif truth:fn+=1
            elif value:fp+=1
            else:tn+=1
        div=lambda a,b:a/b if b else 0.
        by_tag[key]=dict(tp=tp,fp=fp,tn=tn,fn=fn,precision=div(tp,tp+fp),recall=div(tp,tp+fn),f1=div(2*tp,2*tp+fp+fn))
    controls=[pred for row,pred in zip(rows,predictions) if not expected(row)['skin_visible']]
    return dict(images=len(rows),valid_json_rate=sum(pred.get('decision') is not None for pred in predictions)/len(rows),
        by_tag=by_tag,macro_f1=sum(by_tag[key]['f1'] for key in KEYS[1:])/4,
        non_skin_rejection_rate=sum(pred.get('decision') is not None and not pred['decision']['skin_visible'] for pred in controls)/len(controls) if controls else None,
        scope='Compact visible-tag decisions on cleaned public-source groups; not clinical, localization or phone-person validation')


def gate(metrics):
    reasons=[]
    if metrics['valid_json_rate']<.98:reasons.append('JSON_below_0.98')
    if metrics['macro_f1']<.8:reasons.append('macro_F1_below_0.80')
    for key in KEYS[1:]:
        for name in ('precision','recall'):
            if metrics['by_tag'][key][name]<.8:reasons.append(key+':'+name+'_below_0.80')
    if metrics['non_skin_rejection_rate'] is not None and metrics['non_skin_rejection_rate']<.9:reasons.append('object_rejection_below_0.90')
    return dict(eligible=not reasons,failures=reasons)


def evaluate(engine,rows,path,batch_size=1):
    import torch
    if path.exists():raise FileExistsError('Predictions already exist; caller must validate the frozen protocol before reuse')
    engine.model.eval();engine.model.config.use_cache=True
    predictions=[];processor=engine.processor;processor.tokenizer.padding_side='left'
    for offset in range(0,len(rows),batch_size):
        chunk=rows[offset:offset+batch_size];prompts=[messages(row) for row in chunk]
        batch=processor.apply_chat_template(prompts,tokenize=True,add_generation_prompt=True,
            padding=True,return_dict=True,return_tensors='pt').to(engine.model.device)
        batch.pop('token_type_ids',None);start=time.monotonic()
        with torch.inference_mode():ids=engine.model.generate(**batch,max_new_tokens=120,max_time=35,do_sample=False)
        torch.cuda.synchronize();elapsed=(time.monotonic()-start)/len(chunk)
        outputs=processor.batch_decode(ids[:,batch['input_ids'].shape[1]:],skip_special_tokens=True)
        for row,text in zip(chunk,outputs):
            try:decision=parse(text)
            except (ValueError,TypeError):decision=None
            predictions.append(dict(id=row['id'],subject_id=row['subject_id'],decision=decision,raw_output=text,seconds=round(elapsed,4)))
        path.parent.mkdir(parents=True,exist_ok=True)
        path.with_suffix('.progress.json').write_text(json.dumps(dict(completed=len(predictions),total=len(rows))))
        print('EVAL_PROGRESS',path.name,len(predictions),len(rows),flush=True)
    metrics=score(rows,predictions);path.write_text(json.dumps(dict(metrics=metrics,predictions=predictions),ensure_ascii=False,indent=2))
    return metrics,predictions


def class_weights(rows,balanced):
    result={}
    for key in KEYS:
        positive=sum(expected(row)[key] for row in rows);negative=len(rows)-positive
        if min(positive,negative)==0:raise ValueError('Training class lacks both decisions: '+key)
        result[key]={'true':min(2.,max(.5,len(rows)/(2*positive))) if balanced else 1.,
                     'false':min(2.,max(.5,len(rows)/(2*negative))) if balanced else 1.}
    return result


def train_candidate(engine,rows,config,out,backup,lineage):
    import torch
    from torch.nn import functional as F
    from peft import LoraConfig,get_peft_model
    if config['epochs']<1 or config['rank']<1 or config['lr']<=0 or not 0<config['visual_lr']<=config['lr']:
        raise ValueError('Invalid predeclared training parameters')
    if out.exists() and any(out.iterdir()):raise FileExistsError('Fresh candidate output required')
    torch.manual_seed(config['seed']);rng=random.Random(config['seed']);weights=class_weights(rows,config['balanced'])
    names=lora_targets([name for name,_ in engine.model.named_modules()],config['modules'])
    model=get_peft_model(engine.model,LoraConfig(r=config['rank'],lora_alpha=config['rank']*2,lora_dropout=.05,
        target_modules=names,bias='none',task_type='CAUSAL_LM'))
    model.config.use_cache=False;model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False});model.enable_input_require_grads()
    groups={'language':[],'visual':[]}
    for name,value in model.named_parameters():
        if not value.requires_grad:continue
        if '.visual.' in name:
            if 'merger' not in name or 'lora_' not in name:raise ValueError('Unapproved visual backbone gradient')
            groups['visual'].append(value)
        else:groups['language'].append(value)
    optimizer=torch.optim.AdamW([dict(params=values,lr=config['visual_lr'] if kind=='visual' else config['lr']) for kind,values in groups.items() if values],weight_decay=.01)
    parameters=groups['language']+groups['visual'];total=math.ceil(len(rows)/8)*config['epochs'];warmup=max(1,math.ceil(.05*total))
    schedule=torch.optim.lr_scheduler.LambdaLR(optimizer,lambda n:min(1.,(n+1)/warmup)*(.5+.5*math.cos(math.pi*max(0,n-warmup)/max(1,total-warmup))))
    history=[];step=0;torch.cuda.reset_peak_memory_stats()
    for epoch in range(config['epochs']):
        order=list(rows);rng.shuffle(order);model.train()
        for start in range(0,len(order),8):
            chunk=order[start:start+8];optimizer.zero_grad(set_to_none=True);losses=[]
            for row in chunk:
                batch,positions,targets=encode(engine.processor,row,flip=rng.random()<.5)
                batch={key:value.to(model.device) for key,value in batch.items()};positions=positions.to(model.device);targets=targets.to(model.device)
                # Project only the five supervised hidden states through the large vocabulary head.
                logits=model(**batch,logits_to_keep=positions-1).logits[0].float()
                if logits.shape[0]!=5:raise ValueError('Decision logits slicing changed')
                token_loss=F.cross_entropy(logits,targets,reduction='none')
                values=expected(row);w=torch.tensor([weights[key]['true' if values[key] else 'false'] for key in KEYS],device=model.device)
                loss=(token_loss*w).sum()/w.sum()
                if not torch.isfinite(loss):raise RuntimeError('Nonfinite decision loss')
                (loss/len(chunk)).backward();losses.append(float(loss.detach()))
            norm=torch.nn.utils.clip_grad_norm_(parameters,1.)
            if not torch.isfinite(norm) or float(norm)<=0:raise RuntimeError('Missing/nonfinite adapter gradient')
            optimizer.step();schedule.step();step+=1
            entry=dict(candidate=config['name'],epoch=epoch+1,step=step,total_steps=total,loss=sum(losses)/len(losses),gradient_norm=float(norm))
            history.append(entry)
            if step==1 or step%5==0 or step==total:print(json.dumps(entry),flush=True)
    out.mkdir(parents=True);model.save_pretrained(out,safe_serialization=True);engine.processor.save_pretrained(out)
    summary=dict(lineage=lineage,config=config,steps=step,training_images=len(rows),decision_tokens_per_image=5,class_weights=weights,
        trainable_parameters=sum(value.numel() for value in parameters),visual_trainable_parameters=sum(value.numel() for value in groups['visual']),
        peak_gpu_allocated_bytes=torch.cuda.max_memory_allocated(),history=history,research_only=True,automatic_deployment=False)
    (out/'training-summary.json').write_text(json.dumps(summary,indent=2));backup_checkpoint(out,backup/out.name,summary)
    model.gradient_checkpointing_disable();model.eval();engine.model=model
    return summary


def unload(engine):
    import torch
    engine.model.disable_input_require_grads()
    engine.model=engine.model.unload()
    if hasattr(engine.model,'peft_config'):delattr(engine.model,'peft_config')
    torch.cuda.empty_cache()

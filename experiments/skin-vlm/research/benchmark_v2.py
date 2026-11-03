"""Bounded ACNE04 patch experiment; author boxes, frozen holdout, no user photos.

This task is annotation-conditional lesion presence in local crops. It does not
validate the app's four skin tags, diagnosis, anatomical localization, or normal
skin. The original dataset has no reliable subject identifiers.
"""
from __future__ import annotations
import argparse, contextlib, csv, hashlib, html, io, json, math, random, statistics, time
from pathlib import Path
from PIL import Image, ImageStat

ROOT=Path(__file__).resolve().parent/'cycles/v2'
SEED=20261031
TASK='acne04-color-matched-patch-presence-v2'
SYSTEM='你是皮膚影像研究助手。判斷局部影像中是否有可見的痘痘樣病灶，不診斷疾病。只輸出一個 JSON 物件，只有 lesion_present 欄位，值為 true 或 false。'
QUESTION='觀察這張局部影像，有可見的痘痘樣病灶嗎？'
CONFIGS=[{'name':'lora-r8-attention-mlp-hardneg','rank':8,'alpha':16,'lr':1e-4,'epochs':2,'target':'language attention and MLP','augmentation':'seeded horizontal flip, 50% of training exposures','loss':'boolean decision token only; no prompt, image, or JSON scaffold loss'}]

def digest(data):return hashlib.sha256(data).hexdigest()
def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(path)
def read_jsonl(path):return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
def status(phase,**kw):
    data={'phase':phase,'updated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),**kw}
    write_json(ROOT/'status.json',data);print(json.dumps(data,ensure_ascii=False),flush=True)
def dhash(im):
    small=im.convert('L').resize((9,8));p=list(small.getdata());n=0
    for y in range(8):
        for x in range(8):n=(n<<1)|int(p[y*9+x]>p[y*9+x+1])
    return n
def overlap(a,b):return max(a[0],b[0])<min(a[2],b[2]) and max(a[1],b[1])<min(a[3],b[3])

def prepare(train_parents=128,val_parents=24,test_parents=48):
    import pyarrow.parquet as pq
    target=ROOT/'data/frozen-v2'
    if (target/'dataset.json').exists():return json.loads((target/'dataset.json').read_text())
    target.mkdir(parents=True,exist_ok=True)
    rows=[];invalid=[]
    for p in sorted((ROOT/'data/raw').glob('*.parquet')):
        for batch in pq.ParquetFile(p).iter_batches(batch_size=32):
            for row in batch.to_pylist():
                try:
                    image=Image.open(io.BytesIO(row['image']['bytes'])).convert('RGB')
                    if min(image.size)<256:raise ValueError('short edge below 256')
                    b=row['lesion_boxes'];boxes=[]
                    for box in zip(b['xmin'],b['ymin'],b['xmax'],b['ymax']):
                        # Pascal VOC coordinates are 1-indexed inclusive.
                        x1,y1,x2,y2=box;box=(max(0,x1-1),max(0,y1-1),min(image.width,x2),min(image.height,y2))
                        if box[2]>box[0] and box[3]>box[1]:boxes.append(box)
                    if not boxes:raise ValueError('no valid boxes')
                    row.update(_hash=digest(image.tobytes()),_dhash=dhash(image),_width=image.width,_height=image.height,_boxes=boxes)
                    rows.append(row)
                except Exception as e:invalid.append({'file':row['file_name'],'reason':str(e)})
    # Cluster exact and low-distance perceptual duplicates before assigning crops.
    parent=list(range(len(rows)))
    def find(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    for i,r in enumerate(rows):
        for j in range(i):
            s=rows[j]
            if r['_hash']==s['_hash'] or (r['_dhash']^s['_dhash']).bit_count()<=3:
                a,b=find(i),find(j)
                if a!=b:parent[a]=b
    previous=json.loads((ROOT.parents[1]/'data/frozen-v1/dataset.json').read_text())
    excluded={r['group_id'] for r in read_jsonl(Path(previous['manifests']['test']))}
    groups={}
    for i,r in enumerate(rows):groups.setdefault(find(i),[]).append(r)
    buckets={'train':[],'val':[],'test':[]};promoted=0
    for group in groups.values():
        gid=min(r['_hash'] for r in group)
        if gid in excluded:continue  # keep the previous cycle's test entirely out of this cycle
        if any(r['split']=='test' for r in group):
            split='test';promoted+=sum(r['split']!='test' for r in group)
        else:split='val' if int(gid[:8],16)%5==0 else 'train'
        # One parent per near-duplicate group for this pilot.
        row=sorted(group,key=lambda r:r['file_name'])[0];row['_group']=gid;buckets[split].append(row)
    manifests={};counts={};selected_groups={}
    for split,limit in [('train',train_parents),('val',val_parents),('test',test_parents)]:
        bucket=sorted(buckets[split],key=lambda r:digest((str(SEED)+r['_group']).encode()))
        out=[];selected=[]
        for row in bucket:
            if len(selected)>=limit:break
            im=Image.open(io.BytesIO(row['image']['bytes'])).convert('RGB');w,h=im.size;short=min(w,h)
            rng=random.Random(int(row['_hash'][:16],16)^SEED)
            candidates=[b for b in row['_boxes'] if max(b[2]-b[0],b[3]-b[1])<=short*.35]
            if not candidates:continue
            b=rng.choice(candidates);side=max(128,int(short*.30),math.ceil(max(b[2]-b[0],b[3]-b[1])*2.5));side=min(side,short)
            cx=(b[0]+b[2])/2;cy=(b[1]+b[3])/2
            x=min(max(0,int(cx-side/2)),w-side);y=min(max(0,int(cy-side/2)),h-side)
            pos=(x,y,x+side,y+side);neg=None
            # Negative means no overlap with supplied boxes plus a margin;
            # it must never be interpreted as clinician-confirmed healthy skin.
            margin=max(4,int(short*.01))
            expanded=[(a-margin,b-margin,c+margin,d+margin) for a,b,c,d in row['_boxes']]
            color_image=im.resize((max(1,w//8),max(1,h//8)))
            def mean_color(box):
                scaled=tuple(int(v/8) for v in box)
                return ImageStat.Stat(color_image.crop(scaled).resize((16,16))).mean
            reference=mean_color(pos)
            matched=[]
            for _ in range(200):
                nx=rng.randint(0,w-side);ny=rng.randint(0,h-side);box=(nx,ny,nx+side,ny+side)
                if any(overlap(box,b) for b in expanded):continue
                color=mean_color(box)
                distance=sum((a-b)**2 for a,b in zip(reference,color))**.5
                if distance<=45:
                    spatial=((nx-x)**2+(ny-y)**2)**.5/max(w,h)
                    matched.append((distance+spatial*8,box))
            if matched:neg=min(matched,key=lambda item:item[0])[1]
            if neg is None:continue
            selected.append(row['_group'])
            for label,box in [(True,pos),(False,neg)]:
                item_id=row['_hash'][:20]+('-positive' if label else '-negative')
                crop=im.crop(box);crop.thumbnail((512,512));path=target/'patches'/split/(item_id+'.jpg');path.parent.mkdir(parents=True,exist_ok=True);crop.save(path,quality=95)
                out.append({'id':item_id,'group_id':row['_group'],'image':str(path),'lesion_present':label,'annotation_origin':'ACNE04 original author bounding boxes','label_derivation':'box-containing crop' if label else 'crop with no overlap with supplied boxes; not confirmed healthy','source_file':row['file_name'],'crop_box':box,'sha256':digest(path.read_bytes()),'native_crop_size':side,'source_split':row['split']})
        if len(selected)<limit:raise RuntimeError(f'Insufficient eligible parents for {split}: {len(selected)}/{limit}')
        manifest=target/(split+'.jsonl');manifest.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in out));manifests[split]=str(manifest);counts[split]={'parents':len(selected),'patches':len(out),'positive':sum(r['lesion_present'] for r in out),'negative':sum(not r['lesion_present'] for r in out),'sha256':digest(manifest.read_bytes())};selected_groups[split]=set(selected)
    assert not (selected_groups['train']&selected_groups['val'] or selected_groups['train']&selected_groups['test'] or selected_groups['val']&selected_groups['test'])
    meta={'task':TASK,'seed':SEED,'manifests':manifests,'counts':counts,'source_rows':len(rows)+len(invalid),'eligible_source_rows':len(rows),'rejected_sources':invalid,'near_duplicate_clusters':len(groups),'canonical_train_parents_promoted_to_test_to_prevent_leakage':promoted,'dedup':'decoded RGB sha256 and 64-bit dHash distance <=3; all crops stay with their parent cluster','subject_disjoint':False,'subject_limitation':'Reliable subject identifiers are unavailable; this is a parent-image/near-duplicate holdout, not proven person-disjoint.','scope_limitation':'Only box-derived local lesion presence. Other skin tags, anatomical region names, clinical diagnosis, and deployment safety are not evaluated.','negative_limitation':'Negatives are author-box-free crops matched to the positive crop mean RGB (distance <=45); matching is not a validated skin segmentation method and does not establish healthy skin. Annotation omissions may affect results.','previous_test_parent_groups_excluded':len(excluded),'negative_sampling':'200 candidates, require mean RGB distance <=45 to positive patch; minimize color distance plus spatial distance; all selection uses boxes, never model predictions.'}
    write_json(target/'dataset.json',meta)
    write_json(ROOT/'protocol.json',{'task':TASK,'frozen_dataset':str(target/'dataset.json'),'configs':CONFIGS,'selection':'validation macro-F1, then validation precision, then fewer parameters; test scores never select a winner','test_policy':'Only baseline and the validation-selected final candidate are evaluated on the locked test set. No further tuning using test errors in this version.','promotion_rule':{'minimum_test_macro_f1':.80,'minimum_test_precision':.85,'minimum_test_macro_f1_gain':.03,'minimum_test_valid_json_rate':.98},'promotion_scope':'Research task only; no automatic deployment to the four-tag skin diary.','stop_rule':'One predefined attention+MLP candidate, at most two epochs. Exclude every previous-cycle test parent group. Stop after this bounded cycle, even if no model improves.','primary_metric':'macro-F1 with invalid responses counted as errors','secondary_metrics':['positive precision','positive recall','balanced accuracy','valid JSON rate','p50/p95 inference latency'],'uncertainty':'95% parent-cluster bootstrap CI for paired test macro-F1 difference; small pilot, annotation-conditional and not clinical accuracy.'})
    return meta

def messages(image):return [{'role':'system','content':[{'type':'text','text':SYSTEM}]},{'role':'user','content':[{'type':'image','image':image},{'type':'text','text':QUESTION}]}]
def encode(processor,row,training=False):
    import torch
    with Image.open(row['image']) as im:image=im.convert('RGB')
    if training and row.get('_flip'):image=image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    prompt=messages(image)
    prefix=processor.apply_chat_template(prompt,tokenize=True,add_generation_prompt=True,return_dict=True,return_tensors='pt')
    if not training:return prefix
    full=prompt+[{'role':'assistant','content':[{'type':'text','text':json.dumps({'lesion_present':row['lesion_present']},separators=(',',':'))}]}]
    batch=processor.apply_chat_template(full,tokenize=True,add_generation_prompt=False,return_dict=True,return_tensors='pt')
    n=prefix['input_ids'].shape[1]
    if not torch.equal(prefix['input_ids'],batch['input_ids'][:,:n]):raise ValueError('prefix mismatch')
    labels=batch['input_ids'].clone();labels[:,:n]=-100;labels[batch['attention_mask']==0]=-100
    boolean_ids=[]
    for value in ('true','false'):
        token_ids=processor.tokenizer.encode(value,add_special_tokens=False)
        if len(token_ids)!=1:raise ValueError('Boolean tokenization changed; review loss mask')
        boolean_ids.extend(token_ids)
    keep=torch.zeros_like(labels,dtype=torch.bool)
    for token_id in boolean_ids:keep|=labels==token_id
    if int(keep.sum())!=1:raise ValueError('Expected exactly one supervised decision token')
    labels[~keep]=-100;batch['labels']=labels
    if batch['input_ids'].shape[1]>2048:raise ValueError('oversized example')
    return batch

def score(rows,predictions):
    tp=fp=tn=fn=invalid=0
    for row,p in zip(rows,predictions):
        y=row['lesion_present'];v=p.get('prediction')
        if v is None:
            invalid+=1
            if y:fn+=1
            else:fp+=1
        elif y and v:tp+=1
        elif y:fn+=1
        elif v:fp+=1
        else:tn+=1
    div=lambda a,b:a/b if b else 0.
    precision=div(tp,tp+fp);recall=div(tp,tp+fn);neg_recall=div(tn,tn+fp)
    f1=div(2*tp,2*tp+fp+fn);negative_f1=div(2*tn,2*tn+fp+fn)
    lat=sorted(p.get('seconds',0) for p in predictions)
    return {'n':len(rows),'tp':tp,'fp':fp,'tn':tn,'fn':fn,'invalid':invalid,'positive_precision':precision,'positive_recall':recall,'positive_f1':f1,'negative_f1':negative_f1,'macro_f1':(f1+negative_f1)/2,'balanced_accuracy':(recall+neg_recall)/2,'accuracy':div(tp+tn,len(rows)),'valid_json_rate':1-div(invalid,len(rows)),'latency_p50_seconds':statistics.median(lat) if lat else None,'latency_p95_seconds':lat[min(len(lat)-1,math.ceil(.95*len(lat))-1)] if lat else None}

def evaluate(model,processor,rows,path):
    import torch
    if path.exists():
        existing=read_jsonl(path);byid={p['id']:p for p in existing}
    else:byid={}
    model.eval();model.config.use_cache=True
    with path.open('a') as out:
        for i,row in enumerate(rows):
            if row['id'] in byid:continue
            batch=encode(processor,row);batch={k:v.to(model.device) for k,v in batch.items()};start=time.perf_counter()
            with torch.inference_mode():ids=model.generate(**batch,max_new_tokens=32,do_sample=False)
            if torch.cuda.is_available():torch.cuda.synchronize()
            elapsed=time.perf_counter()-start;text=processor.batch_decode(ids[:,batch['input_ids'].shape[1]:],skip_special_tokens=True)[0]
            try:
                cleaned=text.strip()
                if cleaned.startswith('```json') and cleaned.endswith('```'):cleaned=cleaned[7:-3].strip()
                obj=json.loads(cleaned)
                if set(obj)!={'lesion_present'} or type(obj['lesion_present']) is not bool:raise ValueError('schema')
                value=obj['lesion_present']
            except Exception:value=None
            p={'id':row['id'],'group_id':row['group_id'],'label':row['lesion_present'],'prediction':value,'raw_output':text,'seconds':round(elapsed,4)}
            byid[row['id']]=p;out.write(json.dumps(p,ensure_ascii=False)+'\n');out.flush()
            if (i+1)%16==0:print('EVAL_PROGRESS',path.name,i+1,len(rows),flush=True)
    predictions=[byid[r['id']] for r in rows];metrics=score(rows,predictions);write_json(path.with_suffix('.metrics.json'),metrics)
    return metrics,predictions

def train_candidate(base,processor,rows,val,config,out):
    import torch
    from peft import LoraConfig,get_peft_model
    torch.manual_seed(SEED);rng=random.Random(SEED)
    names=[n for n,_ in base.named_modules() if 'language_model' in n and n.endswith(('q_proj','k_proj','v_proj','o_proj','gate_proj','up_proj','down_proj'))]
    model=get_peft_model(base,LoraConfig(r=config['rank'],lora_alpha=config['alpha'],lora_dropout=.05,target_modules=names,bias='none',task_type='CAUSAL_LM'))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False});model.enable_input_require_grads()
    params=[v for n,v in model.named_parameters() if v.requires_grad]
    if any('visual' in n for n,v in model.named_parameters() if v.requires_grad):raise RuntimeError('vision must stay frozen')
    optimizer=torch.optim.AdamW(params,lr=config['lr'],weight_decay=.01)
    accum=4;steps=math.ceil(len(rows)/accum)*config['epochs'];warmup=max(1,math.ceil(steps*.05))
    schedule=lambda s:min(1.,(s+1)/warmup)*(.5+.5*math.cos(math.pi*max(0,s-warmup)/max(1,steps-warmup)))
    scheduler=torch.optim.lr_scheduler.LambdaLR(optimizer,schedule)
    out.mkdir(parents=True,exist_ok=True);history=[];best=-1.;step=0;stale=0
    for epoch in range(config['epochs']):
        order=list(rows);rng.shuffle(order);model.train();model.config.use_cache=False;losses=[];start=time.perf_counter()
        for offset in range(0,len(order),accum):
            micro=order[offset:offset+accum];optimizer.zero_grad(set_to_none=True)
            for row in micro:
                row=dict(row,_flip=rng.random()<.5)
                batch=encode(processor,row,True);loss=model(**{k:v.to(model.device) for k,v in batch.items()}).loss
                if not torch.isfinite(loss):raise RuntimeError('nonfinite loss')
                (loss/len(micro)).backward();losses.append(float(loss.detach()))
            norm=torch.nn.utils.clip_grad_norm_(params,1.)
            if not torch.isfinite(norm):raise RuntimeError('nonfinite gradient')
            optimizer.step();scheduler.step();step+=1
            if step%8==0:status('training',candidate=config['name'],epoch=epoch+1,step=step,total_steps=steps,mean_loss=sum(losses[-32:])/len(losses[-32:]))
        metrics,_=evaluate(model,processor,val,out/f'validation-epoch{epoch+1}.jsonl')
        entry={'epoch':epoch+1,'step':step,'mean_train_loss':sum(losses)/len(losses),'training_and_validation_seconds':time.perf_counter()-start,'validation':metrics};history.append(entry);write_json(out/'history.json',history)
        if metrics['macro_f1']>best+1e-8:
            best=metrics['macro_f1'];stale=0;model.save_pretrained(out/'best',safe_serialization=True);processor.save_pretrained(out/'best');write_json(out/'best-epoch.json',entry)
        else:stale+=1
        if stale>=1:break
    model.gradient_checkpointing_disable();base=model.unload();del model,optimizer,scheduler,params
    torch.cuda.empty_cache()
    best_info=json.loads((out/'best-epoch.json').read_text())
    result={**config,'best_epoch':best_info['epoch'],'validation':best_info['validation'],'trainable_parameters':sum(2*config['rank']*m.in_features for n,m in []),'history':history,'adapter':str(out/'best')}
    # Read the actual safetensors rather than estimate parameter counts.
    from safetensors import safe_open
    with safe_open(out/'best/adapter_model.safetensors',framework='pt',device='cpu') as tensors:result['trainable_parameters']=sum(tensors.get_tensor(k).numel() for k in tensors.keys())
    write_json(out/'result.json',result)
    return base,result

def bootstrap(rows,base,final,n=1000):
    groups=sorted(set(r['group_id'] for r in rows));idx={g:[i for i,r in enumerate(rows) if r['group_id']==g] for g in groups};rng=random.Random(SEED);values=[]
    for _ in range(n):
        sampled=[i for g in rng.choices(groups,k=len(groups)) for i in idx[g]]
        r=[rows[i] for i in sampled];values.append(score(r,[final[i] for i in sampled])['macro_f1']-score(r,[base[i] for i in sampled])['macro_f1'])
    values.sort();return {'method':'paired parent-cluster bootstrap','resamples':n,'lower_95':values[int(.025*n)],'upper_95':values[int(.975*n)]}

def render_report(summary):
    records=[('原始模型','validation',summary['baseline']['validation'])]
    records += [(r['name'],'validation',r['validation']) for r in summary['candidates']]
    if summary.get('test'):
        records += [('原始模型','locked test',summary['test']['baseline']),('驗證集選出的最佳版本','locked test',summary['test']['selected'])]
    with (ROOT/'comparison.csv').open('w',newline='') as f:
        fields=['model','split','n','macro_f1','positive_precision','positive_recall','balanced_accuracy','valid_json_rate','latency_p50_seconds','latency_p95_seconds','tp','fp','tn','fn','invalid'];writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for name,split,m in records:writer.writerow({'model':name,'split':split,**{k:m[k] for k in fields[2:]}})
    esc=lambda s:html.escape(str(s));trs=''.join('<tr><td>'+esc(name)+'</td><td>'+esc(split)+'</td><td>'+str(m['n'])+'</td>'+''.join(f'<td>{m[k]*100:.1f}%</td>' for k in ['macro_f1','positive_precision','positive_recall','valid_json_rate'])+f'<td>{m["latency_p50_seconds"]:.2f}s</td></tr>' for name,split,m in records)
    delta=summary.get('test',{}).get('macro_f1_difference');ci=summary.get('test',{}).get('paired_difference_ci95')
    verdict='尚在訓練與驗證中。' if delta is None else f'保留測試集 macro-F1 差異：{delta*100:+.1f} 個百分點。配對重抽樣 95% 區間：[{ci["lower_95"]*100:+.1f}, {ci["upper_95"]*100:+.1f}]。'
    if summary.get('test'):verdict+=' '+('通過預設研究門檻。' if summary['test']['research_gate_passed'] else '未通過預設研究門檻，保留原始模型。')
    counts=summary['dataset']['counts'];dataset='；'.join(f'{s}：{c["parents"]} 張來源影像／{c["patches"]} 個局部裁切' for s,c in counts.items())
    page='''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>BLINK 模型優化紀錄</title><style>body{margin:0;background:#faf8fb;color:#463e4a;font:15px/1.8 system-ui}main{max-width:1100px;margin:auto;padding:32px 20px}a{color:#876b93}h1{font-size:30px}h2{font-size:19px;margin-top:30px}.card{padding:22px;background:white;border:1px solid #e6dce9;border-radius:16px;margin:18px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}th,td{text-align:left;padding:12px;border-bottom:1px solid #eee}th{color:#876b93}.badge{color:#876b93;font-size:12px}p{margin:8px 0}li{margin:6px 0}.note{font-size:13px;color:#75697d}</style><main><a href="/skin-photo">← 返回照片分析</a><p class="badge">BLINK RESEARCH · 可重現的研究紀錄</p><h1>第二輪：更嚴格的局部辨識比較。</h1><div class="card"><strong>目前結論</strong><p>'''+esc(verdict)+'''</p><p class="note">這是局部「是否覆蓋資料集標註病灶」的研究任務，不是四類膚況或醫療診斷準確率。研究權重尚未替換正式照片分析模型。</p></div><h2>實測比較</h2><div class="card scroll"><table><thead><tr><th>版本</th><th>資料分組</th><th>局部數</th><th>Macro-F1</th><th>陽性精確率</th><th>陽性召回率</th><th>JSON 合格率</th><th>延遲中位數</th></tr></thead><tbody>'''+trs+'''</tbody></table></div><h2>資料與判斷標準</h2><div class="card"><p>'''+esc(dataset)+'''</p><ul><li>影像與標註：ACNE04 原作者框選，使用固定版本的公開鏡像；學術研究限定。陰性局部加入顏色與鄰近程度配對，降低明顯背景的捷徑。</li><li>先依來源影像及近似重複群組分組，再裁切。驗證集選版本，保留測試集只比較原始與最終版本。</li><li>無法解析的輸出也計為錯誤；不把拒答直接排除。</li><li>門檻：test macro-F1 ≥80%、陽性精確率 ≥85%、比原始 macro-F1 增加至少3個百分點、JSON合格率 ≥98%。</li><li>這輪只測試一個加入語言 MLP 與翻轉增強的 LoRA 候選，每個最多兩輪；前輪保留測試影像完全排除。</li></ul></div><h2>結果的適用範圍</h2><div class="card"><p>資料沒有可靠的人物識別碼，無法宣稱已做到不同人物完全分離。未覆蓋標註框的局部不等於經醫師確認的正常皮膚；可能包含背景或漏標。泛紅、色沉、脫屑及實際使用者照片尚未驗證。</p><p class="note">小型試驗的成績不能代表臨床表現。調參不使用保留測試集的錯誤案例；下一輪須另立版本與保留資料。</p><p>來源：<a href="https://github.com/xpwu95/LDL">原始研究</a> · <a href="https://huggingface.co/datasets/Layered-Labs/ACNE04">資料鏡像</a></p></div><p class="note">更新時間（UTC）：'''+esc(summary.get('updated_utc',''))+'''</p></main></html>'''
    (ROOT/'report.html').write_text(page)
    public=Path(__file__).resolve().parents[3]/'frontend/public/skin-research/v2'
    if public.parent.exists():public.mkdir(exist_ok=True);(public/'index.html').write_text(page)

def main():
    import torch
    from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
    from peft import PeftModel
    status('preparing_dataset');meta=prepare()
    train,val,test=[read_jsonl(Path(meta['manifests'][s])) for s in ['train','val','test']]
    results=ROOT/'runs/v2';results.mkdir(parents=True,exist_ok=True)
    model_path=Path(__file__).resolve().parent.parent/'model';status('loading_model')
    processor=AutoProcessor.from_pretrained(model_path,trust_remote_code=False,local_files_only=True)
    processor.image_processor.size={'shortest_edge':128*32*32,'longest_edge':384*32*32}
    base=Qwen3VLForConditionalGeneration.from_pretrained(model_path,torch_dtype=torch.bfloat16,attn_implementation='sdpa',trust_remote_code=False,use_safetensors=True,local_files_only=True).to('cuda:0').eval()
    status('baseline_validation');baseline,_=evaluate(base,processor,val,results/'baseline-validation.jsonl')
    summary={'task':TASK,'dataset':meta,'baseline':{'validation':baseline},'candidates':[],'updated_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'deployment':'research only; production four-tag model unchanged'}
    def save():
        summary['updated_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());write_json(ROOT/'summary.json',summary);render_report(summary)
    save()
    for config in CONFIGS:
        out=results/config['name']
        if (out/'result.json').exists():result=json.loads((out/'result.json').read_text())
        else:base,result=train_candidate(base,processor,train,val,config,out)
        summary['candidates'].append(result);save()
    winner=max(summary['candidates'],key=lambda r:(r['validation']['macro_f1'],r['validation']['positive_precision'],-r['trainable_parameters']))
    summary['selected_by_validation']=winner['name'];save()
    status('locked_test_baseline');base_metrics,base_predictions=evaluate(base,processor,test,results/'baseline-test.jsonl')
    status('locked_test_selected',candidate=winner['name']);final=PeftModel.from_pretrained(base,winner['adapter'],is_trainable=False)
    final_metrics,final_predictions=evaluate(final,processor,test,results/'selected-test.jsonl')
    delta=final_metrics['macro_f1']-base_metrics['macro_f1'];ci=bootstrap(test,base_predictions,final_predictions)
    gate=final_metrics['macro_f1']>=.80 and final_metrics['positive_precision']>=.85 and delta>=.03 and final_metrics['valid_json_rate']>=.98
    summary['test']={'baseline':base_metrics,'selected':final_metrics,'macro_f1_difference':delta,'paired_difference_ci95':ci,'research_gate_passed':gate,'statistically_clear_positive_difference':ci['lower_95']>0,'automatic_deployment':False}
    save();status('complete',selected=winner['name'],test_macro_f1=final_metrics['macro_f1'],difference=delta,research_gate_passed=gate)

if __name__=='__main__':
    try:main()
    except Exception as e:
        status('failed',error_type=type(e).__name__,error=str(e));raise

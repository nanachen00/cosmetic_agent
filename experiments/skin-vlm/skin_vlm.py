"""Local Qwen3-VL inference. Accepts only local images and local model weights."""
from __future__ import annotations
import argparse, hashlib, json, time
from pathlib import Path
from PIL import Image, ImageOps, ImageStat
from core import SYSTEM, envelope, parse

def open_image(path):
    path=Path(path)
    if path.stat().st_size>12*1024*1024: raise ValueError('Image exceeds 12 MiB')
    with Image.open(path) as raw:
        if raw.width*raw.height>25_000_000: raise ValueError('Image exceeds 25 MP')
        im=ImageOps.exif_transpose(raw).convert('RGB')
    st=ImageStat.Stat(im.convert('L')); reasons=[]
    if min(im.size)<224: reasons.append('too_small')
    if st.mean[0]<30: reasons.append('too_dark')
    if st.mean[0]>235: reasons.append('too_bright')
    if st.stddev[0]<5: reasons.append('low_contrast')
    return im, dict(width=im.width,height=im.height,brightness=round(st.mean[0],2),reasons=reasons,
                   note='Heuristic checks only; face/blur/lighting assessment not validated.')

def rejected(quality):
    return envelope(dict(image_scope='uncertain',observations=[],limitations=['照片品質不足，請重新拍攝。'],
                         follow_up_question='可以提供清楚且沒有濾鏡的照片嗎？'),quality)

def messages_for(image,note=''):
    return [{'role':'system','content':[{'type':'text','text':SYSTEM}]},
            {'role':'user','content':[{'type':'image','image':image},
             {'type':'text','text':'Observe this image for a skincare diary. Context: '+note[:500]}]}]

class SkinVLM:
    def __init__(self,model_path,adapter=None):
        import torch
        from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
        self.torch=torch
        path=Path(model_path)
        if not path.is_dir(): raise ValueError('Prepare a local model directory first')
        self.processor=AutoProcessor.from_pretrained(path,trust_remote_code=False,local_files_only=True)
        self.processor.image_processor.size={'shortest_edge':256*32*32,'longest_edge':768*32*32}
        self.model=Qwen3VLForConditionalGeneration.from_pretrained(path,
            torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
            attn_implementation='sdpa',trust_remote_code=False,use_safetensors=True,local_files_only=True)
        if adapter:
            from peft import PeftModel
            self.model=PeftModel.from_pretrained(self.model,adapter,is_trainable=False)
        self.model.to('cuda:0' if torch.cuda.is_available() else 'cpu').eval()

    def analyze(self,path,note=''):
        image,quality=open_image(path)
        if quality['reasons']: return rejected(quality)
        batch=self.processor.apply_chat_template(messages_for(image,note),tokenize=True,
              add_generation_prompt=True,return_dict=True,return_tensors='pt').to(self.model.device)
        start=time.perf_counter()
        with self.torch.inference_mode():
            ids=self.model.generate(**batch,max_new_tokens=400,do_sample=False,repetition_penalty=1.05)
        if self.torch.cuda.is_available(): self.torch.cuda.synchronize()
        text=self.processor.batch_decode(ids[:,batch['input_ids'].shape[1]:],skip_special_tokens=True)[0]
        try: result=envelope(parse(text),quality)
        except (ValueError,TypeError) as e:
            result=dict(status='invalid_model_output',error=str(e),requires_user_confirmation=True,
                        eligible_for_auto_save=False)
        return dict(**result,raw_output=text,inference_seconds=round(time.perf_counter()-start,3),
                    image_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())

def main():
    p=argparse.ArgumentParser();p.add_argument('image');p.add_argument('--model',default='model')
    p.add_argument('--adapter');p.add_argument('--note',default='');p.add_argument('--output',default='results/prediction.json')
    a=p.parse_args();_,q=open_image(a.image)
    result=rejected(q) if q['reasons'] else SkinVLM(a.model,a.adapter).analyze(a.image,a.note)
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(out.read_text())

if __name__=='__main__': main()

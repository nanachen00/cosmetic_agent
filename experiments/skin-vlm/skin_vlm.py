"""Local Qwen3-VL inference. Accepts only local images and local model weights."""
from __future__ import annotations
import argparse, hashlib, io, json, time, warnings
from pathlib import Path
from PIL import Image, ImageOps, ImageStat
from core import SYSTEM, envelope, parse

def open_image(path):
    path=Path(path)
    if path.stat().st_size>12*1024*1024: raise ValueError('Image exceeds 12 MiB')
    with path.open('rb') as source:data=source.read(12*1024*1024+1)
    return open_image_bytes(data)

def open_image_bytes(data):
    if not isinstance(data,bytes) or not 0<len(data)<=12*1024*1024:raise ValueError('Invalid image size')
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as raw:
            if raw.format not in {'JPEG','PNG','WEBP'}:raise ValueError('Unsupported image format')
            if raw.width*raw.height>25_000_000:raise ValueError('Image exceeds 25 MP')
            if getattr(raw,'n_frames',1)!=1:raise ValueError('Animated images are unsupported')
            raw.verify()
        with Image.open(io.BytesIO(data)) as raw:
            raw.load()
            if 'transparency' in raw.info or 'A' in raw.getbands() and raw.getextrema()[-1][0]<255:
                raise ValueError('Transparent images are unsupported')
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
        if not torch.cuda.is_available():
            raise RuntimeError('Qwen image inference requires the GPU service; configure its SSH-forwarded endpoint')
        is_rocm=bool(torch.version.hip)
        path=Path(model_path)
        if not path.is_dir(): raise ValueError('Prepare a local model directory first')
        self.processor=AutoProcessor.from_pretrained(path,trust_remote_code=False,local_files_only=True)
        self.processor.image_processor.size={'shortest_edge':256*32*32,'longest_edge':768*32*32}
        self.model=Qwen3VLForConditionalGeneration.from_pretrained(path,
            dtype=torch.bfloat16,
            attn_implementation='eager' if is_rocm else 'sdpa',trust_remote_code=False,use_safetensors=True,local_files_only=True)
        self.runtime={'attention':'eager' if is_rocm else 'sdpa','device':torch.cuda.get_device_name(0)}
        if is_rocm:
            from rocm_patch_projection import configure_rocm_patch_projection
            self.runtime['patch_projection']=configure_rocm_patch_projection(self.model)
        if adapter:
            from peft import PeftModel
            adapter_path=Path(adapter)
            if not adapter_path.is_dir() or not (adapter_path/'adapter_model.safetensors').is_file():
                raise ValueError('A local safetensors adapter is required; remote/pickle checkpoints are not accepted')
            self.model=PeftModel.from_pretrained(self.model,str(adapter_path),is_trainable=False,local_files_only=True)
        self.model.to('cuda:0').eval()

    def analyze(self,path,note=''):
        with Path(path).open('rb') as source:data=source.read(12*1024*1024+1)
        return self.analyze_bytes(data,note)

    def analyze_bytes(self,data,note=''):
        # Server-only numeric diagnostics; never include text, image bytes,
        # hashes, paths or user context, and never add these to the HTTP result.
        # Reached-budget flags describe observations, not a proven finish reason.
        self.last_diagnostics=dict(generated_tokens=0,eos_seen=False,eos_at_end=False,
            token_cap_reached=False,time_budget_reached=False,generate_seconds=0.,
            synchronize_seconds=0.,valid_output=False)
        image,quality=open_image_bytes(data)
        if quality['reasons']: return rejected(quality)
        batch=self.processor.apply_chat_template(messages_for(image,note),tokenize=True,
              add_generation_prompt=True,return_dict=True,return_tensors='pt').to(self.model.device)
        batch.pop('token_type_ids',None)
        start=time.perf_counter()
        try:
            with self.torch.inference_mode():
                ids=self.model.generate(**batch,max_new_tokens=400,max_time=60,do_sample=False,repetition_penalty=1.05)
        except Exception:
            seconds=time.perf_counter()-start
            self.last_diagnostics.update(generate_seconds=round(seconds,4),time_budget_reached=seconds>=60)
            raise
        generated_at=time.perf_counter()
        self.last_diagnostics.update(generate_seconds=round(generated_at-start,4),time_budget_reached=generated_at-start>=60)
        try:
            if self.torch.cuda.is_available(): self.torch.cuda.synchronize()
        finally:
            synchronized_at=time.perf_counter()
            self.last_diagnostics['synchronize_seconds']=round(synchronized_at-generated_at,4)
        generated=ids[:,batch['input_ids'].shape[1]:]
        eos=getattr(getattr(self.model,'generation_config',None),'eos_token_id',None)
        if eos is None:eos=getattr(self.processor.tokenizer,'eos_token_id',None)
        eos_ids=[eos] if isinstance(eos,int) else (eos or [])
        tokens=int(generated.shape[1])
        token_ids=generated[0].detach().cpu().tolist()  # Only after timed GPU synchronization.
        self.last_diagnostics.update(generated_tokens=tokens,
            eos_seen=any(value in eos_ids for value in token_ids),
            eos_at_end=bool(tokens and token_ids[-1] in eos_ids),
            token_cap_reached=tokens>=400,time_budget_reached=generated_at-start>=60,
            generate_seconds=round(generated_at-start,4),synchronize_seconds=round(synchronized_at-generated_at,4))
        text=self.processor.batch_decode(generated,skip_special_tokens=True)[0]
        try: result=envelope(parse(text),quality)
        except (ValueError,TypeError) as e:
            result=dict(status='invalid_model_output',error=str(e),requires_user_confirmation=True,
                        eligible_for_auto_save=False)
        self.last_diagnostics['valid_output']=result['status'] in {'needs_confirmation','needs_retake'}
        return dict(**result,raw_output=text,inference_seconds=round(time.perf_counter()-start,3),
                    image_sha256=hashlib.sha256(data).hexdigest())

def main():
    p=argparse.ArgumentParser();p.add_argument('image');p.add_argument('--model',default='model')
    p.add_argument('--adapter');p.add_argument('--note',default='');p.add_argument('--output',default='results/prediction.json')
    a=p.parse_args();_,q=open_image(a.image)
    result=rejected(q) if q['reasons'] else SkinVLM(a.model,a.adapter).analyze(a.image,a.note)
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(out.read_text())

if __name__=='__main__': main()

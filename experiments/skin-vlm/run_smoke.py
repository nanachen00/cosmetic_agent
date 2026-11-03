"""Actual local inference on two controls and one attributed public image."""
import json,time,urllib.request
from pathlib import Path
from skin_vlm import SkinVLM
from sample_controls import create

root=Path(__file__).resolve().parent
create(root);sample=root/'data/public_acne_ccby4.jpg'
url='https://upload.wikimedia.org/wikipedia/commons/f/f4/0601_Acne_Vulgaris.jpg'
source={'file':'public_acne_ccby4.jpg','source_url':'https://commons.wikimedia.org/wiki/File:0601_Acne_Vulgaris.jpg',
        'download_url':url,'author':'Dr.Gandikota Raghurama Rao','license':'CC-BY-4.0',
        'license_url':'https://creativecommons.org/licenses/by/4.0/','modified':False,
        'purpose':'Inference smoke example only, not independently reviewed ground truth or training data.'}
try:
    if not sample.exists():
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'BLINK-AI-Research/0.1'}),timeout=30) as r:
            sample.write_bytes(r.read(2*1024*1024))
    (root/'data/public_image_attribution.json').write_text(json.dumps(source,indent=2))
except Exception as e:
    print('Public sample unavailable:',type(e).__name__,str(e)[:200],flush=True)
start=time.perf_counter();engine=SkinVLM(str(root/'model'));loaded=time.perf_counter()-start
results=[]
for name in ('low_contrast_control.png','non_skin_control.png','public_acne_ccby4.jpg'):
    path=root/'data'/name
    if not path.exists(): continue
    result=engine.analyze(path);results.append({'image':name,'result':result})
    print(json.dumps(results[-1],ensure_ascii=False),flush=True)
out=root/'results';out.mkdir(exist_ok=True)
(out/'inference_smoke.json').write_text(json.dumps({'model':'Qwen/Qwen3-VL-4B-Instruct',
    'model_revision':(root/'model_revision.txt').read_text().strip(),'load_seconds':round(loaded,2),
    'clinical_validation':False,'results':results},ensure_ascii=False,indent=2))
print('INFERENCE_SMOKE_COMPLETE',flush=True)

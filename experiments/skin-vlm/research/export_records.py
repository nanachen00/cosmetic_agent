"""Export text records only. Excludes photos, model weights, and credentials."""
import hashlib,json,platform,sys
from pathlib import Path
from importlib.metadata import version,PackageNotFoundError

ROOT=Path(__file__).resolve().parent

def main():
    packages={}
    for name in ['torch','transformers','peft','accelerate','pyarrow','Pillow','safetensors']:
        try:packages[name]=version(name)
        except PackageNotFoundError:packages[name]=None
    environment={'python':sys.version,'platform':platform.platform(),'packages':packages,'base_model':'Qwen/Qwen3-VL-4B-Instruct','base_model_revision':'ebb281ec70b05090aa6165b016eac8ec08e71b17','hardware':'AMD Instinct MI300X; shared with existing services','user_uploaded_photos_used_for_training':False}
    (ROOT/'environment.json').write_text(json.dumps(environment,indent=2))
    exact=['summary.json','protocol.json','comparison.csv','report.html','all-comparisons.csv','final-comparison.json','comparison.html','environment.json','compatibility-probe.json','metric-tests.log','decision-mask-tests.log','training-negative-quality-audit.json','data/raw/provenance.json','data/raw/ORIGINAL_README.md','data/raw/MIRROR_README.md','data/frozen-v1/dataset.json','data/frozen-v1/train.jsonl','data/frozen-v1/val.jsonl','data/frozen-v1/test.jsonl','cycles/v2/summary.json','cycles/v2/protocol.json','cycles/v2/comparison.csv','cycles/v2/data/frozen-v2/dataset.json','cycles/v2/data/frozen-v2/train.jsonl','cycles/v2/data/frozen-v2/val.jsonl','cycles/v2/data/frozen-v2/test.jsonl']
    exact += ['cycle2-training-negative-audit.json','adapter-export.json']
    paths=[ROOT/name for name in exact]
    for run in [ROOT/'runs/v1',ROOT/'cycles/v2/runs/v2']:
        paths += [p for p in run.rglob('*') if p.is_file() and p.suffix in {'.json','.jsonl','.py'} and 'best' not in p.relative_to(run).parts]
    records={str(p.relative_to(ROOT)):p.read_text() for p in paths if p.is_file()}
    manifest={name:hashlib.sha256(text.encode()).hexdigest() for name,text in records.items()}
    records['record_hashes.json']=json.dumps(manifest,indent=2)
    destination=ROOT/'records-export.json';destination.write_text(json.dumps({'files':records,'photos_included':False,'weights_included':False},ensure_ascii=False))
    print(json.dumps({'files':len(records),'bytes':destination.stat().st_size,'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'photos_included':False}),flush=True)

if __name__=='__main__':main()

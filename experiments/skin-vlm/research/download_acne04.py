"""Download a pinned, public academic research mirror, verifying every shard."""
import argparse, concurrent.futures, hashlib, json, urllib.request, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REV = 'e375d8dac98b8562c5ab462d5f00ac011e8a47db'
REPO = 'Layered-Labs/ACNE04'

def get(url):
    with urllib.request.urlopen(url, timeout=90) as r:
        return r.read()

def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(4*1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'data/raw')
    raw=parser.parse_args().output;raw.mkdir(parents=True,exist_ok=True)
    tree=json.loads(get(f'https://huggingface.co/api/datasets/{REPO}/tree/{REV}?recursive=true'))
    (raw/'dataset_tree.json').write_text(json.dumps(tree,indent=2))
    (raw/'MIRROR_README.md').write_bytes(get(f'https://huggingface.co/datasets/{REPO}/raw/{REV}/README.md'))
    (raw/'ORIGINAL_README.md').write_bytes(get('https://raw.githubusercontent.com/xpwu95/LDL/master/README.md'))
    records=[]
    def download(item):
        name=item['path']; path=raw/Path(name).name
        expected=item['lfs']['oid']
        if not path.exists() or file_hash(path)!=expected:
            temp=path.with_suffix('.partial')
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(f'https://huggingface.co/datasets/{REPO}/resolve/{REV}/{name}?download=true',timeout=90) as src, temp.open('wb') as dst:
                        while chunk:=src.read(4*1024*1024):dst.write(chunk)
                    if temp.stat().st_size!=item['size']:raise ValueError('size mismatch')
                    if file_hash(temp)!=expected:raise ValueError('sha256 mismatch')
                    temp.replace(path);break
                except Exception:
                    if attempt==2:raise
                    time.sleep(2)
        record={'path':str(path),'source_path':name,'bytes':path.stat().st_size,'sha256':expected}
        print(json.dumps({'downloaded':name,'bytes':record['bytes']}),flush=True)
        return record
    files=[i for i in tree if i['type']=='file' and i['path'].endswith('.parquet')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:records=list(pool.map(download,files))
    (raw/'provenance.json').write_text(json.dumps({'repository':REPO,'revision':REV,'original_source':'https://github.com/xpwu95/LDL','license':'academic/research only; no commercial use without original author permission','labels':'original author annotations redistributed by mirror; not independently re-adjudicated','downloaded_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'files':records},indent=2))
    print('DOWNLOAD_VERIFIED',flush=True)

if __name__=='__main__':main()

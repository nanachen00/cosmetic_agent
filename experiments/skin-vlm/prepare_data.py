"""Create reproducible subject-level splits and detect exact-image leakage."""
from __future__ import annotations
import argparse,hashlib,json,random
from pathlib import Path
from core import load_rows

def assert_no_leakage(*groups):
    subjects,hashes,ids=set(),set(),set()
    for rows in groups:
        s={r['subject_id'] for r in rows};h={hashlib.sha256(Path(r['image']).read_bytes()).hexdigest() for r in rows}
        i={r['id'] for r in rows}
        if subjects&s or hashes&h or ids&i: raise ValueError('Subject/ID/image leaked across splits')
        subjects|=s;hashes|=h;ids|=i

def main():
    p=argparse.ArgumentParser();p.add_argument('manifest');p.add_argument('--out',default='data/splits')
    p.add_argument('--seed',type=int,default=42);a=p.parse_args();rows=load_rows(a.manifest)
    if any(r['label_origin']!='human_verified' for r in rows): raise ValueError('Reviewed labels required')
    subjects=sorted({r['subject_id'] for r in rows})
    if len(subjects)<3: raise ValueError('Need at least three independent subjects')
    random.Random(a.seed).shuffle(subjects);n=max(1,round(len(subjects)*.15))
    sets={'test':set(subjects[:n]),'val':set(subjects[n:2*n]),'train':set(subjects[2*n:])}
    splits={k:[r for r in rows if r['subject_id'] in s] for k,s in sets.items()}
    assert_no_leakage(*splits.values());out=Path(a.out)
    if out.exists() and any(out.iterdir()): raise ValueError('Preserve frozen splits; use empty output folder')
    out.mkdir(parents=True,exist_ok=True)
    for k,v in splits.items(): (out/(k+'.jsonl')).write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in v)+'\n')
    (out/'split_info.json').write_text(json.dumps({'seed':a.seed,'counts':{k:len(v) for k,v in splits.items()}},indent=2))
    print({k:len(v) for k,v in splits.items()})

if __name__=='__main__': main()

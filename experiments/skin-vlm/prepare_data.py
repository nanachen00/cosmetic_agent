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
    p.add_argument('--seed',type=int,default=42)
    p.add_argument('--allow-source-expert',action='store_true',help='Validated original expert-derived research labels only')
    a=p.parse_args();rows=load_rows(a.manifest)
    from data_review import require_reviewed_labels,group_source_images
    require_reviewed_labels(rows,a.allow_source_expert)
    expert=[row for row in rows if row['label_origin']=='source_expert_derived']
    if expert:group_source_images(expert)
    subjects=sorted({r['subject_id'] for r in rows})
    if len(subjects)<3: raise ValueError('Need at least three independent declared groups')
    random.Random(a.seed).shuffle(subjects);n=max(1,round(len(subjects)*.15))
    sets={'test':set(subjects[:n]),'val':set(subjects[n:2*n]),'train':set(subjects[2*n:])}
    splits={k:[r for r in rows if r['subject_id'] in s] for k,s in sets.items()}
    assert_no_leakage(*splits.values());out=Path(a.out)
    if out.exists() and any(out.iterdir()): raise ValueError('Preserve frozen splits; use empty output folder')
    out.mkdir(parents=True,exist_ok=True)
    for k,v in splits.items(): (out/(k+'.jsonl')).write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in v)+'\n')
    (out/'split_info.json').write_text(json.dumps({'seed':a.seed,'counts':{k:len(v) for k,v in splits.items()},
        'group_counts':{k:len({r['subject_id'] for r in v}) for k,v in splits.items()},
        'manifest_sha256':{k:hashlib.sha256((out/(k+'.jsonl')).read_bytes()).hexdigest() for k in splits},
        'subject_disjoint':all(r.get('subject_id_verified') is True for r in rows),
        'source_expert_images':len(expert),'research_only':bool(expert),
        'limitation':'Unverified source-image groups are not proven independent people.' if expert else 'Subject identities are declared by the supplied manifest.'},indent=2))
    print({k:len(v) for k,v in splits.items()})

if __name__=='__main__': main()

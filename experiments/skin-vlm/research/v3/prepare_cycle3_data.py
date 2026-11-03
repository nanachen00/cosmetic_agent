"""Create a fully new ACNE04 patch cohort, excluding every prior used group."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from PIL import Image,ImageStat

try:
    from .facial_research import write_json
except ImportError:
    from facial_research import write_json

SEED=20261101


def digest(value: bytes) -> str:return hashlib.sha256(value).hexdigest()
def dhash(image: Image.Image) -> int:
    pixels=list(image.convert("L").resize((9,8)).getdata());value=0
    for y in range(8):
        for x in range(8):value=(value<<1)|int(pixels[y*9+x]>pixels[y*9+x+1])
    return value
def overlap(a: Sequence[int],b: Sequence[int]) -> bool:
    return max(a[0],b[0])<min(a[2],b[2]) and max(a[1],b[1])<min(a[3],b[3])
def read_jsonl(path: Path) -> List[Dict[str,Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def prior_groups(manifests: Sequence[Path]) -> set:
    groups=set()
    for path in manifests:
        if path.is_file(): groups.update(x["group_id"] for x in read_jsonl(path))
    if not groups: raise ValueError("no historical groups loaded; refusal prevents accidental reuse")
    return groups


def load_rows(raw: Path) -> Tuple[List[Dict[str,Any]],List[Dict[str,str]]]:
    import pyarrow.parquet as pq
    rows=[];rejected=[]
    for path in sorted(raw.glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=32):
            for source in batch.to_pylist():
                try:
                    image=Image.open(io.BytesIO(source["image"]["bytes"])).convert("RGB")
                    if min(image.size)<256: raise ValueError("short edge below 256")
                    box_data=source["lesion_boxes"];boxes=[]
                    for values in zip(box_data["xmin"],box_data["ymin"],box_data["xmax"],box_data["ymax"]):
                        x1,y1,x2,y2=values;box=(max(0,x1-1),max(0,y1-1),min(image.width,x2),min(image.height,y2))
                        if box[2]>box[0] and box[3]>box[1]: boxes.append(box)
                    if not boxes: raise ValueError("no valid boxes")
                    row=dict(source);row.update(_bytes=source["image"]["bytes"],_hash=digest(image.tobytes()),
                        _dhash=dhash(image),_boxes=boxes,_width=image.width,_height=image.height);rows.append(row)
                except Exception as exc: rejected.append({"file":str(source.get("file_name","unknown")),"reason":str(exc)})
    if not rows: raise ValueError("no eligible parquet rows in "+str(raw))
    return rows,rejected


def cluster(rows: Sequence[Mapping[str,Any]]) -> Dict[int,List[Mapping[str,Any]]]:
    parent=list(range(len(rows)))
    def find(i: int) -> int:
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    for i,row in enumerate(rows):
        for j in range(i):
            other=rows[j]
            if row["_hash"]==other["_hash"] or (row["_dhash"]^other["_dhash"]).bit_count()<=3:
                a,b=find(i),find(j)
                if a!=b:parent[a]=b
    groups={}
    for i,row in enumerate(rows):groups.setdefault(find(i),[]).append(row)
    return groups


def crop_pair(row: Mapping[str,Any],group_id: str,split: str,output: Path) -> List[Dict[str,Any]]:
    image=Image.open(io.BytesIO(row["_bytes"])).convert("RGB");w,h=image.size;short=min(w,h)
    rng=random.Random(int(row["_hash"][:16],16)^SEED)
    candidates=[box for box in row["_boxes"] if max(box[2]-box[0],box[3]-box[1])<=short*.35]
    if not candidates:return []
    target=rng.choice(candidates);side=min(max(128,int(short*.30),math.ceil(max(target[2]-target[0],target[3]-target[1])*2.5)),short)
    cx=(target[0]+target[2])/2;cy=(target[1]+target[3])/2
    x=min(max(0,int(cx-side/2)),w-side);y=min(max(0,int(cy-side/2)),h-side);positive=(x,y,x+side,y+side)
    margin=max(4,int(short*.01));expanded=[(a-margin,b-margin,c+margin,d+margin) for a,b,c,d in row["_boxes"]]
    small=image.resize((max(1,w//8),max(1,h//8)))
    def colour(box: Sequence[int]) -> Sequence[float]:
        return ImageStat.Stat(small.crop(tuple(int(v/8) for v in box)).resize((16,16))).mean
    reference=colour(positive);matched=[]
    for _ in range(200):
        nx=rng.randint(0,w-side);ny=rng.randint(0,h-side);box=(nx,ny,nx+side,ny+side)
        if any(overlap(box,item) for item in expanded):continue
        distance=sum((a-b)**2 for a,b in zip(reference,colour(box)))**.5
        if distance<=45:matched.append((distance+math.hypot(nx-x,ny-y)/max(w,h)*8,box))
    if not matched:return []
    negative=min(matched,key=lambda x:x[0])[1];result=[]
    for label,box in ((True,positive),(False,negative)):
        item_id=row["_hash"][:20]+("-positive" if label else "-negative")
        path=output/"patches"/split/(item_id+".jpg");path.parent.mkdir(parents=True,exist_ok=True)
        patch=image.crop(box);patch.thumbnail((512,512));patch.save(path,quality=95)
        result.append({"id":item_id,"group_id":group_id,"image":str(path.resolve()),"lesion_present":label,
            "annotation_origin":"ACNE04 original author bounding boxes","label_derivation":"box-containing crop" if label else
            "crop with no overlap with supplied boxes; not clinician-confirmed healthy","source_file":row["file_name"],
            "source_severity":row.get("severity",row.get("level")),"crop_box":box,"sha256":digest(path.read_bytes())})
    return result


def main() -> None:
    p=argparse.ArgumentParser();p.add_argument("--raw",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--exclude-manifest",type=Path,action="append",required=True)
    p.add_argument("--train-parents",type=int,default=160);p.add_argument("--validation-parents",type=int,default=32)
    p.add_argument("--test-parents",type=int,default=64);a=p.parse_args()
    if (a.output/"dataset.json").exists():
        raise FileExistsError("frozen dataset already exists; use a new versioned output directory")
    forbidden=prior_groups(a.exclude_manifest);rows,rejected=load_rows(a.raw);groups=cluster(rows)
    eligible=[]
    for group in groups.values():
        group_id=min(x["_hash"] for x in group)
        if group_id not in forbidden:eligible.append((digest((str(SEED)+group_id).encode()),group_id,sorted(group,key=lambda x:x["file_name"])[0]))
    eligible.sort();limits={"test":a.test_parents,"validation":a.validation_parents,"train":a.train_parents};offset=0;counts={}
    for split in ("test","validation","train"):
        out=[];parents=0
        while parents<limits[split] and offset<len(eligible):
            _,group_id,row=eligible[offset];offset+=1;pair=crop_pair(row,group_id,split,a.output)
            if pair:out.extend(pair);parents+=1
        if parents<limits[split]:raise RuntimeError("insufficient eligible parents for "+split)
        manifest=a.output/("validation.jsonl" if split=="validation" else split+".jsonl")
        manifest.parent.mkdir(parents=True,exist_ok=True);manifest.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in out),encoding="utf-8")
        counts[split]={"parents":parents,"patches":len(out),"positive":sum(x["lesion_present"] for x in out),
                       "negative":sum(not x["lesion_present"] for x in out),"sha256":digest(manifest.read_bytes())}
    meta={"name":"acne04-cycle3-new-cohort","seed":SEED,"counts":counts,"source_rows":len(rows)+len(rejected),
          "eligible_source_rows":len(rows),"rejected_source_count":len(rejected),"historical_groups_excluded":len(forbidden),
          "forbidden_locked_test_groups":sorted(forbidden),"dedup":"decoded RGB SHA256 + dHash distance <=3",
          "subject_disjoint":False,"subject_limitation":"ACNE04 has no reliable subject IDs; group isolation is by source image and near-duplicate cluster.",
          "test_policy":"test.jsonl is frozen here but train_cycle3.py has no test input and cannot read it for selection.",
          "license":"academic/research use only; no automatic production deployment"}
    write_json(a.output/"dataset.json",meta);print(json.dumps(meta,ensure_ascii=False,indent=2))


if __name__=="__main__":main()

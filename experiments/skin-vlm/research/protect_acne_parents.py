"""Recover immutable original frames behind an ACNE04 protected crop manifest.

Reads only file names and image bytes from the pinned source parquet. No model
predictions, disease labels or lesion boxes participate in this protection.
"""
import argparse,hashlib,json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from research.download_acne04 import REPO,REV,file_hash
from research.v3.train_cycle3 import require_persistent_mount


def main():
    p=argparse.ArgumentParser()
    for name in ('raw','manifest','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--manifest-sha256',required=True);args=p.parse_args()
    mount=require_persistent_mount([args.raw,args.manifest,args.output])
    if file_hash(args.manifest)!=args.manifest_sha256:raise ValueError('Protected crop manifest changed')
    rows=[json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    parents={row['source_file'] for row in rows}
    if not parents or any(Path(name).name!=name for name in parents):raise ValueError('Invalid protected source names')
    provenance=json.loads((args.raw/'provenance.json').read_text())
    if provenance['repository']!=REPO or provenance['revision']!=REV:raise ValueError('Pinned ACNE04 source revision required')
    tree={row['path']:row for row in json.loads((args.raw/'dataset_tree.json').read_text()) if row['type']=='file'}
    shards=[]
    for record in provenance['files']:
        source=tree[record['source_path']];path=args.raw/Path(record['source_path']).name
        if not path.name.endswith('.parquet') or record['sha256']!=source['lfs']['oid'] or path.stat().st_size!=source['size'] or file_hash(path)!=source['lfs']['oid']:
            raise ValueError('Pinned source shard checksum mismatch')
        shards.append(path)
    if set(shards)!=set(args.raw.glob('*.parquet')):raise ValueError('Unregistered source parquet')
    partial=args.output.with_name(args.output.name+'.partial')
    if args.output.exists() or partial.exists():raise FileExistsError('Fresh source-parent output required')
    (partial/'images').mkdir(parents=True)
    import pyarrow.parquet as pq
    found={}
    for shard in shards:
        for batch in pq.ParquetFile(shard).iter_batches(batch_size=8,columns=['file_name','image']):
            for source in batch.to_pylist():
                name=source['file_name']
                if name not in parents:continue
                data=source['image']['bytes'];digest=hashlib.sha256(data).hexdigest()
                if name in found:
                    if found[name]['sha256']!=digest:raise ValueError('Ambiguous source file name')
                    continue
                target=partial/'images'/(digest+Path(name).suffix.lower());target.write_bytes(data)
                if file_hash(target)!=digest:raise RuntimeError('Parent write failed checksum verification')
                found[name]=dict(id='protected-acne04-parent-'+name,image=str(args.output/'images'/target.name),
                    sha256=digest,source_file=name,source_repository=REPO,source_revision=REV,
                    source_parquet=shard.name,label_origin='protected_source_parent',training_eligible=False,
                    source_crop_groups=sorted({row['group_id'] for row in rows if row['source_file']==name}))
    if set(found)!=parents:raise ValueError('Not all protected source parents were recovered')
    manifest=partial/'protected-parents.jsonl'
    manifest.write_text(''.join(json.dumps(found[name])+'\n' for name in sorted(found)))
    report=dict(parents=len(found),protected_crops=len(rows),repository=REPO,revision=REV,mount=mount,
        crop_manifest_sha256=args.manifest_sha256,parent_manifest_sha256=file_hash(manifest),
        extraction_code_sha256=file_hash(Path(__file__)),
        imported_code_sha256={name:file_hash(ROOT/name) for name in ('research/download_acne04.py','research/v3/train_cycle3.py')},
        source_files_sha256={path.name:file_hash(path) for path in [args.raw/'provenance.json',args.raw/'dataset_tree.json']},
        all_source_shards_verified=True,labels_or_predictions_used=False,training_eligible=False,
        scope='Source-frame leakage protection only; source groups are not verified people')
    (partial/'provenance.json').write_text(json.dumps(report,indent=2));partial.rename(args.output)
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()

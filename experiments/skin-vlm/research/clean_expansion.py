"""Immutable, auditable image cleaning and group-level holdouts.

Raw images remain unchanged. EXIF orientation/RGB conversion is inspected in
memory, exactly as for inference. No skin-colour enhancement or upscaling.
"""
from __future__ import annotations
import argparse,collections,hashlib,json,math,random,sys,warnings
from pathlib import Path
import numpy as np
from PIL import Image,ImageOps

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core import TAGS,validate
from data_review import require_reviewed_labels
from research.download_coco_controls import candidates as coco_candidates,target as coco_target


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def rows(path):return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2))
def jsonl(path,values):path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in values))


def image_features(path,expected,allow_small=False):
    raw=path.read_bytes()
    if not expected or hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('source_checksum_mismatch')
    if len(raw)>12*1024*1024:raise ValueError('image_exceeds_12_MiB')
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        with Image.open(path) as im:
            if im.format not in {'JPEG','PNG','WEBP'}:raise ValueError('unsupported_image_format')
            if min(im.size)<224 and not allow_small:raise ValueError('short_edge_below_224_no_upscale')
            if im.width*im.height>25_000_000:raise ValueError('image_exceeds_25_MP')
            if getattr(im,'n_frames',1)!=1:raise ValueError('animated_image')
            im.verify()
        with Image.open(path) as original:
            if 'A' in original.getbands() and original.getextrema()[-1][0]<255:raise ValueError('transparent_image')
            original.load();orientation=original.getexif().get(274,1);mode=original.mode
            image=ImageOps.exif_transpose(original).convert('RGB')
    rgb_hash=hashlib.sha256((str(image.size)+' RGB\0').encode()+image.tobytes()).hexdigest()
    gray=np.asarray(image.convert('L').resize((128,128)),dtype=np.float32)
    flags=[]
    if float(gray.std())<3 and not allow_small:raise ValueError('near_uniform_image')
    if float(gray.std())<5:flags.append('low_contrast_review')
    if float(gray.mean())<30:flags.append('underexposed_review')
    if float(gray.mean())>235:flags.append('overexposed_review')
    if float((gray<2).mean())>.55:flags.append('excessive_black_or_redaction')
    lap=-4*gray[1:-1,1:-1]+gray[:-2,1:-1]+gray[2:,1:-1]+gray[1:-1,:-2]+gray[1:-1,2:]
    if float(lap.var())<8:flags.append('low_detail_review')
    def bits(values):
        result=0
        for value in values:result=(result<<1)|int(value)
        return result
    def hashes(im):
        small=np.asarray(im.convert('L').resize((9,8)),dtype=np.float64)
        dh=bits((small[:,:-1]>small[:,1:]).ravel())
        square=np.asarray(im.convert('L').resize((32,32)),dtype=np.float64)
        basis=np.cos(np.pi*np.arange(8)[:,None]*(np.arange(32)[None,:]+.5)/32)
        coeff=(basis@square@basis.T).ravel()[1:]
        ph=bits(coeff>np.median(coeff))
        return dh,ph
    dh,ph=hashes(image);flip_dh,flip_ph=hashes(ImageOps.mirror(image))
    return dict(sha256=expected,normalized_rgb_sha256=rgb_hash,width=image.width,height=image.height,
        source_mode=mode,exif_orientation=orientation,normalization=['exif_transpose','RGB'],
        brightness=round(float(gray.mean()),3),contrast=round(float(gray.std()),3),
        laplacian_variance=round(float(lap.var()),3),dhash64=dh,phash63=ph,
        mirrored_dhash64=flip_dh,mirrored_phash63=flip_ph,quality_flags=flags)


def duplicate_clusters(records):
    parent=list(range(len(records)))
    def find(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    def union(i,j):
        a,b=find(i),find(j)
        if a!=b:parent[max(a,b)]=min(a,b)
    pairs=[];declared={}
    for i,a in enumerate(records):
        groups=[('source',a['declared_group'])] if a.get('declared_group') else []
        groups.extend(('manual',group) for group in a.get('manual_review_groups',[]))
        for group in groups:
            if group in declared:union(i,declared[group])
            else:declared[group]=i
        for j,b in enumerate(records[:i]):
            reason=None
            if a['sha256']==b['sha256'] or a['normalized_rgb_sha256']==b['normalized_rgb_sha256']:reason='exact'
            else:
                dh=min((a['dhash64']^b[key]).bit_count() for key in ('dhash64','mirrored_dhash64'))
                ph=min((a['phash63']^b[key]).bit_count() for key in ('phash63','mirrored_phash63'))
                if dh<=6 and ph<=4:reason='perceptual_or_mirrored'
            if reason:union(i,j);pairs.append(dict(a=a['id'],b=b['id'],reason=reason))
    clusters=collections.defaultdict(list)
    for i,row in enumerate(records):clusters[find(i)].append(row)
    return list(clusters.values()),pairs


def apply_manual_groups(records,document):
    """Conservative capture-context groups; never verified patient identities.

    Reviewers must bind every member to its original bytes. A missing member or
    changed file is an error, rather than a silently ignored grouping decision.
    """
    if document.get('verified_patient_identities') is not False:raise ValueError('Manual groups are not verified patient identities')
    by_id=collections.defaultdict(list)
    for row in records:by_id[row['id']].append(row)
    seen=set()
    for group in document.get('groups',[]):
        name=group.get('id');members=group.get('members',[])
        if not isinstance(name,str) or not name or name in seen or not group.get('reason'):raise ValueError('Invalid manual source group')
        seen.add(name)
        ids=[member.get('id') for member in members]
        if len(ids)<2 or len(set(ids))!=len(ids):raise ValueError('Manual group requires distinct image IDs')
        for member in members:
            matches=by_id.get(member['id'],[])
            if not matches or not member.get('sha256') or any(row['sha256']!=member['sha256'] for row in matches):
                raise ValueError('Manual group member missing or checksum changed: '+member['id'])
            for row in matches:row.setdefault('manual_review_groups',[]).append(name)


def resolve_image(path,manifest,path_maps):
    path=Path(path)
    if not path.is_absolute():return (manifest.parent/path).resolve()
    for source,destination in path_maps:
        if path.is_relative_to(source):return (destination/path.relative_to(source)).resolve()
    return path.resolve()


def object_validator(row,path,cache):
    annotation=path.parent.parent/'instances_val2017.json'
    if annotation not in cache:
        raw=annotation.read_bytes();metadata=json.loads(raw)
        cache[annotation]=(hashlib.sha256(raw).hexdigest(),{image['id']:(image,classes) for image,classes in coco_candidates(metadata)})
    expected,eligible=cache[annotation]
    if expected!=row.get('source_annotation_sha256') or row.get('source')!='https://cocodataset.org/':raise ValueError('COCO provenance mismatch')
    image,classes=eligible[row['source_image_id']]
    if row['target']!=coco_target() or row['source_categories']!=classes or path.name!=image['file_name']:raise ValueError('COCO source target mismatch')


def take_holdout(pool,size,minimum=12):
    """Source-label stratification only; never examines model predictions."""
    selected=[];counts=collections.Counter();remaining=list(pool)
    available=collections.Counter(tag for row in pool for tag in {o['tag'] for o in row['target']['observations']})
    while len(selected)<size:
        deficient={tag for tag in TAGS if counts[tag]<minimum}
        options=[]
        for i,row in enumerate(remaining):
            tags={o['tag'] for o in row['target']['observations']}
            if any(counts[tag]>=size-minimum for tag in tags):continue
            score=sum((minimum-counts[tag])/max(1,available[tag]) for tag in deficient&tags)
            options.append((score,-i,i,tags))
        if not options:raise ValueError('Cannot form a covered source-label holdout')
        _,_,i,tags=max(options);selected.append(remaining.pop(i));counts.update(tags)
    if any(counts[tag]<minimum or counts[tag]>size-minimum for tag in TAGS):raise ValueError('Source-label holdout coverage insufficient')
    return selected,remaining


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--existing',type=Path,action='append',default=[])
    p.add_argument('--new',type=Path,action='append',default=[])
    p.add_argument('--protect',type=Path,action='append',default=[])
    p.add_argument('--review-queue',type=Path,action='append',default=[])
    p.add_argument('--ocr',type=Path,action='append',default=[])
    p.add_argument('--manual-exclusions',type=Path)
    p.add_argument('--manual-groups',type=Path)
    p.add_argument('--path-map',action='append',default=[])
    p.add_argument('--output',type=Path,required=True);p.add_argument('--seed',type=int,default=20261116)
    args=p.parse_args()
    if args.output.exists() and any(args.output.iterdir()):raise FileExistsError('Use fresh output; never replace frozen splits')
    maps=[tuple(Path(value).resolve() for value in pair.split('=',1)) for pair in args.path_map]
    ocr={};input_records=[]
    for path in args.ocr:
        input_records.append(dict(role='ocr',path=str(path.resolve()),sha256=sha(path)))
        for row in rows(path):
            if not row.get('sha256'):raise ValueError('OCR must bind to image bytes')
            ocr[row['sha256']]=row
    manual={}
    if args.manual_exclusions:
        manual=json.loads(args.manual_exclusions.read_text())
        input_records.append(dict(role='visual_quality_exclusions',path=str(args.manual_exclusions.resolve()),sha256=sha(args.manual_exclusions)))
    records=[];audit=[];object_cache={};seen=set()
    for role,manifests in [('existing',args.existing),('new',args.new),('protected',args.protect),('review_pending',args.review_queue)]:
        for manifest in manifests:
            input_records.append(dict(role=role,path=str(manifest.resolve()),sha256=sha(manifest)))
            source_rows=rows(manifest)
            for row in source_rows:row['image']=str(resolve_image(row['image'],manifest,maps))
            expert=[row for row in source_rows if role in {'existing','new'} and row.get('label_origin')=='source_expert_derived']
            require_reviewed_labels(expert,True)
            for original in source_rows:
                row=dict(original);path=Path(row['image']);identity=(role,row['id'])
                if identity in seen:raise ValueError('Duplicate ID within input role: '+str(identity))
                seen.add(identity)
                entry=dict(id=row['id'],role=role,image=str(path),status='pending',reasons=[])
                try:
                    expected=row.get('image_sha256') or row.get('sha256')
                    features=image_features(path,expected,allow_small=role=='protected')
                    if role in {'existing','new'}:
                        if row.get('usage_permitted') is not True or not row.get('license'):raise ValueError('Source use permission missing')
                        validate(row['target'])
                        if row['label_origin']=='source_object_derived':object_validator(row,path,object_cache)
                        elif row['label_origin']!='source_expert_derived':raise ValueError('Only verified source labels enter gold splits')
                    if role!='protected':
                        decision=manual.get(row['id'])
                        if decision:
                            if decision['sha256']!=expected:raise ValueError('Visual exclusion is attached to different image bytes')
                            entry['reasons'].extend(decision['reasons'])
                        check=ocr.get(expected)
                        if not check or check.get('complete') is not True or check.get('policy_version')!=2:entry['reasons'].append('OCR_missing_or_failed')
                        elif check.get('condition_token_hits'):entry['reasons'].append('condition_text_label_leakage')
                        entry['reasons'].extend(features['quality_flags'])
                    entry.update(features)
                    entry['declared_group']=row.get('group_id') if role=='review_pending' else row.get('subject_id') if row.get('subject_id_verified') else None
                    entry['fresh']=role=='new';entry['row']=row
                    records.append(entry)
                except (OSError,ValueError,KeyError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc:
                    if role=='protected':raise ValueError('Cannot audit protected image: '+str(path)) from exc
                    entry.update(status='rejected',reasons=[str(exc)])
                audit.append(entry)
    if args.manual_groups:
        input_records.append(dict(role='manual_capture_context_groups',path=str(args.manual_groups.resolve()),sha256=sha(args.manual_groups)))
        apply_manual_groups(records,json.loads(args.manual_groups.read_text()))
    clusters,pairs=duplicate_clusters(records);eligible=[];review=[]
    for cluster in clusters:
        group='image-cluster-'+min(row['sha256'] for row in cluster)
        protected=any(row['role']=='protected' for row in cluster)
        gold=[row for row in cluster if row['role'] in {'existing','new'}]
        signatures={json.dumps(row['row']['target'],ensure_ascii=False,sort_keys=True) for row in gold}
        for row in cluster:row['cluster_id']=group
        if protected:
            for row in cluster:
                row['status']='protected' if row['role']=='protected' else 'quarantined'
                if row['role']!='protected':row['reasons'].append('matches_prior_holdout')
            continue
        if len(signatures)>1:
            for row in cluster:row['status']='quarantined';row['reasons'].append('duplicate_source_label_conflict')
            continue
        good=[row for row in gold if not row['reasons']]
        winner=min(good,key=lambda row:(row['fresh'],row['id'])) if good else None
        pending=[row for row in cluster if row['role']=='review_pending']
        pending_representative=min(pending,key=lambda row:row['id']) if pending else None
        for row in cluster:
            if row['role']=='review_pending':
                row['status']='review_pending' if not row['reasons'] else 'quarantined'
                if row is not pending_representative:
                    hash_related=any(pair['a']==row['id'] or pair['b']==row['id'] for pair in pairs)
                    row['status']='duplicate' if hash_related else 'quarantined'
                    row['reasons'].append('duplicate_review_source_image' if hash_related else 'related_capture_context_representative_only')
                review.append(row)
            elif row is winner:
                row['status']='accepted';item=dict(row['row'],subject_id=group,subject_id_verified=False,
                    group_unit='source image / perceptual and mirrored duplicate cluster',
                    cleaning_sha256=row['normalized_rgb_sha256'],fresh_source_image=row['fresh'])
                eligible.append(item)
            elif row['reasons']:row['status']='quarantined'
            else:row['status']='duplicate';row['reasons'].append('same_image_cluster')
    gold=[row for row in eligible if row['label_origin']=='source_expert_derived']
    controls=[row for row in eligible if row['label_origin']=='source_object_derived']
    fresh=[row for row in gold if row['fresh_source_image']];old=[row for row in gold if not row['fresh_source_image']]
    rng=random.Random(args.seed);rng.shuffle(fresh);rng.shuffle(controls)
    if len(fresh)<160:raise ValueError('Need at least 160 cleaned NEW expert images for independent new holdouts')
    val_n=test_n=64
    val,fresh=take_holdout(fresh,val_n);test,fresh=take_holdout(fresh,test_n)
    splits={'val':val,'test':test,'train':old+fresh}
    c=min(16,len(controls)//5)
    splits['val']+=controls[:c];splits['test']+=controls[c:2*c];splits['train']+=controls[2*c:]
    for name,items in splits.items():
        rng.shuffle(items)
        if name in {'val','test'}:
            counts={tag:sum(tag in {o['tag'] for o in row['target']['observations']} for row in items if row['label_origin']=='source_expert_derived') for tag in TAGS}
            if any(n<10 or n>val_n-10 for n in counts.values()):raise ValueError('Insufficient expert positive/negative holdout coverage: '+name)
    group_sets=[{row['subject_id'] for row in items} for items in splits.values()]
    if any(a&b for i,a in enumerate(group_sets) for b in group_sets[:i]):raise AssertionError('Cross-split duplicate cluster')
    args.output.mkdir(parents=True,exist_ok=True)
    for name,items in splits.items():jsonl(args.output/(name+'.jsonl'),items)
    for item in audit:item.pop('row',None)
    jsonl(args.output/'image-audit.jsonl',audit);write(args.output/'duplicate-pairs.json',pairs)
    coverage={name:dict(images=len(items),source_expert_images=sum(row['label_origin']=='source_expert_derived' for row in items),
        object_controls=sum(row['label_origin']=='source_object_derived' for row in items),
        supplied_skin_tone_counts=dict(collections.Counter(str(row.get('source_skin_tone','unknown')) for row in items if row['label_origin']=='source_expert_derived')),
        tags={tag:sum(tag in {o['tag'] for o in row['target']['observations']} for row in items) for tag in sorted(TAGS)}) for name,items in splits.items()}
    report=dict(status='frozen_clean_splits',seed=args.seed,counts=coverage,
        manifest_sha256={name:sha(args.output/(name+'.jsonl')) for name in splits},
        input_manifests=input_records,status_counts=dict(collections.Counter(row['status'] for row in audit)),
        reason_counts=dict(collections.Counter(reason for row in audit for reason in row['reasons'])),
        duplicate_pairs=len(pairs),source_image_clusters=len(clusters),subject_disjoint=False,
        cleaning_code_sha256=sha(Path(__file__)),normalization='EXIF transpose/RGB in memory; immutable originals; no colour correction, upscaling or auto crop',
        split_method='New source-image groups only for val/test; source-label stratification with >=12 positive and negative per tag, independent of predictions; prior training stays training',
        label_policy='Expert source appearance labels and source-object controls only; AI/pending labels never validation or test gold',
        augmentation='None during cleaning; train-only horizontal mirroring permitted in the frozen training protocol',
        review_queue_not_used_for_training=sum(row['role']=='review_pending' for row in audit),
        protected_holdout_entries=sum(row['role']=='protected' for row in audit),
        limitations=['Source-image groups are not verified unique people.','Clinical source images do not establish phone-selfie generalization.'],
        research_only=True,automatic_deployment=False)
    write(args.output/'split_info.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

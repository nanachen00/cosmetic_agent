"""Bounded COCO non-skin controls; retain original per-image CC BY/SA rights.

No people/animal annotations are allowed. These are object-source controls,
not dermatologist labels or proof that every pixel contains no human skin.
"""
from __future__ import annotations
import argparse,collections,hashlib,io,json,time,urllib.request,zipfile
from pathlib import Path
from PIL import Image

SOURCE='https://cocodataset.org/'
ARCHIVE='https://s3.amazonaws.com/images.cocodataset.org/annotations/annotations_trainval2017.zip'
BLOCKED={'person','bird','cat','dog','horse','sheep','cow','elephant','bear','zebra','giraffe'}


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()


def candidates(metadata):
    categories={row['id']:row['name'] for row in metadata['categories']};byid=collections.defaultdict(list)
    for row in metadata['annotations']:byid[row['image_id']].append(row)
    rows=[]
    for image in metadata['images']:
        annotations=byid[image['id']];names={categories[row['category_id']] for row in annotations}
        if image['license'] not in {4,5} or min(image['width'],image['height'])<224 or not names or names&BLOCKED:continue
        if max(row['area'] for row in annotations)/(image['width']*image['height'])<.15:continue
        rows.append((image,sorted(names)))
    return sorted(rows,key=lambda row:hashlib.sha256(str(row[0]['id']).encode()).hexdigest())


def target():
    return dict(image_scope='non_skin',observations=[],limitations=['此照片無法用於觀察膚況。'],follow_up_question='可以提供清楚的皮膚照片嗎？')


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cache',type=Path,required=True);p.add_argument('--max-images',type=int,default=96);a=p.parse_args()
    if not 1<=a.max_images<=128:raise ValueError('Control budget must be 1..128')
    if (a.output/'provenance.json').exists():raise FileExistsError('Frozen control snapshot exists')
    a.output.mkdir(parents=True,exist_ok=True);a.cache.mkdir(parents=True,exist_ok=True)
    archive=a.cache/'annotations_trainval2017.zip'
    if not archive.exists():
        partial=archive.with_suffix('.partial')
        for attempt in range(3):
            try:
                count=0
                with urllib.request.urlopen(ARCHIVE,timeout=60) as source,partial.open('wb') as out:
                    expected=int(source.headers['Content-Length']) if source.headers.get('Content-Length') else None
                    if expected and expected>300*1024*1024:raise ValueError('Unexpected annotation archive size')
                    while block:=source.read(4*1024*1024):
                        count+=len(block)
                        if count>300*1024*1024:raise ValueError('Annotation download exceeds budget')
                        out.write(block)
                    if expected and count!=expected:raise OSError('Truncated annotation download')
                partial.replace(archive);break
            except OSError:
                if attempt==2:raise
                time.sleep(2**attempt)
    with zipfile.ZipFile(archive) as z:data=z.read('annotations/instances_val2017.json')
    metadata=json.loads(data);annotation=a.output/'instances_val2017.json';annotation.write_bytes(data)
    annotation_hash=hashlib.sha256(data).hexdigest();licenses={row['id']:row for row in metadata['licenses']}
    rows=[];rejected=[]
    for image,classes in candidates(metadata):
        if len(rows)>=a.max_images:break
        name=image['file_name']
        if Path(name).name!=name:raise ValueError('Unexpected source image path')
        url='https://s3.amazonaws.com/images.cocodataset.org/val2017/'+name
        try:
            path=a.output/'images'/name
            if path.exists():raw=path.read_bytes()
            else:
                for attempt in range(3):
                    try:
                        with urllib.request.urlopen(url,timeout=30) as source:raw=source.read(12*1024*1024+1)
                        break
                    except OSError:
                        if attempt==2:raise
                        time.sleep(attempt+1)
            if len(raw)>12*1024*1024:raise ValueError('Image size budget exceeded')
            with Image.open(io.BytesIO(raw)) as picture:
                picture.verify()
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format!='JPEG' or picture.size!=(image['width'],image['height']):raise ValueError('Image differs from source metadata')
                picture.load()
            path.parent.mkdir(exist_ok=True)
            if not path.exists():path.write_bytes(raw)
            license_=licenses[image['license']]
            rows.append(dict(id='coco2017-'+str(image['id']),subject_id='source-coco2017-'+str(image['id']),subject_id_verified=False,
                image='images/'+name,source=SOURCE,license=license_['name'],license_url=license_['url'],usage_permitted=True,
                research_only=True,label_origin='source_object_derived',source_image_url=url,flickr_url=image.get('flickr_url'),
                source_image_id=image['id'],source_annotation_sha256=annotation_hash,image_sha256=hashlib.sha256(raw).hexdigest(),
                source_categories=classes,target=target()))
            print(json.dumps({'downloaded':len(rows),'budget':a.max_images}),flush=True)
        except Exception as exc:rejected.append({'source_image_id':image['id'],'error':str(exc)})
    (a.output/'manifest.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
    (a.output/'provenance.json').write_text(json.dumps(dict(source=SOURCE,archive_url=ARCHIVE,
        archive_sha256=sha(archive),annotation_sha256=annotation_hash,checksum_type='locally measured SHA-256; not a vendor-published digest',
        downloaded_images=len(rows),license_selection=[licenses[4],licenses[5]],rejected=rejected,
        label_scope='Non-skin object-source controls; not skin feature gold or pixel-level no-skin proof',
        automatic_deployment=False),indent=2))


if __name__=='__main__':main()

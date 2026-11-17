"""Bounded research intake of original expert concepts and Atlas images.

Only the original Atlas host, whose terms explicitly permit non-commercial
use with attribution, is used. Original Fitzpatrick image MD5s must match;
SHA-256s are recorded for every accepted image. Derived appearance labels are
kept distinct from locally reviewed app labels. No automatic deployment.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

from PIL import Image,ImageOps

CONCEPTS='https://skincon-dataset.github.io/files/annotations_fitzpatrick17k.csv'
METADATA='https://raw.githubusercontent.com/mattgroh/fitzpatrick17k/main/fitzpatrick17k.csv'
FITZ_TERMS='https://raw.githubusercontent.com/mattgroh/fitzpatrick17k/main/README.md'
ATLAS_TERMS='https://atlasdermatologico.com.br/'
MAPPING={
    'acne_like_bumps':('Papule','Comedo','Pustule'),
    'redness':('Erythema',),
    'dark_marks':('Brown(Hyperpigmentation)',),
    'flaking':('Scale',),
}
EVIDENCE={'acne_like_bumps':'可見小型丘疹或膿皰樣凸起。','redness':'局部皮膚呈現泛紅色澤。',
          'dark_marks':'可見較周圍皮膚深的棕色色澤。','flaking':'表面可見片狀或細碎的脫屑。'}


def get(url,limit=12*1024*1024,atlas_only=False):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url,timeout=20) as response:
                if atlas_only and (urllib.parse.urlparse(response.url).hostname or '').removeprefix('www.')!='atlasdermatologico.com.br':
                    raise ValueError('Image redirected outside the original licensed host')
                data=response.read(limit+1)
            if len(data)>limit:raise ValueError('Download exceeds per-object budget')
            return data
        except (OSError,TimeoutError):
            if attempt==2:raise
            time.sleep(1+attempt)


def digest(data):return hashlib.sha256(data).hexdigest()
def appearance_tags(row):
    if row.get('Do not consider this image')!='0':raise ValueError('Source experts excluded this image')
    for columns in MAPPING.values():
        if any(row.get(column) not in {'0','1'} for column in columns):raise ValueError('Unknown source concept label')
    return [tag for tag,columns in MAPPING.items() if any(row[column]=='1' for column in columns)]


def target_for(tags):
    return {'image_scope':'skin_closeup','observations':[{'tag':tag,'region':'unknown','evidence':EVIDENCE[tag]} for tag in tags],
            'limitations':['光線、角度與影像品質可能影響判讀；此結果僅描述可見外觀。'],
            'follow_up_question':'可以補充想記錄的部位與近期外觀變化嗎？'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--max-images',type=int,default=160)
    parser.add_argument('--exclude-manifest',type=Path,action='append',default=[])
    args=parser.parse_args()
    if not 0<=args.max_images<=512:raise ValueError('Image budget must be 0..512')
    if (args.output/'provenance.json').exists():raise FileExistsError('Completed snapshot exists')
    args.output.mkdir(parents=True,exist_ok=True)
    sources={}
    for name,url in (('skincon.csv',CONCEPTS),('fitzpatrick.csv',METADATA),('FITZ-README.md',FITZ_TERMS),('ATLAS-terms.html',ATLAS_TERMS)):
        data=get(url);(args.output/name).write_bytes(data);sources[name]={'url':url,'sha256':digest(data),'bytes':len(data)}
    terms=(args.output/'ATLAS-terms.html').read_text(errors='replace').lower()
    if 'free to use for non-comercial' not in terms:raise ValueError('Atlas source permission text changed; review before ingestion')
    concepts={row['ImageID'].rsplit('.',1)[0]:row for row in csv.DictReader((args.output/'skincon.csv').open())
              if row['Do not consider this image']=='0'}
    excluded=set();exclusion_records=[]
    for path in args.exclude_manifest:
        data=path.read_bytes();records=[json.loads(line) for line in data.decode().splitlines() if line.strip()]
        for row in records:
            value=row.get('source_image_md5')
            if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value):raise ValueError('Invalid exclusion source ID')
            excluded.add(value)
        exclusion_records.append({'path':str(path.resolve()),'sha256':digest(data),'rows':len(records)})
    candidates=[]
    for row in csv.DictReader((args.output/'fitzpatrick.csv').open()):
        parsed=urllib.parse.urlparse(row['url'])
        if parsed.hostname and parsed.hostname.removeprefix('www.')=='atlasdermatologico.com.br' and row['md5hash'] in concepts and row['md5hash'] not in excluded:
            row=dict(row);row['tags']=appearance_tags(concepts[row['md5hash']]);candidates.append(row)
    # Greedily balance positive concepts and explicit all-four-negative controls.
    remaining=sorted(candidates,key=lambda row:digest(row['md5hash'].encode()));selected=[];counts=Counter()
    while remaining:
        def priority(row):
            return min(counts[tag] for tag in (row['tags'] or ['none_of_four']))
        index=min(range(len(remaining)),key=lambda i:priority(remaining[i]))
        row=remaining.pop(index);selected.append(row);counts.update(row['tags'] or ['none_of_four'])
    rows=[];rejected=[]
    for source in selected:
        if len(rows)>=args.max_images:break
        name=source['md5hash'];url=source['url'].replace('http://','https://',1)
        try:
            if not re.fullmatch('[0-9a-f]{32}',name):raise ValueError('Invalid original image identifier')
            image_path=args.output/'images'/(name+'.jpg')
            data=image_path.read_bytes() if image_path.exists() else get(url,atlas_only=True)
            if hashlib.md5(data).hexdigest()!=name:raise ValueError('Original source image MD5 mismatch')
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {'JPEG','PNG'}:raise ValueError('Only source JPEG/PNG images accepted')
                if min(image.size)<224 or image.width*image.height>25_000_000:raise ValueError('Image dimensions outside research quality bounds')
                image=ImageOps.exif_transpose(image).convert('RGB')
                rgb_sha256=digest(image.tobytes())
                small=list(image.convert('L').resize((9,8)).getdata());dhash=0
                for y in range(8):
                    for x in range(8):dhash=(dhash<<1)|int(small[y*9+x]>small[y*9+x+1])
            image_path.parent.mkdir(parents=True,exist_ok=True)
            if not image_path.exists():image_path.write_bytes(data)
            rows.append({'id':'skincon-'+name,'subject_id':'unverified-source-image-'+name,'subject_id_verified':False,
                         'image':'images/'+name+'.jpg','source':'https://skincon-dataset.github.io/',
                         'license':'Fitzpatrick17k CC BY-NC-SA 3.0; Atlas non-commercial use with attribution',
                         'license_record':['FITZ-README.md','ATLAS-terms.html'],'usage_permitted':True,'research_only':True,
                         'label_origin':'source_expert_derived','source_annotation_sha256':sources['skincon.csv']['sha256'],
                         'source_annotation_url':CONCEPTS,'source_image_url':url,'source_image_md5':name,
                         'image_sha256':digest(data),'rgb_sha256':rgb_sha256,'dhash64':dhash,
                         'source_concepts':{column:concepts[name][column] for columns in MAPPING.values() for column in columns},
                         'source_skin_tone':source.get('fitzpatrick_scale'),'device_id':None,'captured_date':None,
                         'locally_readjudicated':False,'target':target_for(source['tags'])})
            print(json.dumps({'downloaded':len(rows),'budget':args.max_images}),flush=True)
        except Exception as exc:rejected.append({'source_id':name,'url':url,'reason':str(exc)})
        time.sleep(.25)
    (args.output/'manifest.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
    coverage=Counter(o['tag'] for row in rows for o in row['target']['observations'])
    report={'source':'SkinCon expert concepts / Fitzpatrick17k / original Atlas images only','research_only':True,
            'excluded_source_images':len(excluded),'exclusion_manifests':exclusion_records,
            'automatic_deployment':False,'downloaded_images':len(rows),'source_candidates':len(candidates),
            'mapped_positive_counts':dict(coverage),'empty_observation_controls':sum(not row['target']['observations'] for row in rows),
            'source_files':sources,'rejected':rejected,'mapping':MAPPING,
            'limitations':['Source clinical concepts are mapped appearance labels; no local expert re-adjudication.',
                           'No reliable patient IDs, capture dates or devices; source-image and near-duplicate grouping required.',
                           'Clinical close-ups differ from phone face photos; retain a separate phone-photo validation set.',
                           'Acne-like bumps are morphology, not a diagnosis; no severity or treatment labels.']}
    (args.output/'provenance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({key:report[key] for key in ('downloaded_images','mapped_positive_counts','empty_observation_controls')},indent=2))


if __name__=='__main__':main()

"""Keep original expert-derived research labels distinct from local reviews."""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path
import urllib.parse

SOURCE='https://skincon-dataset.github.io/'
ANNOTATIONS=SOURCE+'files/annotations_fitzpatrick17k.csv'
ANNOTATION_SHA256='ec0f1ab9b5013f0ec410aa5e978d49da7c3990d8631d1b7564f5c0e153735692'


def require_reviewed_labels(rows,allow_source_expert=False):
    from research.download_skincon import appearance_tags,target_for
    tables={}
    for row in rows:
        if row['label_origin']=='human_verified':continue
        if not allow_source_expert or row['label_origin']!='source_expert_derived':
            raise ValueError('Reviewed labels required; pending/AI labels are not training gold')
        if row.get('source')!=SOURCE or row.get('source_annotation_url')!=ANNOTATIONS \
                or row.get('source_annotation_sha256')!=ANNOTATION_SHA256 or row.get('research_only') is not True:
            raise ValueError('Original expert annotation provenance missing')
        if (urllib.parse.urlparse(row.get('source_image_url','')).hostname or '').removeprefix('www.')!='atlasdermatologico.com.br':
            raise ValueError('Original image host permission has not been verified')
        image=Path(row['image']);data=image.read_bytes()
        if hashlib.sha256(data).hexdigest()!=row.get('image_sha256') or hashlib.md5(data).hexdigest()!=row.get('source_image_md5'):
            raise ValueError('Original expert image checksum mismatch')
        annotation_file=image.parent.parent/'skincon.csv'
        if annotation_file not in tables:
            data=annotation_file.read_bytes()
            if hashlib.sha256(data).hexdigest()!=ANNOTATION_SHA256:raise ValueError('Source annotation CSV changed')
            tables[annotation_file]={r['ImageID'].rsplit('.',1)[0]:r for r in csv.DictReader(annotation_file.open())}
        annotation=tables[annotation_file][row['source_image_md5']]
        actual={finding['tag'] for finding in row['target']['observations']}
        expected=appearance_tags(annotation)
        if actual!=set(expected) or row['target']!=target_for(expected):
            raise ValueError('Appearance target differs from the original expert concepts')


def group_source_images(rows):
    """Group exact/near-duplicate images without claiming person identities."""
    parent=list(range(len(rows)))
    def find(index):
        while parent[index]!=index:
            parent[index]=parent[parent[index]];index=parent[index]
        return index
    for i,row in enumerate(rows):
        for j,other in enumerate(rows[:i]):
            if row['rgb_sha256']==other['rgb_sha256'] or (row['dhash64']^other['dhash64']).bit_count()<=3:
                a,b=find(i),find(j)
                if a!=b:parent[a]=b
    clusters={}
    for i,row in enumerate(rows):clusters.setdefault(find(i),[]).append(row)
    for cluster in clusters.values():
        group='unverified-source-cluster-'+min(row['source_image_md5'] for row in cluster)
        for row in cluster:
            row['subject_id']=group;row['subject_id_verified']=False;row['group_unit']='source image / near-duplicate cluster'
    return rows

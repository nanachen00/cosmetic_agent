"""Audit seven previously missing original Atlas URLs; never expand class skew.

Public source metadata only. No model predictions, split changes, retries,
authentication, alternate mirrors or image publication. Only HTTP-200 bytes
whose original MD5 matches can become a cleaning candidate; nothing is gold
or automatically admitted to training by this audit.
"""
from __future__ import annotations
import argparse,collections,csv,datetime,hashlib,io,json,re,urllib.error,urllib.parse,urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
SOURCES={
    'skincon.csv':'https://skincon-dataset.github.io/files/annotations_fitzpatrick17k.csv',
    'fitzpatrick.csv':'https://raw.githubusercontent.com/mattgroh/fitzpatrick17k/main/fitzpatrick17k.csv',
    'FITZ-README.md':'https://raw.githubusercontent.com/mattgroh/fitzpatrick17k/main/README.md',
    'ATLAS-terms.html':'https://atlasdermatologico.com.br/',
}
MAPPING={'acne_like_bumps':('Papule','Comedo','Pustule'),'redness':('Erythema',),
         'dark_marks':('Brown(Hyperpigmentation)',),'flaking':('Scale',)}
ANNOTATION_SHA256='ec0f1ab9b5013f0ec410aa5e978d49da7c3990d8631d1b7564f5c0e153735692'

def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def sha(data):return hashlib.sha256(data).hexdigest()
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
class AtlasOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        parsed=urllib.parse.urlsplit(newurl)
        if parsed.scheme!='https' or (parsed.hostname or '').removeprefix('www.')!='atlasdermatologico.com.br':
            raise ValueError('Refuse redirect outside the licensed original Atlas host')
        return super().redirect_request(request,fp,code,msg,headers,newurl)

def fetch(url,limit=12*1024*1024,atlas_only=False):
    request=urllib.request.Request(url,headers={'User-Agent':'BLINK bounded academic provenance audit'})
    handlers=[urllib.request.ProxyHandler({})]
    if atlas_only:handlers.append(AtlasOnlyRedirect())
    opener=urllib.request.build_opener(*handlers)
    with opener.open(request,timeout=20) as response:
        data=response.read(limit+1)
        if len(data)>limit:raise ValueError('Object exceeds audit budget')
        return data,dict(http_status=response.status,final_url=response.url,
            content_type=response.headers.get('Content-Type'),etag=response.headers.get('ETag'),
            last_modified=response.headers.get('Last-Modified'))

def tags(row):
    if row.get('Do not consider this image')!='0':raise ValueError('Excluded by source experts')
    if any(row.get(name) not in {'0','1'} for names in MAPPING.values() for name in names):raise ValueError('Unknown source concept')
    return [tag for tag,names in MAPPING.items() if any(row[name]=='1' for name in names)]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--old',type=Path,action='append',required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if args.output.exists():raise FileExistsError('Fresh audit directory required')
    args.output.mkdir(parents=True);snapshots=args.output/'source-snapshots';snapshots.mkdir()
    source_records={}
    for name,url in SOURCES.items():
        data,info=fetch(url);(snapshots/name).write_bytes(data)
        source_records[name]=dict(requested_url=url,sha256=sha(data),bytes=len(data),**info)
    if source_records['skincon.csv']['sha256']!=ANNOTATION_SHA256:raise ValueError('Source annotations changed; stop rather than relabel frozen data')
    if 'free to use for non-comercial' not in (snapshots/'ATLAS-terms.html').read_text(errors='replace').lower():raise ValueError('Atlas permission wording changed')
    if 'creativecommons.org/licenses/by-nc-sa/3.0/' not in (snapshots/'FITZ-README.md').read_text():raise ValueError('Fitzpatrick license reference changed')
    concepts={r['ImageID'].rsplit('.',1)[0]:r for r in csv.DictReader((snapshots/'skincon.csv').open()) if r['Do not consider this image']=='0'}
    metadata=list(csv.DictReader((snapshots/'fitzpatrick.csv').open()));used=set();missing=set();prior=[]
    for old in args.old:
        manifest=old/'manifest.jsonl';provenance=old/'provenance.json'
        used.update(json.loads(line)['source_image_md5'] for line in manifest.read_text().splitlines() if line.strip())
        missing.update(r['source_id'] for r in json.loads(provenance.read_text()).get('rejected',[]))
        prior.extend(dict(path=str(path.resolve()),sha256=sha(path.read_bytes())) for path in [manifest,provenance])
    joined=[];other_hosts=collections.Counter()
    for row in metadata:
        if row['md5hash'] not in concepts:continue
        host=(urllib.parse.urlsplit(row['url']).hostname or '').removeprefix('www.')
        if host=='atlasdermatologico.com.br':joined.append(row)
        else:other_hosts[host]+=1
    remaining=[r for r in joined if r['md5hash'] not in used]
    recheck=[r for r in remaining if r['md5hash'] in missing]
    if len(recheck)!=7:raise ValueError('Expected exactly the seven reviewed missing source IDs')
    counts=collections.Counter();results=[]
    for row in remaining:counts.update(tags(concepts[row['md5hash']]) or ['none_of_four'])
    for row in sorted(recheck,key=lambda r:r['md5hash']):
        key=row['md5hash'];assert re.fullmatch('[0-9a-f]{32}',key)
        url=row['url'].replace('http://','https://',1)
        result=dict(source_id=key,requested_url=url,checked_at_utc=utc(),attempts=1,
            source_tags=tags(concepts[key]),source_concepts={name:concepts[key][name] for names in MAPPING.values() for name in names},
            source_skin_tone=row.get('fitzpatrick_scale'),label_origin='source_expert_derived',
            subject_identity_verified=False,accepted_for_training=False,image_saved=False)
        try:
            data,info=fetch(url,atlas_only=True);result.update(info,received_bytes=len(data),received_sha256=sha(data))
            host=(urllib.parse.urlsplit(info['final_url']).hostname or '').removeprefix('www.')
            result['original_md5_matches']=hashlib.md5(data).hexdigest()==key
            if host!='atlasdermatologico.com.br':raise ValueError('Redirect outside licensed original host')
            if not result['original_md5_matches']:raise ValueError('Original MD5 mismatch; no relabelled replacement accepted')
            # A recovered image needs the complete independent cleaning flow first.
            result['status']='source_recovered_needs_full_cleaning_not_training'
            path=args.output/'recovered-candidates'/f'{key}.image';path.parent.mkdir(exist_ok=True);path.write_bytes(data);result['image_saved']=True
        except urllib.error.HTTPError as error:
            result.update(http_status=error.code,final_url=error.url,status='unavailable_original_http_error')
        except (OSError,ValueError) as error:
            result.update(status='unavailable_or_unverified',error_type=type(error).__name__)
        results.append(result)
        print(json.dumps({'completed':len(results),'total':7,'http_status':result.get('http_status'),'status':result['status']}),flush=True)
    save(args.output/'seven-original-url-checks.json',results)
    report=dict(checked_at_utc=utc(),research_only=True,automatic_deployment=False,skincon_valid_expert_rows=len(concepts),
        fitzpatrick_rows=len(metadata),atlas_expert_join=len(joined),previously_collected_source_ids=len(used),
        remaining_atlas_rows=len(remaining),remaining_source_tag_counts=dict(counts),previously_unavailable_rechecked=len(recheck),
        requested_image_urls=7,image_retries=0,other_remaining_rows_not_downloaded=len(remaining)-len(recheck),
        new_train_eligible_images=0,recovered_needing_full_cleaning=sum(r['image_saved'] for r in results),
        http_status_counts=dict(collections.Counter(str(r.get('http_status','unavailable')) for r in results)),
        source_snapshots=source_records,prior_intakes=prior,other_expert_metadata_hosts=dict(other_hosts),
        locked_719_split_changed=False,test_model_predictions_read=False,ai_labels_used_as_gold=False,
        limitations=['Four-tag-negative source concepts do not mean healthy skin or a healthy face.',
            'Source-image identity is not verified patient identity.',
            'Images without original bytes cannot pass MD5/SHA/decode/quality/mirror/OCR gates; none are claimed cleaned.',
            'Remaining redness-only rows were intentionally not fetched; count expansion would not address requested weak classes.'])
    save(args.output/'gap-report.json',report);print(json.dumps({key:report[key] for key in ['remaining_atlas_rows','http_status_counts','new_train_eligible_images','recovered_needing_full_cleaning']},indent=2))

if __name__=='__main__':main()

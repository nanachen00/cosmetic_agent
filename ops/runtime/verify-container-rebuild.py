#!/usr/bin/env python3
"""Record and verify a real MI300X container replacement without rebuilding it.

Never controls Kubernetes, reads .env/private SSH keys, deletes data, or starts
the production API. The post check requires an independently retained snapshot
hash, a changed container identity, unchanged NFS assets, and a GPU base load.
"""
from __future__ import annotations
import argparse,base64,concurrent.futures,datetime,hashlib,json,os,re,secrets,ssl,subprocess,sys,urllib.request,urllib.error
from pathlib import Path

ROOT=Path('/mnt/goodagents')
AREA=ROOT/'outputs/rebuild-verification'
MODEL_LOCATIONS={
    'photo-base':ROOT/'models/qwen3-vl-4b-instruct',
    'taide-prod':ROOT/'blink-runtime/data/models/taide-12b',
    'embedding':ROOT/'hf-cache/hub/models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181',
    'taide-ab':ROOT/'blink-runtime/data/models/taide-12b-2602',
    'reranker':ROOT/'hf-cache/hub/models--BAAI--bge-reranker-v2-m3/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e',
}
REQUIRED_MODELS={'photo-base','taide-prod','embedding'}
EXCLUDED_PARTS={'__pycache__','pids','logs','secrets','credentials','.git','.cache','.ipynb_checkpoints'}
PUBLIC_SOURCE_SUFFIXES={'.py','.sh','.lock','.txt','.json','.conf','.jinja','.jinja2'}

def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def write(path,value):
    temporary=path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2));temporary.chmod(0o600);temporary.replace(path)
def below(path,root=ROOT):
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('Path escapes its permitted root')
    return path
def member(folder,name):
    p=Path(name)
    if p.is_absolute() or '..' in p.parts or any(part.startswith('.') for part in p.parts):raise ValueError('Unsafe manifest member')
    return below(folder/p,folder)
def model_member(folder,name):
    # Hugging Face snapshot files legitimately link to sibling blob storage.
    p=Path(name)
    if p.is_absolute() or '..' in p.parts or any(part.startswith('.') for part in p.parts):raise ValueError('Unsafe model member')
    return below(folder/p,ROOT)

def require_mount(expected_source=None):
    data=json.loads(subprocess.check_output(['findmnt','-J','-T',str(ROOT)],text=True))['filesystems'][0]
    if data['target']!=str(ROOT) or data['fstype'] not in {'nfs','nfs4'} or 'rw' not in data.get('options','').split(','):
        raise RuntimeError('Expected writable /mnt/goodagents NFS mount is missing; no directory will be created')
    if expected_source and data['source']!=expected_source:raise RuntimeError('Persistent NFS source differs from the expected volume')
    return data

def identity_claims(token):
    # Only return identity fields; never retain or print credentials/full claims.
    part=token.split('.')[1]
    claims=json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4)))
    value=claims.get('kubernetes.io',{})
    return dict(pod_uid=value.get('pod',{}).get('uid'),pod_name=value.get('pod',{}).get('name'),namespace=value.get('namespace'))

def mount_identity(lines):
    result={}
    for line in lines:
        fields=line.split()
        if len(fields)<5:continue
        source,target=fields[3:5]
        if target=='/etc/hosts':
            found=re.search(r'/pods/([0-9a-f-]{36})/',source)
            if found:result['mount_pod_uid']=found.group(1)
        if target=='/etc/hostname':
            found=re.search(r'/containers/([0-9a-f]{64})/',source)
            if found:result['container_id']=found.group(1)
    return result

def capture_identity():
    result=dict(hostname=os.uname().nodename,**mount_identity(Path('/proc/self/mountinfo').read_text().splitlines()))
    sa=Path('/var/run/secrets/kubernetes.io/serviceaccount')
    if (sa/'token').is_file():
        result.update(identity_claims((sa/'token').read_text()))
    if result.get('pod_uid') and result.get('mount_pod_uid') and result['pod_uid']!=result['mount_pod_uid']:
        raise RuntimeError('Projected token and mountinfo disagree on Pod UID')
    result['pod_uid']=result.get('mount_pod_uid') or result.get('pod_uid')
    result['identity_sources']=['projected serviceaccount identity claims only','/etc/hosts and /etc/hostname mountinfo']
    result['image_id']=None;result['pvc_spec']=None
    if os.getenv('KUBERNETES_SERVICE_HOST') and (sa/'token').is_file() and result.get('namespace'):
        url='https://'+os.environ['KUBERNETES_SERVICE_HOST']+':'+os.getenv('KUBERNETES_SERVICE_PORT_HTTPS','443')
        url+='/api/v1/namespaces/'+result['namespace']+'/pods/'+result['hostname']
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+(sa/'token').read_text().strip()})
        try:
            with urllib.request.urlopen(request,context=ssl.create_default_context(cafile=str(sa/'ca.crt')),timeout=10) as response:pod=json.load(response)
            result['api_pod_uid']=pod['metadata']['uid']
            result['image_id']=[dict(name=x['name'],image=x['image'],image_id=x.get('imageID')) for x in pod['status']['containerStatuses']]
            result['pvc_spec']=[dict(name=x['name'],claim=x['persistentVolumeClaim']['claimName']) for x in pod['spec']['volumes'] if 'persistentVolumeClaim' in x]
            result['pod_api_status']=200
        except urllib.error.HTTPError as exc:result['pod_api_status']=exc.code
        except (OSError,ValueError):result['pod_api_status']='unavailable'
    return result

def replacement_kind(before,after):
    old=before.get('container_id');new=after.get('container_id')
    if not old or not new:raise ValueError('Container identity unavailable; a process restart is insufficient evidence')
    if old==new:raise ValueError('Container identity did not change; actual rebuild remains unverified')
    if before.get('pod_uid') and after.get('pod_uid') and before['pod_uid']!=after['pod_uid']:return 'pod_recreated'
    return 'container_recreated_within_same_pod'

def public_host_keys():
    records={}
    for path in sorted(Path('/etc/ssh').glob('ssh_host_*_key.pub')):
        result=subprocess.run(['ssh-keygen','-lf',str(path)],capture_output=True,text=True,check=True)
        fields=result.stdout.split()
        records[path.name]=dict(fingerprint=fields[1],bits=fields[0],public_file_sha256=sha(path))
    if not records:raise RuntimeError('SSH host public fingerprints unavailable')
    return records

def check_host_keys(before,after,evidence=None):
    wanted={name:value['fingerprint'] for name,value in after.items()}
    previous={name:value['fingerprint'] for name,value in before.items()}
    if previous==wanted:return 'unchanged'
    if not evidence or evidence.get('method') not in {'school_console','school_administrator'} or evidence.get('fingerprints')!=wanted or not evidence.get('verified_at'):
        raise ValueError('SSH host key changed; out-of-band school-console/administrator verification is required')
    return 'changed_with_operator_attested_out_of_band_record'

def files_under(folder,suffixes=None):
    if not folder.is_dir():return []
    result=[]
    for path in folder.rglob('*'):
        relative=path.relative_to(folder)
        if any(part in EXCLUDED_PARTS or part.startswith('.') for part in relative.parts):continue
        if path.is_file() and (suffixes is None or path.suffix in suffixes):result.append(below(path,ROOT))
    return sorted(result)

def hash_records(records):
    def work(item):
        path,expected,role=item;stat=path.stat();actual=sha(path);after=path.stat()
        if (stat.st_size,stat.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise RuntimeError('Asset changed during hashing: '+str(path))
        if expected and actual!=expected:raise ValueError('Asset checksum mismatch: '+str(path))
        return str(path),dict(sha256=actual,bytes=stat.st_size,role=role,recorded_expected_sha256=expected)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:return dict(pool.map(work,records))

def verify_records(records):
    return hash_records([(below(Path(path)),record['sha256'],record['role']) for path,record in records.items()])

def package_inventory(python):
    code="import importlib.metadata as m,json,sys;print(json.dumps({'python':sys.version,'base_executable':sys._base_executable,'prefix':sys.prefix,'packages':sorted([{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()],key=lambda x:x['name'].lower())}))"
    result=subprocess.run([str(python),'-c',code],capture_output=True,text=True,check=True,timeout=30,env=python_environment())
    return json.loads(result.stdout)

def python_environment():
    env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HOME=str(ROOT/'hf-cache'),
             PYTHONNOUSERSITE='1',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
    for name in ('PYTHONPATH','PYTHONHOME','LD_PRELOAD','LD_LIBRARY_PATH'):env.pop(name,None)
    return env

def photo_record(value):
    source=Path(value['source']);python=Path(value['python'])
    if not source.is_absolute() or not python.is_absolute():raise ValueError('Absolute managed photo source/Python required')
    below(source,ROOT);below(python,ROOT)
    if type(value.get('port')) is not int or value['port']!=8025 or value.get('adapter') is not None:
        raise ValueError('Only the canonical base-model photo service on 8025 can be baselined')
    if value.get('command')!=[str(python),'-u',str(source/'photo_service.py')]:
        raise ValueError('Managed photo command does not match its source/Python')
    return source,python

def active_configuration(runtime,required=False):
    active=ROOT/'outputs/runtime/photo-model.active.json'
    if not active.is_file():
        if required:raise FileNotFoundError('Persistent active photo runtime configuration is required before the final baseline')
        return None
    value=json.loads(active.read_text())
    if value.get('instance')!='photo-model' or type(value.get('port')) is not int or value['port']!=8025:
        raise ValueError('Durable photo configuration must select canonical instance/port')
    if value.get('source')!=runtime['source'] or value.get('python')!=runtime['python']:
        raise ValueError('Durable photo source/Python differs from the managed live service')
    metadata=json.loads((Path(runtime['python']).parent.parent/'runtime-ready.json').read_text())
    lock=value.get('native_lock_sha256')
    if value.get('profile')!='secure-rocm71' or metadata.get('profile')!=value['profile'] or not isinstance(lock,str) or not re.fullmatch('[a-f0-9]{64}',lock) or metadata.get('native_lock_sha256')!=lock:
        raise ValueError('Durable photo profile/native lock differs from the verified secure environment')
    return active

def bge_configuration(value,active,metadata,source_manifest):
    source=Path(value['source']);python=Path(value['python'])
    if not source.is_absolute() or not python.is_absolute():raise ValueError('Absolute managed BGE source/Python required')
    below(source,ROOT);below(python,ROOT)
    if value.get('service')!='blink-managed-bge-v1' or value.get('instance')!='bge-model' or type(value.get('port')) is not int or value['port']!=8002:
        raise ValueError('Only the canonical managed BGE service on 8002 can be baselined')
    if value.get('command')!=[str(python),'-u',str(source/'bge_secure_service.py'),'--port','8002']:
        raise ValueError('Managed BGE command does not match its source/Python')
    for name in ('source','python','instance','port'):
        if active.get(name)!=value[name]:raise ValueError('Durable BGE configuration differs from the managed live service')
    if active.get('profile')!='secure-rocm71' or metadata.get('profile')!=active['profile']:
        raise ValueError('Durable BGE profile differs from the verified secure environment')
    for name in ('native_lock_sha256','python_lock_sha256'):
        digest=active.get(name)
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest) or metadata.get(name)!=digest:
            raise ValueError('Durable BGE lock differs from the verified secure environment')
    if active.get('source_manifest_sha256')!=sha(source_manifest):raise ValueError('Durable BGE source manifest checksum differs')
    required={'bge_secure_service.py','bounded_http.py','bge-m3.secure.manifest.json','requirements-bge-linux-py311.lock'}
    manifest=json.loads(source_manifest.read_text())
    if not required.issubset(manifest):raise ValueError('Incomplete durable BGE source manifest')
    if {str(path.relative_to(source)) for path in source.rglob('*')}!=set(manifest)|{'source-manifest.json'}:
        raise ValueError('Unreviewed files in durable BGE source')
    for name,digest in manifest.items():
        if sha(member(source,name))!=digest:raise ValueError('Durable BGE serving source checksum differs')
    if sha(source/'requirements-bge-linux-py311.lock')!=metadata['python_lock_sha256']:
        raise ValueError('BGE Python lock differs from its active source')
    return source,python

def bge_runtime_info(required=False):
    active_path=ROOT/'outputs/runtime/bge-model.active.json'
    if not active_path.is_file():
        if required:raise FileNotFoundError('Persistent formal BGE configuration required before the final baseline')
        return None
    state=ROOT/'outputs/runtime/pids/bge-model.identity.json'
    value=json.loads(state.read_text());active=json.loads(active_path.read_text())
    python=Path(value['python']);source=Path(value['source'])
    below(python,ROOT);below(source,ROOT)
    metadata=json.loads((python.parent.parent/'runtime-ready.json').read_text())
    source,python=bge_configuration(value,active,metadata,source/'source-manifest.json')
    proc=Path('/proc')/str(value['pid']);ticks=proc.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]
    command=proc.joinpath('cmdline').read_bytes().rstrip(b'\0').split(b'\0')
    if ticks!=str(value['start_ticks']) or command!=[x.encode() for x in value['command']] or proc.joinpath('exe').resolve()!=python.resolve():
        raise RuntimeError('Managed BGE serving identity is stale')
    return dict(source=str(source),python=str(python),resolved_python=str(python.resolve()),port=8002,
                active_configuration_file=str(active_path),environment=package_inventory(python))

def same_packages(before,after):
    if before['packages']!=after['packages'] or before['python']!=after['python']:
        raise ValueError('Persistent Python/package versions differ from the pre-rebuild inventory')
    return dict(packages=len(after['packages']),python_and_package_versions_match=True)

def runtime_info(require_active=False):
    state=ROOT/'outputs/runtime/pids/photo-model.identity.json'
    if not state.is_file():raise FileNotFoundError('Managed photo service state is missing')
    value=json.loads(state.read_text());source,python=photo_record(value)
    proc=Path('/proc')/str(value['pid'])
    ticks=proc.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]
    command=proc.joinpath('cmdline').read_bytes().rstrip(b'\0').split(b'\0')
    if ticks!=str(value['start_ticks']) or command!=[x.encode() for x in value['command']] or proc.joinpath('exe').resolve()!=python.resolve():raise RuntimeError('Managed serving identity is stale')
    if not (source/'photo_service.py').is_file():raise FileNotFoundError('Active photo source missing')
    runtime=dict(source=str(source),python=str(python),resolved_python=str(python.resolve()),port=value['port'],adapter=value.get('adapter'))
    active=active_configuration(runtime,require_active)
    if active:runtime['active_configuration_file']=str(active)
    runtime['environment']=package_inventory(python)
    return runtime

def pre(args):
    mount=require_mount(args.expected_source)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}',args.run_id):raise ValueError('Invalid run id')
    folder=AREA/args.run_id
    if folder.exists():raise FileExistsError('Fresh rebuild run required; existing evidence is never overwritten')
    manifest_path=below(args.model_manifest);catalog_path=below(args.checkpoint_catalog)
    if sha(catalog_path)!=args.catalog_sha256:raise ValueError('Independent Mac catalog checksum mismatch')
    model_manifest=json.loads(manifest_path.read_text());catalog=json.loads(catalog_path.read_text())
    if len(catalog['checkpoints'])!=args.expected_adapters:raise ValueError('Unexpected independent checkpoint inventory')
    records=[];models=[];adapters=[]
    for key,location in MODEL_LOCATIONS.items():
        metadata=model_manifest['models'][key]
        if not location.is_dir():
            if key in REQUIRED_MODELS:raise FileNotFoundError('Required restored model missing: '+key)
            models.append(dict(key=key,path=str(location),available=False,required=False,status='not_present_not_restored'))
            continue
        weights=metadata['weight_sha256']
        indexes=list(location.glob('*.safetensors.index.json'))
        for index in indexes:
            for name in set(json.loads(index.read_text())['weight_map'].values()):
                if not model_member(location,name).is_file():raise FileNotFoundError('Model shard named in index is missing: '+name)
        if not list(location.glob('*.safetensors')) and not list(location.glob('*.bin')):raise FileNotFoundError('Model has no restored weight files: '+key)
        for name in weights:
            if not model_member(location,name).is_file():raise FileNotFoundError('Expected official weight missing: '+name)
        for path in files_under(location):records.append((path,weights.get(str(path.relative_to(location))),'model:'+key))
        models.append(dict(key=key,path=str(location),resolved=str(location.resolve()),available=True,required=key in REQUIRED_MODELS,repo_id=metadata['repo_id'],revision=metadata['revision'],weight_hashes_available=bool(weights),checksum_provenance=metadata.get('checksum_provenance',{'kind':'pinned_model_manifest_weight_hashes'})))
    for entry in catalog['checkpoints']:
        relative=entry['relative_path'];backup=member(ROOT/'checkpoint-backups',relative)
        source=member(ROOT/'checkpoints',relative)
        if not (source/'adapter_model.safetensors').is_file():source=source/'best'
        sidecar=backup/'checkpoint-sha256.json'
        if sha(sidecar)!=entry['manifest_sha256']:raise ValueError('Independent Mac checkpoint manifest differs: '+relative)
        expected=json.loads(sidecar.read_text())
        if 'adapter_model.safetensors' not in expected:raise ValueError('Checkpoint has no safetensors weight')
        for name,digest in expected.items():
            records.extend([(member(backup,name),digest,'checkpoint-backup'),(member(source,name),digest,'checkpoint-original')])
        records.append((sidecar,entry['manifest_sha256'],'checkpoint-manifest'))
        adapters.append(dict(relative_path=relative,source=str(source),backup=str(backup),expected_files=len(expected)))
    runtime=runtime_info(args.require_active_config);source=Path(runtime['source']);python=Path(runtime['python'])
    if runtime.get('active_configuration_file'):
        active=Path(runtime['active_configuration_file'])
        records.append((below(active),None,'active-runtime-configuration'))
    records.extend((p,None,'active-serving-source') for p in files_under(source))
    for path in files_under(ROOT/'blink-runtime/src',PUBLIC_SOURCE_SUFFIXES):
        if not path.is_relative_to(source):records.append((path,None,'other-persistent-source-and-locks'))
    for path in (ROOT/'blink-runtime/src').glob('*/source-manifest.json'):
        records.append((below(path),None,'frozen-source-manifest'))
    records.append((python.resolve(),None,'persistent-python-executable'))
    for name in ['pyvenv.cfg','runtime-ready.json','python-install-report.json']:
        p=python.parent.parent/name
        if p.is_file():records.append((p,None,'runtime-install-metadata'))
    embedding=bge_runtime_info(args.require_bge_config)
    if embedding:
        records.append((below(Path(embedding['active_configuration_file'])),None,'embedding-active-configuration'))
        records.extend((p,None,'embedding-serving-source') for p in files_under(Path(embedding['source'])))
        epy=Path(embedding['python']);records.append((epy.resolve(),None,'embedding-python-executable'))
        for name in ['pyvenv.cfg','runtime-ready.json','python-install-report.json','native-install-report.json']:
            path=epy.parent.parent/name
            if path.is_file():records.append((path,None,'embedding-runtime-install-metadata'))
    for dirname in ['qwen-expanded-20261115-r2','qwen-app-expert-20261115-r2','qwen-app-visual-20261115-r1','qwen-clean-20261116-r1']:
        records.extend((p,None,'training-output') for p in files_under(ROOT/'outputs/skin-vlm'/dirname,{'.json','.jsonl','.lock'}))
    records.extend([(manifest_path,None,'official-model-manifest'),(catalog_path,args.catalog_sha256,'independent-Mac-catalog')])
    critical=hash_records(records)
    identity=capture_identity();keys=public_host_keys()
    folder.mkdir(parents=True,mode=0o700)
    sentinel=folder/'persistent-sentinel';sentinel.write_bytes(secrets.token_bytes(64));sentinel.chmod(0o600)
    snapshot=dict(schema_version=1,captured_at_utc=utc(),run_id=args.run_id,mount=mount,identity=identity,ssh_public_host_keys=keys,
        sentinel=dict(path=str(sentinel),sha256=sha(sentinel)),models=models,adapters=adapters,runtime=runtime,embedding_runtime=embedding,
        critical_files=critical,verifier_sha256=sha(Path(__file__)),independent_catalog_sha256=args.catalog_sha256,
        missing_control_metadata=[key for key in ('image_id','pvc_spec') if identity.get(key) is None],
        status='prepared_not_rebuilt',historical_missing_adapter_restored=False,
        scope='Pre-rebuild inventory only; NFS-side copies are not independent storage. Mac copies are independently catalogued.')
    path=folder/'pre.json';write(path,snapshot)
    print(json.dumps(dict(status=snapshot['status'],snapshot=str(path),snapshot_sha256=sha(path),models=len(models),adapters=len(adapters),files=len(critical),bytes=sum(x['bytes'] for x in critical.values()),identity=identity,ssh_public_host_keys=keys),indent=2))

def post(args):
    require_mount()
    path=below(args.snapshot)
    if sha(path)!=args.snapshot_sha256:raise ValueError('Pre snapshot differs from independently retained hash')
    snapshot=json.loads(path.read_text());require_mount(snapshot['mount']['source'])
    report=dict(checked_at_utc=utc(),run_id=snapshot['run_id'],failures=[],actual_rebuild_verified=False)
    def check(label,callback):
        try:report[label]=callback()
        except Exception as exc:report['failures'].append(dict(check=label,error=str(exc)))
    check('verifier_code',lambda:sha(Path(__file__))==snapshot['verifier_sha256'] or (_ for _ in ()).throw(ValueError('Verifier code changed')))
    current=capture_identity();report['identity']=current
    check('replacement_kind',lambda:replacement_kind(snapshot['identity'],current))
    evidence=json.loads(args.host_key_evidence.read_text()) if args.host_key_evidence else None
    check('ssh_host_key_verification',lambda:check_host_keys(snapshot['ssh_public_host_keys'],public_host_keys(),evidence))
    check('sentinel',lambda:sha(Path(snapshot['sentinel']['path']))==snapshot['sentinel']['sha256'] or (_ for _ in ()).throw(ValueError('NFS sentinel changed')))
    # A same-container negative control does not reread every large weight file.
    if not report['failures']:check('assets',lambda:dict(files=len(verify_records(snapshot['critical_files'])),hashes_match=True))
    else:report['assets']=dict(status='not_run_until_container_identity_and_SSH_trust_are_verified')
    if not report['failures']:
        runtime=snapshot['runtime'];code="import sys,json;sys.path.insert(0,sys.argv[1]);from skin_vlm import SkinVLM;engine=SkinVLM(sys.argv[2]);print(json.dumps({'base_loaded':True,'device':engine.runtime['device'],'attention':engine.runtime['attention']}))"
        check('runtime_versions',lambda:same_packages(runtime['environment'],package_inventory(Path(runtime['python']))))
        if snapshot.get('embedding_runtime'):
            embedding=snapshot['embedding_runtime']
            check('embedding_runtime_versions',lambda:same_packages(embedding['environment'],package_inventory(Path(embedding['python']))))
    if not report['failures']:
        env=python_environment()
        def load():
            library=subprocess.check_output([runtime['python'],'-c',"import importlib.util,pathlib;print(pathlib.Path(importlib.util.find_spec('torch').origin).parent/'lib')"],text=True,env=env).strip()
            env['LD_LIBRARY_PATH']=library+':/opt/rocm/lib:/opt/rocm/lib64'
            result=subprocess.run([runtime['python'],'-c',code,runtime['source'],str(MODEL_LOCATIONS['photo-base'])],env=env,capture_output=True,text=True,timeout=180,check=True)
            return json.loads(result.stdout.splitlines()[-1])
        check('official_base_gpu_load',load)
    report['actual_rebuild_verified']=not report['failures']
    report['status']='container_and_persistent_assets_verified' if not report['failures'] else 'not_verified'
    report['missing_control_metadata']=[key for key in ('image_id','pvc_spec') if current.get(key) is None]
    destination=path.parent/('post-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json');write(destination,report)
    print(json.dumps(report,indent=2));return 0 if not report['failures'] else 1

def restore_check(args):
    require_mount(args.expected_source);folder=below(args.directory,AREA/'restore-staging')
    path=folder/'checkpoint-sha256.json'
    if sha(path)!=args.manifest_sha256:raise ValueError('Restored manifest differs from independently measured Mac original')
    expected=json.loads(path.read_text())
    result=hash_records([(member(folder,name),digest,'restored-checkpoint') for name,digest in expected.items()])
    report=dict(checked_at_utc=utc(),status='restore_staging_verified',directory=str(folder),files=len(result),bytes=sum(x['bytes'] for x in result.values()),
        independent_Mac_manifest_sha256=args.manifest_sha256,sha256_verified=True,actual_container_rebuild=False,scope='One Mac-to-new-NFS-folder restoration; not a container rebuild or all-checkpoint restore exercise')
    write(folder.parent/(folder.name+'-verification.json'),report);print(json.dumps(report,indent=2))

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    pre_p=sub.add_parser('pre');pre_p.add_argument('--run-id',required=True);pre_p.add_argument('--expected-source',required=True)
    pre_p.add_argument('--model-manifest',type=Path,required=True);pre_p.add_argument('--checkpoint-catalog',type=Path,required=True)
    pre_p.add_argument('--catalog-sha256',required=True);pre_p.add_argument('--expected-adapters',type=int,default=17)
    pre_p.add_argument('--require-active-config',action='store_true',help='Require the durable managed runtime selection before the final baseline')
    pre_p.add_argument('--require-bge-config',action='store_true',help='Require the promoted secure BGE active pointer/source/environment before the final baseline')
    post_p=sub.add_parser('post');post_p.add_argument('--snapshot',type=Path,required=True);post_p.add_argument('--snapshot-sha256',required=True);post_p.add_argument('--host-key-evidence',type=Path)
    restore_p=sub.add_parser('restore-check');restore_p.add_argument('--directory',type=Path,required=True);restore_p.add_argument('--manifest-sha256',required=True);restore_p.add_argument('--expected-source',required=True)
    sub.add_parser('identity',help='Print only container identity and SSH public fingerprints for a school-console trust check')
    args=p.parse_args()
    if args.action=='identity':
        print(json.dumps(dict(identity=capture_identity(),ssh_public_host_keys=public_host_keys()),indent=2));return 0
    try:return {'pre':pre,'post':post,'restore-check':restore_check}[args.action](args) or 0
    except Exception as exc:print(json.dumps(dict(status='failed',error=str(exc))),file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())

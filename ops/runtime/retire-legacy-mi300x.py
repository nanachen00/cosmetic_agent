#!/usr/bin/env python3
"""Plan legacy MI300X retirement; never stops a process unless apply is explicit.

Only the fixed TAIDE/old-remote-service classes below can be retired. Model
files, environments, logs and managed identity files are never deleted.
"""
from __future__ import annotations
import argparse,datetime,hashlib,json,math,os,re,signal,subprocess,sys,time,urllib.request
from pathlib import Path

ROOT=Path('/mnt/goodagents')
EXPECTED_NFS='192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11'
SERVICES={
    'legacy_backend8010':8010,'legacy_router8001':8001,
    'taide_gateway8000':8000,'taide_gateway8003':8003,'taide_engine8200':8200,
}
PROTECTED_PORTS={8002,8025,8026,8027}
REQUIRED_READINESS={'mac_backend_ready','mac_legacy_router_disabled','enable_taide_is_zero','all_text_roles_use_terra',
                    'remote_backend_unused','remote_router_unused','qwen_ready','embedding_available','mi300x_host_verified'}
TERRA_ROLES=('chat','intent','input_guard','pain_gain','composer')
SECRET_OPTIONS={'--api-key','--api_key','--token','--hf-token','--password','--access-token','--bearer-token'}

def utc():return datetime.datetime.now(datetime.timezone.utc)
def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def timestamp(text):return datetime.datetime.fromisoformat(text.replace('Z','+00:00'))
def fresh(value,key,maximum_age):
    age=(utc()-timestamp(value[key])).total_seconds()
    if age < -30 or age>maximum_age:raise ValueError('Review evidence is stale or has a future timestamp')
def require_mount():
    value=json.loads(subprocess.check_output(['findmnt','-J','-T',str(ROOT)],text=True))['filesystems'][0]
    if value['target']!=str(ROOT) or value['fstype'] not in {'nfs','nfs4'} or value['source']!=EXPECTED_NFS or 'rw' not in value.get('options','').split(','):
        raise RuntimeError('Expected writable school NFS mount missing')
    return value
def json_path(path):
    if path.suffix!='.json' or path.name.startswith('.') or not path.resolve().is_relative_to(ROOT/'outputs'):
        raise ValueError('Operational JSON evidence must remain under /mnt/goodagents/outputs')
    return path
def write(path,value):
    json_path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():raise FileExistsError('Fresh operational report path required')
    with path.open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2)
    path.chmod(0o600)
def update_owned_report(path,value):
    # Called only after this invocation exclusively created the audit file.
    temporary=path.with_suffix('.partial')
    with temporary.open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2)
    temporary.chmod(0o600);temporary.replace(path)

def container_identity():
    result={'hostname':os.uname().nodename}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields=line.split()
        if len(fields)<5:continue
        if fields[4]=='/etc/hostname':
            match=re.search(r'/containers/([a-f0-9]{64})/',fields[3])
            if match:result['container_id']=match.group(1)
        if fields[4]=='/etc/hosts':
            match=re.search(r'/pods/([a-f0-9-]{36})/',fields[3])
            if match:result['pod_uid']=match.group(1)
    if not result.get('container_id'):raise RuntimeError('Container identity unavailable; refusing process retirement')
    return result

def redact(argv):
    result=[];hide=False
    for value in argv:
        if hide:result.append('[REDACTED]');hide=False;continue
        option=value.split('=',1)[0]
        if option in SECRET_OPTIONS:
            result.append(option+'=[REDACTED]' if '=' in value else option);hide='=' not in value
        elif '://' in value and re.search(r'://[^/]*@',value):result.append('[REDACTED_CREDENTIAL_URL]')
        else:result.append(value)
    return result

def process(pid):
    if int(pid)<=1:raise ValueError('PID 1 and invalid process IDs are never retirement targets')
    p=Path('/proc')/str(pid)
    stat=(p/'stat').read_text().rsplit(')',1)[1].split();raw=(p/'cmdline').read_bytes()
    argv=[item.decode(errors='replace') for item in raw.rstrip(b'\0').split(b'\0')]
    return dict(pid=int(pid),start_ticks=stat[19],ppid=int(stat[1]),state=stat[0],argv=argv,
        command_sha256=hashlib.sha256(raw).hexdigest(),cwd=str((p/'cwd').resolve()),executable=str((p/'exe').resolve()))

def option(argv,name):
    values=[]
    for n,value in enumerate(argv):
        if value==name and n+1<len(argv):values.append(argv[n+1])
        elif value.startswith(name+'='):values.append(value.split('=',1)[1])
    return values[0] if len(values)==1 else None

def classify(name,proc):
    if name not in SERVICES:return False
    argv=proc['argv'];port=SERVICES[name]
    if not argv or 'python' not in Path(proc['executable']).name.lower():return False
    text=' '.join(argv).lower()
    if 'photo_service.py' in text or 'api_server_embedding.py' in text or 'qwen' in text or 'bge-m3' in text:return False
    if name.startswith('taide_gateway'):
        return str(ROOT/'blink-ai/ops/runtime/taide_vllm_gateway.py') in argv and option(argv,'--port')==str(port)
    if name=='taide_engine8200':
        model=option(argv,'--model')
        known={str(ROOT/'blink-runtime/data/models/taide-12b'),str(ROOT/'models/taide-12b'),str(ROOT/'blink-ai/taide-12b'),'taide/Gemma-3-TAIDE-12b-Chat'}
        return 'vllm.entrypoints.openai.api_server' in argv and option(argv,'--port')=='8200' and model in known and option(argv,'--served-model-name')=='taide-12b'
    if name=='legacy_router8001':return str(ROOT/'blink-ai/taide_router_8001.py') in argv
    return 'uvicorn' in argv and 'api.api_server:app' in argv and option(argv,'--port')=='8010'

def listener_inventory():
    listeners=[]
    for line in subprocess.check_output(['ss','-H','-ltnp'],text=True).splitlines():
        fields=line.split()
        if len(fields)<4:continue
        try:port=int(fields[3].rsplit(':',1)[-1])
        except ValueError:continue
        if port in set(SERVICES.values())|PROTECTED_PORTS:
            listeners.append(dict(local_address=fields[3],port=port,pids=sorted({int(x) for x in re.findall(r'pid=(\d+)',line)})))
    return listeners

def established_counts():
    counts={port:0 for port in SERVICES.values()}
    for line in subprocess.check_output(['ss','-H','-tn','state','established'],text=True).splitlines():
        fields=line.split()
        # State-filtered ss omits its state column: Recv-Q Send-Q Local Peer.
        local=fields[2] if len(fields)>=4 else ''
        try:port=int(local.rsplit(':',1)[-1])
        except ValueError:continue
        if port in counts:counts[port]+=1
    return counts

def parse_vllm_metrics(text):
    values={'num_requests_running':[],'num_requests_waiting':[]}
    for line in text.splitlines():
        if not line or line.startswith('#'):continue
        name=line.split('{',1)[0].split()[0].removeprefix('vllm:')
        if name not in values:continue
        parts=line.rsplit('}',1)[-1].split() if '}' in line else line.split()[1:]
        if not parts:raise ValueError('Missing vLLM metric value')
        value=float(parts[0])
        if not math.isfinite(value) or value<0 or value!=int(value):raise ValueError('Invalid vLLM queue count')
        values[name].append(int(value))
    if any(not entries for entries in values.values()):raise ValueError('Required vLLM running/waiting metrics unavailable')
    return {name:sum(entries) for name,entries in values.items()}

def vllm_counts():
    with urllib.request.urlopen('http://127.0.0.1:8200/metrics',timeout=5) as response:
        raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError('Metrics response exceeds bound')
    return parse_vllm_metrics(raw.decode())

def managed_records():
    result=[]
    for folder in [ROOT/'blink-ai/ops/runtime/pids',ROOT/'outputs/runtime/pids']:
        for path in sorted(folder.glob('*.identity.json')):
            try:
                value=json.loads(path.read_text())
                result.append(dict(path=str(path),sha256=digest(path),service=value.get('service'),pid=value.get('pid'),start_ticks=str(value.get('start_ticks')),command=value.get('command')))
            except (OSError,ValueError):result.append(dict(path=str(path),invalid=True))
    return result

def managed_match(proc,records):
    matches=[]
    for value in records:
        if value.get('pid')!=proc['pid']:continue
        same=value.get('start_ticks')==proc['start_ticks'] and (not value.get('command') or value['command']==proc['argv'])
        matches.append(dict(path=value['path'],service=value.get('service'),pid_matches=True,start_ticks_and_command_match=same))
    return dict(status='matching' if matches and all(x['start_ticks_and_command_match'] for x in matches) else 'mismatch' if matches else 'not_found',records=matches)

def controller(name,pids):
    found={}
    for pid in pids:
        item=process(pid);matched=False
        for _ in range(3):
            if classify(name,item):found[item['pid']]=item;matched=True;break
            if item['ppid']<=1:break
            item=process(item['ppid'])
        if not matched:raise ValueError('Listener includes an unapproved owner: '+name)
    if len(found)!=1:raise ValueError('Listener has no unique approved legacy controller: '+name)
    return next(iter(found.values()))

def protected_ancestors(listeners):
    protected=set()
    for row in listeners:
        if row['port'] not in PROTECTED_PORTS:continue
        if not row['pids']:raise ValueError('Cannot verify protected listener ownership')
        for pid in row['pids']:
            seen=set()
            while pid>1:
                if pid in seen or len(seen)>=128:raise ValueError('Cannot verify protected process ancestry')
                seen.add(pid);protected.add(pid);item=process(pid)
                if item['pid']!=pid:raise ValueError('Protected process identity differs')
                pid=item['ppid']
    return protected

def verify_target_listener(row,listeners):
    sockets=[item for item in listeners if item['port']==row['port']]
    owners=sorted({pid for item in sockets for pid in item['pids']})
    expected=sorted({pid for item in row['listeners'] for pid in item['pids']})
    if not owners or any(not item['pids'] for item in sockets) or owners!=expected:raise ValueError('Listener owner changed: '+row['service'])
    same_process(row,controller(row['service'],owners))
    if row['pid'] in protected_ancestors(listeners):raise ValueError('Legacy target owns or is an ancestor of a protected Qwen/embedding listener')

def build_plan():
    mount=require_mount();identity=container_identity();listeners=listener_inventory();managed=managed_records();connections=established_counts()
    targets=[]
    for name,port in SERVICES.items():
        sockets=[row for row in listeners if row['port']==port];pids=sorted({pid for row in sockets for pid in row['pids']})
        if not sockets:targets.append(dict(service=name,port=port,status='not_listening'));continue
        if not pids or any(not row['pids'] for row in sockets):raise ValueError('Cannot identify every listener owner: '+name)
        proc=controller(name,pids);entry={key:value for key,value in proc.items() if key!='argv'}
        entry.update(service=name,port=port,status='running',command=redact(proc['argv']),managed_identity=managed_match(proc,managed),listeners=sockets,established_connections=connections[port])
        targets.append(entry)
    metrics=None;metrics_error=None
    if any(x['service']=='taide_engine8200' and x['status']=='running' for x in targets):
        try:metrics=vllm_counts()
        except (OSError,ValueError) as exc:metrics_error=type(exc).__name__
    protected=[row for row in listeners if row['port'] in PROTECTED_PORTS]
    protected_pids=protected_ancestors(listeners)
    if any(x.get('pid') in protected_pids for x in targets):raise ValueError('A legacy target owns or is an ancestor of a protected Qwen/embedding listener')
    return dict(schema_version=1,created_at_utc=utc().isoformat(),read_only=True,mount=mount,container=identity,targets=targets,protected_listeners=protected,
        vllm_request_counts=metrics,vllm_metrics_error=metrics_error,
        apply_order=list(SERVICES),signal_policy='pidfd-pinned SIGTERM only; no force kill, no file deletion',
        required_readiness=sorted(REQUIRED_READINESS),required_terra_roles=list(TERRA_ROLES),
        title_policy='title_llm_enabled=false requires deterministic; true requires terra',
        scope='Exact retirement plan for legacy text services; 8002/8025/8026/8027 and their ancestors are protected')

def verify_prerequisites(evidence):
    fresh(evidence,'checked_at_utc',300)
    if any(evidence.get(key) is not True for key in REQUIRED_READINESS):raise ValueError('Main roles/readiness prerequisites are incomplete')
    if evidence.get('remaining_taide_roles')!=[]:raise ValueError('Application roles still depend on TAIDE')
    providers=evidence.get('role_providers',{})
    if any(providers.get(role)!='terra' for role in TERRA_ROLES):raise ValueError('All enabled text model roles must explicitly use Terra')
    enabled=evidence.get('title_llm_enabled')
    if type(enabled) is not bool:raise ValueError('Explicit conversation title LLM configuration is required')
    if providers.get('title')!=('terra' if enabled else 'deterministic'):
        raise ValueError('Title provider must match conversation title LLM configuration')
    return True

def same_process(expected,current):
    for key in ['pid','start_ticks','command_sha256','cwd','executable']:
        if expected.get(key)!=current.get(key):raise ValueError('Process identity changed: '+expected['service']+' ('+key+')')
    if not classify(expected['service'],current):raise ValueError('Process no longer belongs to the approved legacy service class')

def validate_plan(plan,evidence):
    verify_prerequisites(evidence);fresh(plan,'created_at_utc',900)
    if plan.get('schema_version')!=1 or plan.get('container')!=container_identity():raise ValueError('Retirement plan belongs to a different container')
    if plan.get('mount',{}).get('source')!=EXPECTED_NFS:raise ValueError('Retirement plan has the wrong volume')
    targets=plan.get('targets',[])
    if len(targets)!=len(SERVICES) or {row.get('service') for row in targets}!=set(SERVICES):raise ValueError('Only the five fixed legacy service classes can be retired')
    listeners=listener_inventory();protected=protected_ancestors(listeners)
    managed=managed_records()
    for row in targets:
        name=row['service'];port=SERVICES[name]
        if row['port']!=port:raise ValueError('Service/port mismatch')
        sockets=[x for x in listeners if x['port']==port]
        if row['status']=='not_listening':
            if sockets:raise ValueError('A new unplanned process occupies '+name)
            continue
        if row.get('status')!='running' or row['pid'] in protected:raise ValueError('Unknown or protected target')
        current=process(row['pid']);same_process(row,current)
        verify_target_listener(row,listeners)
        current_managed=managed_match(current,managed)
        if current_managed!=row['managed_identity'] or current_managed['status']=='mismatch':raise ValueError('Managed identity contradicts or differs from the reviewed plan')
    counts=established_counts()
    for row in targets:
        if row['status']=='running' and row['service']!='taide_engine8200' and counts[row['port']]:raise ValueError('Legacy service still has established client connections: '+row['service'])
    if any(x['service']=='taide_engine8200' and x['status']=='running' for x in targets):
        metrics=vllm_counts()
        if any(metrics.values()):raise ValueError('TAIDE vLLM has running or waiting requests')
    return targets

def stop_one(row,timeout=30,event=None):
    event={} if event is None else event
    event.update(service=row['service'],pid=row['pid'],start_ticks=row['start_ticks'],signal='pidfd-SIGTERM',signal_sent=False,stopped=False)
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        raise RuntimeError('Kernel/Python pidfd support required; no unsafe numeric-PID signal fallback')
    descriptor=os.pidfd_open(row['pid'],0)
    try:
        # Verify after opening the pidfd so PID reuse cannot redirect a signal.
        current=process(row['pid']);same_process(row,current)
        verify_target_listener(row,listener_inventory())
        managed=managed_match(current,managed_records())
        if managed!=row['managed_identity'] or managed['status']=='mismatch':raise ValueError('Managed identity changed before signal')
        if row['service']=='taide_engine8200':
            if any(vllm_counts().values()):raise ValueError('TAIDE became busy; no engine signal sent')
        elif established_counts()[row['port']]:raise ValueError('Legacy service became busy; no signal sent')
        signal.pidfd_send_signal(descriptor,signal.SIGTERM,None,0)
        event['signal_sent']=True
    finally:os.close(descriptor)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:current=process(row['pid'])
        except (FileNotFoundError,ProcessLookupError):current=None
        stopped=current is None or current['start_ticks']!=row['start_ticks'] or current['state']=='Z'
        if stopped:
            if any(x['port']==row['port'] for x in listener_inventory()):raise RuntimeError('Legacy port is still occupied after controller exit; no additional signal sent')
            event['stopped']=True
            return event
        time.sleep(.2)
    raise RuntimeError('Graceful stop timed out; no SIGKILL was sent')

def apply_plan(plan,evidence,audit_callback=None):
    targets=validate_plan(plan,evidence);result=dict(started_at_utc=utc().isoformat(),status='retiring',signals=[],files_deleted=False,environments_or_weights_deleted=False)
    for name in SERVICES:
        row=next(x for x in targets if x['service']==name)
        if row['status']=='not_listening':continue
        try:
            result['next_signal']=dict(service=row['service'],pid=row['pid'],start_ticks=row['start_ticks'])
            if audit_callback:audit_callback(result)
            event={};result['signals'].append(event)
            stop_one(row,event=event);result.pop('next_signal',None)
            if audit_callback:audit_callback(result)
        except Exception as exc:
            result.update(status='partial_or_refused',error=str(exc));return result
    after=listener_inventory()
    result.update(status='legacy_services_retired',remaining_legacy_listeners=[x for x in after if x['port'] in SERVICES.values()],protected_listeners_after=[x for x in after if x['port'] in PROTECTED_PORTS],finished_at_utc=utc().isoformat())
    if result['remaining_legacy_listeners']:result['status']='partial_or_refused'
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['plan','apply'],default='plan',nargs='?')
    p.add_argument('--output',type=Path);p.add_argument('--plan',type=Path);p.add_argument('--plan-sha256');p.add_argument('--prerequisites',type=Path);p.add_argument('--prerequisites-sha256');p.add_argument('--confirm-retire-legacy',action='store_true')
    args=p.parse_args()
    try:
        require_mount()
        if args.action=='plan':report=build_plan()
        else:
            if not args.confirm_retire_legacy or not all([args.plan,args.plan_sha256,args.prerequisites,args.prerequisites_sha256,args.output]):raise ValueError('Explicit apply requires reviewed plan/readiness hashes, confirmation, and a fresh output path')
            json_path(args.output)
            if args.output.exists():raise FileExistsError('Fresh apply report required')
            for path,expected in [(args.plan,args.plan_sha256),(args.prerequisites,args.prerequisites_sha256)]:
                json_path(path)
                if digest(path)!=expected:raise ValueError('Reviewed operational evidence checksum mismatch')
            write(args.output,dict(status='apply_requested',plan_sha256=args.plan_sha256,prerequisites_sha256=args.prerequisites_sha256,started_at_utc=utc().isoformat(),signals=[]))
            try:report=apply_plan(json.loads(args.plan.read_text()),json.loads(args.prerequisites.read_text()),lambda result:update_owned_report(args.output,result))
            except Exception as exc:report=dict(status='refused',error=str(exc),signals=[],files_deleted=False,environments_or_weights_deleted=False)
            update_owned_report(args.output,report)
        if args.output and args.action=='plan':write(args.output,report)
        print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report.get('status') not in {'partial_or_refused','refused'} else 1
    except Exception as exc:print(json.dumps(dict(status='refused',error=str(exc))),file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())

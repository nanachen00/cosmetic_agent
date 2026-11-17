#!/usr/bin/env python3
"""Control the isolated, private MI300X photo service without touching text APIs."""
from __future__ import annotations
import argparse,fcntl,json,os,re,signal,socket,subprocess,time,urllib.error,urllib.request
from pathlib import Path

ROOT=Path('/mnt/goodagents')
EXPECTED_NFS='192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11'
STATE=ROOT/'outputs/runtime/pids/photo-model.identity.json'
ACTIVE=ROOT/'outputs/runtime/photo-model.active.json'
DEFAULT_SOURCE=ROOT/'blink-runtime/src/qwen-photo-serving-20261116-secure-v2'
DEFAULT_PYTHON=ROOT/'blink-runtime/envs/qwen3-vl-photo-secure-t213-r71/bin/python'


def photo_identity(record):
    try:
        source=Path(record['source']);py=Path(record['python'])
        return (source.is_absolute() and py.is_absolute() and source.resolve().is_relative_to(ROOT)
                and py.resolve().is_relative_to(ROOT)
                and record['command']==[str(py),'-u',str(source/'photo_service.py')])
    except (OSError,KeyError,ValueError,TypeError):return False


def wait_ready(record,seconds):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if not live(record):raise SystemExit('Photo process exited or changed identity')
        payload=health(record['port'])
        if payload and payload.get('ready') is True:return payload
        if payload and payload.get('failed') is True:raise SystemExit('Photo model failed to load; inspect its managed log')
        time.sleep(1)
    raise SystemExit('Photo readiness timeout; inspect status and its managed log')


def activate(record):
    ready_file=Path(record['python']).parent.parent/'runtime-ready.json'
    metadata=json.loads(ready_file.read_text())
    if metadata.get('profile')!='secure-rocm71':raise SystemExit('Only the verified secure runtime profile can become the default')
    value=dict(source=record['source'],python=record['python'],port=8025,instance='photo-model',
               profile=metadata['profile'],native_lock_sha256=metadata['native_lock_sha256'])
    partial=ACTIVE.with_suffix('.partial');partial.write_text(json.dumps(value,indent=2));partial.chmod(0o600);partial.replace(ACTIVE)


def live(record):
    try:
        pid=int(record['pid']);proc=Path('/proc')/str(pid)
        ticks=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
        command=(proc/'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        return ticks==str(record['start_ticks']) and command==[value.encode() for value in record['command']]
    except (OSError,KeyError,ValueError,TypeError,IndexError):return False


def health(port):
    try:
        with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/health',timeout=2) as response:return json.load(response)
    except (OSError,ValueError,urllib.error.HTTPError):return None


def port_open(port):
    with socket.socket() as probe:
        probe.settimeout(.5)
        return probe.connect_ex(('127.0.0.1',port))==0


def owns_loopback(record):
    rows=[]
    for line in subprocess.check_output(['ss','-H','-ltnp',f'sport = :{record["port"]}'],text=True).splitlines():
        fields=line.split()
        if len(fields)<4:return False
        rows.append((fields[3],sorted({int(value) for value in re.findall(r'pid=(\d+)',line)})))
    return rows==[('127.0.0.1:'+str(record['port']),[record['pid']])]


def stop(record,state):
    if not live(record):raise SystemExit('Process identity differs; refusing to signal an unknown process')
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        raise SystemExit('pidfd support required; no numeric PID signal fallback')
    descriptor=os.pidfd_open(record['pid'],0)
    try:
        if not live(record):raise SystemExit('Photo PID identity changed after pidfd acquisition')
        if not owns_loopback(record):raise SystemExit('Photo loopback listener owner differs; refusing to signal')
        current=health(record['port'])
        if not isinstance(current,dict) or current.get('busy') is not False:
            raise SystemExit('Cannot confirm photo inference is idle; retry after it completes')
        signal.pidfd_send_signal(descriptor,signal.SIGTERM,None,0)
    finally:os.close(descriptor)
    for _ in range(50):
        if not live(record) and not port_open(record['port']):break
        time.sleep(.1)
    if live(record):raise SystemExit('Photo process did not stop; no forced kill sent')
    if port_open(record['port']):raise SystemExit('Photo process stopped but its port remains occupied; no other process was signalled')
    state.unlink();print('PHOTO_STOPPED')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('start','status','stop'))
    parser.add_argument('--source',type=Path)
    parser.add_argument('--python',type=Path)
    parser.add_argument('--port',type=int,default=8025);parser.add_argument('--wait',type=int,default=180)
    parser.add_argument('--instance',default='photo-model')
    parser.add_argument('--activate',action='store_true')
    args=parser.parse_args()
    if len(args.instance)>48 or not re.fullmatch(r'photo-model(?:-[a-z0-9]+)*',args.instance):raise SystemExit('Invalid managed photo instance name')
    if args.activate and (args.action!='start' or args.instance!='photo-model' or args.port!=8025):raise SystemExit('Activation is only valid for the canonical photo service')
    state=STATE.with_name(args.instance+'.identity.json')
    mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(ROOT)],text=True))['filesystems'][0]
    if (mount['target']!=str(ROOT) or mount['fstype'] not in {'nfs','nfs4'}
            or mount.get('source')!=EXPECTED_NFS or 'rw' not in mount.get('options','').split(',')):
        raise SystemExit('Expected writable school NFS mount missing; refusing control operations')
    active=json.loads(ACTIVE.read_text()) if ACTIVE.exists() else {}
    args.source=args.source or Path(active.get('source',str(DEFAULT_SOURCE)))
    args.python=args.python or Path(active.get('python',str(DEFAULT_PYTHON)))
    if not 1<=args.port<=65535:raise SystemExit('Invalid port')
    state.parent.mkdir(parents=True,exist_ok=True)
    with state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record=json.loads(state.read_text()) if state.exists() else None
        if record and not photo_identity(record):raise SystemExit('Managed identity is not an isolated photo service; refusing to use it')
        if args.action=='status':
            running=bool(record and live(record))
            print(json.dumps({'running':running,'health':health(record['port']) if running else None,
                              'identity':record,'scope':'Private experimental photo model; no clinical quality claim'}))
            return
        if args.action=='stop':
            if not record:return
            stop(record,state);return
        if record and live(record):
            if record['port']!=args.port or record['source']!=str(args.source.resolve()) or record['python']!=str(args.python):
                raise SystemExit('Managed photo process uses a different configuration; refusing to replace it')
            payload=wait_ready(record,args.wait)
            if args.activate:activate(record)
            print(json.dumps({'running':True,'health':payload,'pid':record['pid']}));return
        source=args.source.resolve();py=args.python
        if not source.is_relative_to(ROOT) or not py.resolve().is_relative_to(ROOT):raise SystemExit('Photo source and Python must be persistent')
        if not (source/'photo_service.py').is_file() or not py.is_file():raise SystemExit('Photo source/Python missing')
        if port_open(args.port):raise SystemExit('Photo port occupied; refusing to take over an unknown service')
        log=ROOT/'outputs/runtime/logs'/(args.instance+'.log');log.parent.mkdir(parents=True,exist_ok=True)
        env=dict(os.environ,PHOTO_MODEL_PATH=str(ROOT/'models/qwen3-vl-4b-instruct'),PHOTO_ADAPTER_PATH='',
                 PHOTO_MODEL_BIND_HOST='127.0.0.1',PHOTO_MODEL_PORT=str(args.port),HF_HOME=str(ROOT/'hf-cache'),
                 HF_HUB_OFFLINE='1',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4',TOKENIZERS_PARALLELISM='false')
        for name in ('PYTHONPATH','PYTHONHOME','LD_PRELOAD'):env.pop(name,None)
        env['PYTHONNOUSERSITE']='1'
        # Do not inherit the text engine's different Torch library directory.
        library=subprocess.check_output([str(py),'-c','import pathlib,importlib.util; print(pathlib.Path(importlib.util.find_spec("torch").origin).parent/"lib")'],text=True,env=env,timeout=30).strip()
        env['LD_LIBRARY_PATH']=library+':/opt/rocm/lib:/opt/rocm/lib64'
        subprocess.run([str(py),'-c','import torch; assert torch.version.hip and torch.cuda.is_available() and "MI300X" in torch.cuda.get_device_name(0)'],check=True,env=env,timeout=180)
        command=[str(py),'-u',str(source/'photo_service.py')]
        with log.open('ab',buffering=0) as output:process=subprocess.Popen(command,cwd=source,env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        ticks=(Path('/proc')/str(process.pid)/'stat').read_text().rsplit(')',1)[1].split()[19]
        record=dict(pid=process.pid,start_ticks=ticks,command=command,source=str(source),python=str(py),port=args.port,log=str(log),adapter=None)
        partial=state.with_suffix('.partial');partial.write_text(json.dumps(record,indent=2));partial.chmod(0o600);partial.replace(state)
        print('PHOTO_STARTING',process.pid,flush=True)
        wait_ready(record,args.wait)
        if args.activate:activate(record)
        print('PHOTO_READY',flush=True)


if __name__=='__main__':main()

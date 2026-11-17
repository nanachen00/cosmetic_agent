"""Install an isolated Python 3.11 photo runtime from official ROCm wheels.

Does not modify training/vLLM environments or download model weights. Vendor
hashes are checked where the official index supplies them; their absence is
recorded, never represented as an upstream verification.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from email import message_from_bytes
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parent
PROFILES={
    'secure-rocm71':{'torch':'2.13.0+rocm7.1','torchvision':'0.28.0+rocm7.1','triton-rocm':'3.7.1'},
    'legacy-rocm64':{'torch':'2.9.1+rocm6.4','torchvision':'0.24.1+rocm6.4','pytorch-triton-rocm':'3.5.1'},
}
EXPECTED_NFS='192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11'


def require_persistent_mount():
    root=Path('/mnt/goodagents')
    mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(root)],text=True))['filesystems'][0]
    if (mount['target']!=str(root) or mount['fstype'] not in {'nfs','nfs4'}
            or mount.get('source')!=EXPECTED_NFS or 'rw' not in mount.get('options','').split(',')):
        raise RuntimeError('Expected writable school NFS mount missing')
    return mount


def sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--sources',type=Path,required=True)
    parser.add_argument('--venv',type=Path,required=True)
    parser.add_argument('--profile',choices=PROFILES,default='secure-rocm71')
    parser.add_argument('--python-lock',type=Path)
    parser.add_argument('--wheelhouse',type=Path);args=parser.parse_args()
    expected=PROFILES[args.profile]
    root=Path('/mnt/goodagents')
    mount=require_persistent_mount()
    if sys.version_info[:2]!=(3,11) or sys.platform!='linux' or platform.machine()!='x86_64':
        raise RuntimeError('Use the persistent Linux Python 3.11')
    if not Path(sys.executable).resolve().is_relative_to(root) or not Path(sys.base_prefix).resolve().is_relative_to(root):
        raise RuntimeError('Base Python executable and standard library must be persistent')
    target=args.venv.resolve()
    if target.parent!=root/'blink-runtime/envs':raise ValueError('Use a fresh persistent runtime directory')
    if target.exists() and any(target.iterdir()):raise FileExistsError('Refuse to modify an existing/live environment')
    sources=json.loads(args.sources.read_text())
    if set(sources)!=set(expected):raise ValueError('Unexpected native package selection')
    lock_name='requirements-photo-native-rocm71-py311.lock' if args.profile=='secure-rocm71' else 'requirements-photo-native-py311.lock'
    pinned={}
    for line in (ROOT/lock_name).read_text().splitlines():
        if not line.strip() or line.startswith('#'):continue
        url,checksum=line.split(' --hash=sha256:')
        pinned[url]=checksum
    default_cache=root/'blink-runtime/data/wheels'/('photo-native-py311-'+args.profile)
    if args.profile=='legacy-rocm64':default_cache=root/'data/training/photo-native-py311-rocm64'
    wheelhouse=(args.wheelhouse or default_cache).resolve()
    if not wheelhouse.is_relative_to(root):raise ValueError('Native wheel cache must be persistent')
    wheelhouse.mkdir(parents=True,exist_ok=True)
    source_id=sha256(args.sources)[:16]
    previous_file=wheelhouse/('provenance-'+args.profile+'-'+source_id+'.json')
    def download(item):
        name,record=item;url,fragment=urllib.parse.urldefrag(record['url']);parsed=urllib.parse.urlparse(url)
        if parsed.scheme!='https' or parsed.hostname not in {'download.pytorch.org','download-r2.pytorch.org'}:
            raise ValueError('Native wheel must come from the official PyTorch index')
        path=wheelhouse/Path(urllib.parse.unquote(parsed.path)).name
        vendor=urllib.parse.parse_qs(fragment).get('sha256',[None])[0]
        wanted=pinned.get(url)
        if not wanted or vendor and vendor!=wanted:raise ValueError('Official wheel differs from the tested native hash lock')
        if not path.exists():
            partial=path.with_suffix('.whl.partial')
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(url,timeout=60) as source,partial.open('wb') as output:
                        final=urllib.parse.urlparse(source.geturl())
                        if final.scheme!='https' or final.hostname not in {'download.pytorch.org','download-r2.pytorch.org'}:raise ValueError('Unexpected native wheel redirect')
                        while chunk:=source.read(4*1024*1024):output.write(chunk)
                    break
                except OSError:
                    if attempt==2:raise
                    time.sleep(attempt+1)
            actual=sha256(partial)
            if wanted and actual!=wanted:raise RuntimeError('Native wheel checksum mismatch: '+name)
            partial.replace(path)
        actual=sha256(path)
        if wanted and actual!=wanted:raise RuntimeError('Cached native wheel checksum mismatch: '+name)
        with zipfile.ZipFile(path) as archive:
            files=[entry for entry in archive.namelist() if entry.endswith('.dist-info/METADATA')]
            if len(files)!=1:raise ValueError('Invalid native wheel metadata')
            metadata=message_from_bytes(archive.read(files[0]))
            if metadata['Name'].replace('_','-').lower()!=name or metadata['Version']!=expected[name]:
                raise ValueError('Native wheel name/version mismatch')
        value={'source_url':url,'official_index':record['index'],'version':expected[name],
               'path':str(path),'bytes':path.stat().st_size,'sha256':actual,'vendor_sha256_available':bool(vendor)}
        print('NATIVE_WHEEL_VERIFIED',name,value['bytes'],'vendor_hash',bool(vendor),flush=True)
        return name,value
    with ThreadPoolExecutor(max_workers=2) as pool:records=dict(pool.map(download,sources.items()))
    previous_file.write_text(json.dumps(records,indent=2))
    lock=wheelhouse/('native-'+args.profile+'-'+source_id+'.lock')
    lock_text='\n'.join(record['path']+' --hash=sha256:'+record['sha256'] for record in records.values())+'\n'
    try:
        with lock.open('x') as output:output.write(lock_text)
    except FileExistsError:
        if lock.read_text()!=lock_text:raise ValueError('Existing native install lock differs')
    subprocess.run([sys.executable,'-m','venv',str(target)],check=True)
    py=target/'bin/python'
    python_lock=(args.python_lock or ROOT/'requirements-photo-linux-py311.lock').resolve()
    if not python_lock.is_relative_to(root):raise ValueError('Python dependency lock must be persistent')
    if not python_lock.is_file():raise FileNotFoundError('The tested Python 3.11 hash lock is required')
    python_wheels=root/'data/training/wheels'/sha256(python_lock)[:16]
    python_wheels.mkdir(parents=True,exist_ok=True)
    install_env=dict(os.environ,PIP_INDEX_URL='https://pypi.org/simple')
    install_env.pop('PIP_EXTRA_INDEX_URL',None);install_env.pop('PIP_FIND_LINKS',None)
    for name in ('PYTHONPATH','PYTHONHOME','LD_PRELOAD','LD_LIBRARY_PATH'):install_env.pop(name,None)
    install_env['PYTHONNOUSERSITE']='1'
    subprocess.run([str(py),'-m','pip','download','--disable-pip-version-check','--only-binary=:all:',
                    '--require-hashes','--no-deps','-r',str(python_lock),'--dest',str(python_wheels)],check=True,env=install_env)
    subprocess.run([str(py),'-m','pip','install','--no-index','--no-deps','--require-hashes','-r',str(lock),
                    '--report',str(target/'native-install-report.json')],check=True,env=install_env)
    subprocess.run([str(py),'-m','pip','install','--no-index','--find-links',str(python_wheels),
                    '--require-hashes','--no-deps','-r',str(python_lock),
                    '--report',str(target/'python-install-report.json')],check=True,env=install_env)
    subprocess.run([str(py),'-m','pip','check'],check=True,env=install_env)
    installed=json.loads(subprocess.check_output([str(py),'-c','import importlib.metadata,json; print(json.dumps({name:importlib.metadata.version(name) for name in '+repr(list(expected))+'}))'],text=True,env=install_env))
    if installed!=expected:raise RuntimeError('Installed native versions differ from the selected profile')
    library=subprocess.check_output([str(py),'-c','import importlib.util,pathlib; print(pathlib.Path(importlib.util.find_spec("torch").origin).parent/"lib")'],text=True,env=install_env,timeout=30).strip()
    gpu_env=dict(install_env,LD_LIBRARY_PATH=library+':/opt/rocm/lib:/opt/rocm/lib64',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
    subprocess.run([str(py),'-c','import torch; assert torch.version.hip and torch.cuda.is_available(); assert "MI300X" in torch.cuda.get_device_name(0); a=torch.randn(128,128,device="cuda"); b=a@a.T; assert torch.isfinite(b).all(); torch.cuda.synchronize(); print("PHOTO_GPU_RUNTIME_OK",torch.__version__,torch.version.hip,torch.cuda.get_device_name(0))'],check=True,env=gpu_env,timeout=180)
    (target/'runtime-ready.json').write_text(json.dumps({'native_wheels':records,'mount':mount,'python':'3.11','profile':args.profile,
        'native_lock_sha256':sha256(ROOT/lock_name),
        'native_provenance':str(previous_file),'installed_native_versions':installed,
        'python_lock_sha256':sha256(python_lock),'python_wheelhouse':str(python_wheels),
        'scope':'Isolated photo runtime; model/API validation is separate.',
        'remaining_security_review':'Legacy Torch 2.9.1 remains vulnerable and is for research reproduction only.' if args.profile=='legacy-rocm64' else 'Per-environment advisory scan and full model/API verification are separate gates; installation alone is not a security certificate.'},indent=2))
    print('PHOTO_RUNTIME_INSTALLED',target,flush=True)


if __name__=='__main__':main()

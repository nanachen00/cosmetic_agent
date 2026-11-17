"""Create a fresh, hash-locked training venv using an existing ROCm binary stack.

Run with the persistent ROCm Python, not the Mac Python. This deliberately
refuses to download replacement Torch/CUDA packages or alter a live venv.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import distribution,version
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--venv',type=Path,required=True)
    parser.add_argument('--check-only',action='store_true');args=parser.parse_args()
    lock=json.loads((ROOT/'rocm-binaries.lock.json').read_text());persistent=Path('/mnt/goodagents')
    mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(persistent)],text=True))['filesystems'][0]
    if mount['target']!=str(persistent) or mount['fstype'] not in {'nfs','nfs4'}:
        raise RuntimeError('Persistent /mnt/goodagents NFS mount is required')
    if sys.platform!='linux' or platform.machine()!='x86_64' or sys.version_info[:2]!=(3,10):
        raise RuntimeError('This lock requires Linux x86_64 and the existing Python 3.10 ROCm stack')
    import torch
    if not torch.version.hip or not torch.version.hip.startswith(lock['hip_major_minor']+'.') \
            or not torch.cuda.is_available() or 'MI300X' not in torch.cuda.get_device_name(0):
        raise RuntimeError('Expected working MI300X / ROCm runtime is unavailable')
    actual={name:version(name) for name in lock['packages']}
    if actual!=lock['packages']:raise RuntimeError('Native ROCm package versions do not match the recorded build')
    site=Path(torch.__file__).resolve().parent.parent
    target=args.venv.resolve()
    if not site.is_relative_to(persistent) or not Path(sys.executable).resolve().is_relative_to(persistent):
        raise RuntimeError('The parent Python and native packages must already be persistent')
    if target.parent!=persistent/'blink-runtime/envs' or target.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError('Choose a fresh venv directly inside /mnt/goodagents/blink-runtime/envs')
    if args.check_only:
        print(json.dumps({'mount':mount,'gpu':torch.cuda.get_device_name(0),'native_packages':actual,'parent_site':str(site)},indent=2));return
    if target.exists() and any(target.iterdir()):raise FileExistsError('Do not mutate an existing/live environment; choose a fresh venv')
    subprocess.run([sys.executable,'-m','venv',str(target)],check=True)
    py=target/'bin/python'
    target_site=Path(subprocess.check_output([str(py),'-c','import sysconfig; print(sysconfig.get_path("purelib"))'],text=True).strip())
    sources={}
    def link_or_copy(source,destination):
        # Distinct directory entries prevent a future pip uninstall from
        # traversing a package-directory symlink into the parent's runtime.
        try:os.link(source,destination)
        except OSError:shutil.copy2(source,destination)
        return destination
    for name in ('torch','torchgen','functorch','torchvision','torchvision.libs','triton'):
        source=site/name
        if source.exists():
            shutil.copytree(source,target_site/name,copy_function=link_or_copy,symlinks=True);sources[name]=str(source)
    for name in actual:
        package=distribution(name)
        candidates=list(Path(package.locate_file('')).glob(name.replace('-','_')+'-*.dist-info'))
        if len(candidates)!=1:raise RuntimeError('Cannot identify native metadata: '+name)
        source=candidates[0];shutil.copytree(source,target_site/source.name);sources[source.name]=str(source)
    lockfile=ROOT/'requirements-linux-rocm.lock'
    wheelhouse=persistent/'data/training/wheels'/hashlib.sha256(lockfile.read_bytes()).hexdigest()[:16]
    wheelhouse.mkdir(parents=True,exist_ok=True)
    subprocess.run([str(py),'-m','pip','download','--disable-pip-version-check','--require-hashes','--no-deps',
                    '-r',str(lockfile),'--dest',str(wheelhouse)],check=True)
    subprocess.run([str(py),'-m','pip','install','--disable-pip-version-check','--no-index','--find-links',str(wheelhouse),
                    '--require-hashes','--no-deps','-r',str(lockfile),'--report',str(target/'install-report.json')],check=True)
    subprocess.run([str(py),'-m','pip','check'],check=True)
    subprocess.run([str(py),'-c','import torch,transformers,peft; print("RUNTIME_OK",torch.cuda.get_device_name(0),torch.version.hip,transformers.__version__,peft.__version__)'],check=True)
    (target/'runtime-sources.json').write_text(json.dumps({'mount':mount,'native_packages':actual,'native_package_sources':sources,
        'verified_wheelhouse':str(wheelhouse),'python_lock_sha256':hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        'security_limitations':lock['known_security_limit'],'scope':'Reproduced dependency installation with the recorded ROCm base; not a container rebuild test.'},indent=2))
    print('HASH_LOCKED_ENV_READY',target)


if __name__=='__main__':main()

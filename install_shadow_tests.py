#!/usr/bin/env python3
"""Install the deployment-tolerant Shadow test tools; production source is NOT inspected. No HTTP, service or package actions.

Default target: /var/lib/jenkins/tomod/ops/local-llm-path-benchmark
Does not include or delete .private, .state, runs, diagnostics, workflows or LAST_RUN.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile
import tempfile

ARCHIVE_SHA256 = 'e3e87bc70a5e98f8c6ee42eb2175372ab456352404501f270ee39d0ec6dfffa8'
ROOT_NAME = "local-llm-path-benchmark"
BASELINE = {'MANIFEST.sha256': ['a828bb95b9f98b73c9b020893ea9455aa118bcd740e31f7e3a753d8167b1c25c', '011ae8a2f6ef45b298eff50b38c18c1e53fd1bd50535ac62361cc49233a6b88d'], 'README.md': ['309e8f5596f58408536fabe415c32d9b3f073ffc7fc59e4caca9e60fdac24718'], 'SMOKE_REVIEW.md': ['d6b6095b6b910ba26013795eb25251f7618dd6d5cf78f399070fc8c82c1869ca'], 'SOURCE_NOTES.md': ['1b06c446baeea6e6e91a732e6988a96e824e98f7bceca05ba27a0e4921a28f12'], 'TEST_REPORT.md': ['b30ff823f4eea9de1f15a1130958f62c2dcbacad3a27ad09bb7445b4cee8c5c2'], 'accounts.example.json': ['3f254f4be135ccc28f3bfad39071b87462617cbd2014a1c2e17cdaf502b070d6'], 'auth_clients.py': ['643618ecbdff50ed359dcde26155192000ef62c954922933a6e3e8351ce177e9'], 'bench.py': ['1ee0eb6d0d30539b259e83fef53630c7b2c7e1bc2e84045e6ae2a733c075fd03'], 'common.py': ['48f1d280ba39e5fd8d1029ae52e8c7a8a23802e86c265d5de9e6bccbdf9768fd'], 'compat_gateway.py': ['2d09d44cd26f92b6ce4c91c3be118949c38815d08a4e0e8e1cec72c6f26bc5fa'], 'config.json': ['36869d143889560342c5850126fcd91ec560da6c92f310d8dff579c06cc59aef'], 'config_prepare.py': ['5400e6bfb8da43a79fc785bd08d70635d88095523d6f87247589a205c8b1fce4'], 'configure_test_accounts.py': ['de499db25dc84fa41e23bbe71b8e0648fbd86df2d1125cd209c536882cc44df1'], 'pack_results.sh': ['9984658fc48c23b45a2c5defaa1a89af38e640956ecd655cc60534d129731492'], 'process_control.py': ['c78844f3e0c325141134bed6d4a02541493a6a35c7cc3dc79f8384dbc3d89841'], 'profile_backend.py': ['93d914e5e170eb47be6f673098153934a41514e1d82e222fe93ac060d8b14342'], 'redis_local.py': ['354a3981e80e644df8bb6e4fd0710a4e7d0e9925e0b1049eeb64d1dffb9ebf74'], 'restore_test_state.sh': ['77054c2e7596280aea76c907801e8b67f18ad53f7696b3fc67298b7cc7f6d2dc'], 'run_local_provider_path_benchmark.sh': ['a1b3e83f8d8d911f6d61fa10407a1af75541b1a9b315a7c475c0ceabf601e796'], 'run_verified_shadow_tests.sh': ['ea423012d37a59ceb2c64567ab6ca09b5194619435aedcdd01ce260dee16b71c'], 'selftest.sh': ['71a0fb36a2dd63771a30393b2fa2287ed2865d96c5f260a5da457f253793a03f'], 'shadow_bootstrap.py': ['dccefc603fa2900fe151a9f0634212b0a4305f0bf4fb3a354d4ff32abea147ec'], 'shadow_copy.py': ['0bc568d6b7b85394b94e7f582a168bda90f4b1960a2430f2a04173cec06d4033'], 'shadow_entry.py': ['dfcff8644948543e0c301d945d0591bfd917351d69ad8ba05e9c1a36d2e9d99d'], 'shadow_guard.py': ['7a00fe3e6d453c7f0d84657cee2baeba58233fc9c1cf003dbea15a5f2e2e96bb'], 'smoke_checks.py': ['84cbc00b91984e2e0cd945456bbfe4ab50d1b46c0f4a0a30a261bec8fadc0fb3'], 'source_contract.json': ['11cf62252403ac24faf81b56107bf0bf078b965f0a31fb216b4133e552a317b9', '9d2dfc5bff14293d0fa2f07c219b377e15c2e86a4e6303611816373456271a08'], 'telemetry.py': ['ea7d06c057d40bbedd71686a75cc476ae9a77a99c4d39258ab9b70d2d80b8d07'], 'tests/test_auth_workflow.py': ['91ee92f1858a4540eb0aac821a3079b8018085e6f49da36e65a7de61aaa943b8'], 'tests/test_bundle.py': ['2093b6b0c1ce732edfb5d4f1f4a1afe6cab8cf4ba656cc43d9d3a2c76cf55c6d']}

LOCKS = ('.verified-workflow.lock', '.benchmark.lock')
RESERVED = {'.private', '.state', 'runs', 'diagnostics', 'workflows', 'backups', 'LAST_RUN'}

class Refuse(RuntimeError):
    pass

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def no_symlink(path: Path) -> None:
    for p in [path, *path.parents]:
        if p.is_symlink():
            raise Refuse('Symlink path refused: '+str(p))

def read_archive(path: Path) -> dict[str, bytes]:
    no_symlink(path)
    if not path.is_file() or path.stat().st_size > 8*1024*1024:
        raise Refuse('Archive missing or exceeds reviewed size limit')
    if sha(path.read_bytes()) != ARCHIVE_SHA256:
        raise Refuse('Archive SHA-256 is not this reviewed delivery')
    files={};seen=set();total=0
    with tarfile.open(path, 'r:gz') as tf:
        for m in tf.getmembers():
            p=PurePosixPath(m.name)
            if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!=ROOT_NAME:
                raise Refuse('Unexpected archive path')
            if m.name in seen:raise Refuse('Duplicate archive entry')
            seen.add(m.name)
            if not(m.isfile() or m.isdir()):raise Refuse('Non-regular archive member refused')
            if m.isdir():continue
            rel=PurePosixPath(*p.parts[1:]).as_posix()
            if rel=='.' or p.parts[1] in RESERVED or any(x.startswith('.') for x in p.parts[1:]):
                raise Refuse('Private/runtime content in code archive refused')
            if m.size>512*1024:raise Refuse('Unexpected large code file')
            total+=m.size
            if total>4*1024*1024:raise Refuse('Archive expanded-size limit exceeded')
            stream=tf.extractfile(m)
            if stream is None:raise Refuse('Archive file could not be read')
            files[rel]=stream.read()
    if 'MANIFEST.sha256' not in files:raise Refuse('Manifest missing')
    manifest={}
    for line in files['MANIFEST.sha256'].decode('utf-8').splitlines():
        digest,rel=line.split('  ',1)
        if rel in manifest or len(digest)!=64:raise Refuse('Manifest format invalid')
        manifest[rel]=digest
    if set(manifest)!=(set(files)-{'MANIFEST.sha256'}):raise Refuse('Archive/manifest membership mismatch')
    for rel,digest in manifest.items():
        if sha(files[rel])!=digest:raise Refuse('Archive manifest mismatch: '+rel)
    return files


def check_target(target: Path) -> None:
    no_symlink(target)
    if target.name!=ROOT_NAME or target.parent.name!='ops':
        raise Refuse('Target must be the dedicated ops/local-llm-path-benchmark test directory')
    if target.exists() and not target.is_dir():raise Refuse('Target is not a directory')
    for name in ('backups','.state'):
        p=target/name;no_symlink(p)
        if p.exists() and not p.is_dir():raise Refuse('Expected runtime directory: '+str(p))
    private_state=target/'.state'
    if private_state.is_dir():
        for journal in private_state.glob('*/journal.json'):
            no_symlink(journal)
            try:
                if journal.stat().st_size>2*1024*1024:raise Refuse('Unexpected journal size')
                obj=json.loads(journal.read_text(encoding='utf-8'))
            except (ValueError,OSError):
                raise Refuse('Unreadable recovery journal; resolve it before updating') from None
            if obj.get('cleanup_pass') is not True:
                raise Refuse('Unresolved recovery state: '+str(journal.parent))

@contextmanager
def locks(target: Path, create: bool):
    handles=[]
    try:
        for name in LOCKS:
            p=target/name;no_symlink(p)
            if not p.exists() and not create:continue
            flags=os.O_RDWR | (os.O_CREAT if create else 0) | getattr(os,'O_NOFOLLOW',0)
            fd=os.open(p,flags,0o600);handles.append(fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):raise Refuse('Lock is not a regular file')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Refuse('Benchmark/workflow/recovery is active; no files replaced') from None
        yield
    finally:
        for fd in reversed(handles):os.close(fd)


def plan(target: Path, files: dict[str, bytes]) -> list[dict]:
    check_target(target);rows=[]
    known_existing=[rel for rel in BASELINE if (target/rel).exists()]
    partial=bool(known_existing)
    for rel,data in files.items():
        p=target/rel;no_symlink(p)
        old_hash=None
        if p.exists():
            if not p.is_file() or p.stat().st_size>512*1024:
                raise Refuse('Unexpected target file type/size: '+rel)
            old_hash=sha(p.read_bytes())
            if old_hash not in set(BASELINE.get(rel,[])) | {sha(data)}:
                raise Refuse('Unknown local edits in '+rel+'; refusing overwrite')
        elif partial and rel in BASELINE:
            raise Refuse('Partial existing installation, required file missing: '+rel)
        rows.append({'path':rel,'before_sha256':old_hash,'after_sha256':sha(data),'change':old_hash!=sha(data)})
    return rows


def atomic_write(path: Path, data: bytes, previous_stat=None) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    no_symlink(path)
    fd,temp=tempfile.mkstemp(prefix='.shadow-code-new-',dir=path.parent)
    try:
        mode=stat.S_IMODE(previous_stat.st_mode) if previous_stat else (0o755 if path.suffix=='.sh' else 0o644)
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
            os.fchmod(f.fileno(),mode)
            if previous_stat and (previous_stat.st_uid!=os.geteuid() or previous_stat.st_gid!=os.getegid()):
                os.fchown(f.fileno(),previous_stat.st_uid,previous_stat.st_gid)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def install(archive: Path, target: Path, check_only: bool=False) -> dict:
    files=read_archive(archive);check_target(target)
    if check_only:
        with locks(target,create=False):rows=plan(target,files)
        return {'mode':'check_only','pass':True,'changes':sum(r['change'] for r in rows),'service_actions':0,'files_written':0}
    target.mkdir(parents=True,exist_ok=True)
    with locks(target,create=True):
        rows=plan(target,files);changed=[r for r in rows if r['change']]
        if not changed:return {'mode':'already_current','pass':True,'changes':0,'service_actions':0}
        tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        backup=target/'backups'/('deployment-tolerant-update-'+tag)
        backup.mkdir(parents=True,exist_ok=False,mode=0o700)
        snapshots={};stats={};written=[]
        # Read/backup ALL previous bytes before replacing ANY managed file.
        for r in changed:
            p=target/r['path'];no_symlink(p)
            raw=p.read_bytes() if p.exists() else None
            if (sha(raw) if raw is not None else None)!=r['before_sha256']:
                raise Refuse('Source changed during installation planning: '+r['path'])
            snapshots[r['path']]=raw;stats[r['path']]=p.stat() if raw is not None else None
            if raw is not None:
                dest=backup/r['path'];dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(raw);dest.chmod(0o600)
        (backup/'installation-plan.json').write_text(json.dumps({'archive_sha256':ARCHIVE_SHA256,'files':rows},indent=2)+'\n')
        try:
            # Manifest committed last so an interrupted install cannot falsely verify.
            changed.sort(key=lambda r:(r['path']=='MANIFEST.sha256',r['path']))
            for r in changed:
                rel=r['path'];p=target/rel
                present=p.read_bytes() if p.exists() else None
                if (sha(present) if present is not None else None)!=r['before_sha256']:
                    raise Refuse('Local file changed during installation: '+rel)
                atomic_write(p,files[rel],stats[rel]);written.append(rel)
            for rel,data in files.items():
                if sha((target/rel).read_bytes())!=sha(data):raise Refuse('Post-install hash mismatch: '+rel)
        except BaseException:
            failures=[]
            for rel in reversed(written):
                try:
                    if snapshots[rel] is None:(target/rel).unlink()
                    else:atomic_write(target/rel,snapshots[rel],stats[rel])
                except Exception:failures.append(rel)
            if failures:
                (backup/'ROLLBACK_FAILED.json').write_text(json.dumps({'paths':failures}))
                raise Refuse('Partial code rollback failed; use preserved backups; do not start benchmark') from None
            raise
        return {'mode':'installed','pass':True,'changes':len(changed),'backup':str(backup),'service_actions':0,
                'preserved':'unmanaged files; .private, .state, runs, diagnostics, workflows, LAST_RUN'}


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--archive',required=True)
    ap.add_argument('--target',default='/var/lib/jenkins/tomod/ops/local-llm-path-benchmark')
    ap.add_argument('--check-only',action='store_true')
    a=ap.parse_args();os.umask(0o077)
    result=install(Path(a.archive).absolute(),Path(a.target).absolute(),a.check_only)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except (Refuse,OSError,ValueError,tarfile.TarError) as exc:
        print('[REFUSED]',str(exc),file=sys.stderr);raise SystemExit(1)
    except KeyboardInterrupt:
        print('[INTERRUPTED] Review backup/manifest before rerunning; no service commands were issued',file=sys.stderr)
        raise SystemExit(130)

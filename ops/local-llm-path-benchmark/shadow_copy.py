"""Literal relocation helpers for independent Shadow bytes.
The concurrent runner uses frozen_snapshot.capture/verify, NOT a historical SHA
preflight gate. verify_contract is retained only as an explicit diagnostic helper.
Sanitized uploaded code is never deployed as live source.
"""
from __future__ import annotations
import ast
import os
from pathlib import Path
import shutil
import tokenize
from io import StringIO
from common import Refuse, atomic_json, sha_file

DIRS = ('api','agents','app','core','database','tools','models','schemas','chatgpt',
        'content','prompts','input','data','config','configs')
SKIP = {'.git','frontend','node_modules','venv','.venv','__pycache__','.ipynb_checkpoints',
        '.pytest_cache','logs','tests','test','build','dist','.state','backups'}
EXT = {'.py','.pyi','.json','.yaml','.yml','.toml','.ini','.cfg','.conf','.md','.txt','.sql','.csv'}

def source_files(root: Path):
    for parent,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[x for x in dirs if x not in SKIP and not x.startswith('.')]
        for name in sorted(files):
            p=Path(parent)/name
            if '.bak' in name or name.endswith(('.orig','.save','~')): continue
            if p.suffix in EXT or name.startswith('.env'):
                if p.is_symlink():
                    target=p.resolve(strict=True)
                    if not target.is_relative_to(root.resolve()):
                        raise Refuse('External symlink in source/config: '+str(p.relative_to(root)))
                yield p

def fingerprints(root: Path) -> dict:
    return {p.relative_to(root).as_posix():sha_file(p) for p in source_files(root)}

def verify_contract(root: Path, contract: dict) -> dict:
    mismatches=[]
    for rel,digest in contract['critical_original_sha256'].items():
        p=root/rel
        actual=sha_file(p) if p.is_file() else None
        if actual!=digest: mismatches.append({'path':rel,'expected':digest,'actual':actual})
    return {'pass':not mismatches,'snapshot':contract['snapshot'],'mismatches':mismatches}

def rewrite_literals(text: str, original_root: str, shadow_root: str, proxy_port: int):
    # Only Python string literals: no regex rewriting control flow or function bodies.
    tokens=list(tokenize.generate_tokens(StringIO(text).readline))
    lines=text.splitlines(keepends=True); starts=[]; offset=0
    for line in lines: starts.append(offset); offset+=len(line)
    changes=[]
    for t in tokens:
        if t.type != tokenize.STRING: continue
        try: value=ast.literal_eval(t.string)
        except (ValueError,SyntaxError): continue
        if not isinstance(value,str): continue
        new=value.replace(original_root,shadow_root)
        new=new.replace('http://localhost:8003/v1',f'http://127.0.0.1:{proxy_port}/v1')
        new=new.replace('http://127.0.0.1:8003/v1',f'http://127.0.0.1:{proxy_port}/v1')
        if new != value:
            a=starts[t.start[0]-1]+t.start[1]; b=starts[t.end[0]-1]+t.end[1]
            changes.append((a,b,repr(new),t.start[0]))
    for a,b,v,_ in reversed(changes): text=text[:a]+v+text[b:]
    ast.parse(text)
    return text,[x[3] for x in changes]

def clone(root: Path, dest: Path, proxy_port: int, report: Path) -> dict:
    root=root.resolve(strict=True)
    if dest.exists(): raise Refuse('Shadow source destination already exists')
    if dest.resolve().is_relative_to(root): raise Refuse('Shadow destination must be outside production source')
    dest.mkdir(parents=True,mode=0o700)
    copied={}; changes=[]; unparsed=[]
    for p in source_files(root):
        rel=p.relative_to(root)
        if len(rel.parts)>1 and rel.parts[0] not in DIRS: continue
        if p.stat().st_size>16*1024*1024: raise Refuse('Oversized text asset needs review: '+rel.as_posix())
        q=dest/rel; q.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        data=p.read_bytes(); q.write_bytes(data); q.chmod(0o600)
        if os.stat(q).st_ino==os.stat(p).st_ino and os.stat(q).st_dev==os.stat(p).st_dev:
            raise Refuse('Copy shares source inode')
        copied[rel.as_posix()]=sha_file(q)
        if p.suffix=='.py':
            try: text=data.decode('utf-8-sig'); patched,lines=rewrite_literals(text,str(root),str(dest.resolve()),proxy_port)
            except (SyntaxError,UnicodeDecodeError,tokenize.TokenError) as exc:
                if str(root) in data.decode(errors='replace') or ':8003/v1' in data.decode(errors='replace'):
                    raise Refuse('Live path-bearing Python cannot be safely relocated: '+rel.as_posix()) from None
                unparsed.append(rel.as_posix()); continue
            if lines:
                q.write_text(patched,encoding='utf-8')
                changes.append({'path':rel.as_posix(),'lines':lines,'before_sha256':copied[rel.as_posix()],
                                'after_sha256':sha_file(q),'kind':'path_or_proxy_string_literal_only'})
    if not (dest/'api/api_server.py').is_file(): raise Refuse('Shadow API entrypoint missing')
    result={'copied_count':len(copied),'changes':changes,'unparsed_unmodified_helpers':unparsed,'source_mode':'live_copy_not_sanitized_snapshot'}
    atomic_json(report,result)
    return result

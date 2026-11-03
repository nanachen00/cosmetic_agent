from __future__ import annotations
import hashlib
from pathlib import Path
HERE=Path(__file__).resolve().parent
EXCLUDE_DIRS={'.maintenance','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache'}
def digest(p:Path):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
rows=[]
for p in sorted(HERE.rglob('*')):
 if not p.is_file() or p.is_symlink() or p.name=='MANIFEST.sha256': continue
 rel=p.relative_to(HERE)
 if any(x in EXCLUDE_DIRS for x in rel.parts): continue
 rows.append(f'{digest(p)}  {rel.as_posix()}')
(HERE/'MANIFEST.sha256').write_text('\n'.join(rows)+'\n',encoding='utf-8')
print(f'sealed {len(rows)} files')

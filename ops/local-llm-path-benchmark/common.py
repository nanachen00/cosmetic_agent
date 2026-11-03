"""Standard-library helpers. Never imports BLINK application modules."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request

class Refuse(RuntimeError):
    pass

# Results are best-effort sanitized. Raw process state never belongs in results.
_SECRET = re.compile(r'(?i)(api.?key|password|passwd|secret|authorization|cookie|credential|access.?token|refresh.?token|private.?key|database_url|dsn)')
_KEY = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{10,}|z_[A-Za-z0-9_.-]{30,}|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b')
_URI = re.compile(r'([a-zA-Z][\w+.-]*://)([^\s/]+@)')

def redact(text: str) -> str:
    text = _KEY.sub('<REDACTED>', text)
    text = _URI.sub(r"\1<REDACTED>@", text)
    text = re.sub(r"(?i)(Bearer\s+)\S+", r"\1<REDACTED>", text)
    # Only credential names, not max_tokens/completion_tokens numeric metrics.
    names = r"(?:[A-Z0-9_]*(?:API_KEY|PASSWORD|PASSWD|SECRET|ACCESS_TOKEN|REFRESH_TOKEN|ID_TOKEN)|authorization|cookie|api_key|password|secret|access_token|refresh_token|id_token)"
    text = re.sub(r'(?im)^(\s*(?:export\s+)?' + names + r'\s*=\s*)[^\r\n]*', r'\1<REDACTED>', text)
    text = re.sub(r'(?i)("' + names + r'"\s*:\s*)"(?:[^"\\]|\\.)*"', r'\1"<REDACTED>"', text)
    return text

def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')

def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')

def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def atomic_json(path: Path, obj, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))

_LOCK = threading.Lock()
def event(log_path: Path | str, kind: str, **fields) -> None:
    row = {'timestamp': utc(), 'event': kind, **fields}
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _LOCK, p.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')

def percentile(xs, q):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs: return None
    i = (len(xs)-1) * q; a, b = math.floor(i), math.ceil(i)
    return xs[a] + (xs[b]-xs[a])*(i-a)

def stats(xs):
    xs = [float(x) for x in xs if x is not None]
    if not xs: return {'n':0, 'mean':None, 'p50':None, 'p95':None, 'max':None}
    return {'n':len(xs), 'mean':sum(xs)/len(xs), 'p50':percentile(xs,.5), 'p95':percentile(xs,.95), 'max':max(xs)}

# Local test clients must not inherit HTTP(S)_PROXY from the operator's shell.
def local_opener(*handlers):
    class RefuseRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl): return None
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), RefuseRedirect(), *handlers)

def local_url(url: str) -> None:
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme != 'http' or u.hostname not in {'127.0.0.1', 'localhost', '::1'} or u.username or u.password:
        raise Refuse('Test endpoints must be literal loopback HTTP URLs')

def http_json(url: str, payload=None, timeout=10, headers=None):
    local_url(url)
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    h = {'Content-Type':'application/json', **(headers or {})}
    r = urllib.request.Request(url, data=body, headers=h)
    try:
        with local_opener().open(r, timeout=timeout) as resp:
            raw = resp.read(8*1024*1024); code = resp.status; hh = dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read(8*1024*1024); code = exc.code; hh = dict(exc.headers)
    try: data = json.loads(raw)
    except Exception: data = {'body_preview':redact(raw.decode(errors='replace')[:500])}
    return code, data, hh

def probe(url: str, timeout=90, require_key=None, process=None):
    deadline = time.monotonic() + timeout; last = 'not polled'
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise Refuse('Child exited before readiness; inspect its private raw/sanitized log')
        try:
            status, data, _ = http_json(url, timeout=min(3, max(.1, deadline-time.monotonic())))
            if status == 200 and (require_key is None or require_key in data): return data
            last = 'HTTP ' + str(status)
        except Exception as exc: last = type(exc).__name__
        time.sleep(.5)
    raise Refuse('Readiness timed out at ' + url + ' (' + last + ')')

#!/usr/bin/env bash
# Reload a prebuilt frontend without interrupting backend workers or GPU jobs.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
[[ "$(uname -s)" == Darwin ]] || { echo '[ERROR] Mac frontend only' >&2; exit 2; }
set -a
source "$PROJECT_ROOT/cosmetic-agent/.env"
set +a
PY="${BACKEND_PYTHON:-$PROJECT_ROOT/cosmetic-agent/.venv/bin/python}"
MAC_RUNTIME_DIR="${MAC_RUNTIME_DIR:-${TMPDIR:-/tmp}/blink-cosmetic-agent}"
export NEXT_DIST_DIR="${NEXT_DIST_DIR:-.next}"
"$PY" - "$PROJECT_ROOT" "$MAC_RUNTIME_DIR" "$NEXT_DIST_DIR" <<'PY'
from pathlib import Path
import json, os, re, signal, subprocess, sys, time, urllib.request

project, runtime, dist_name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if not re.fullmatch(r'\.next(?:-[A-Za-z0-9][A-Za-z0-9_-]*)?', dist_name):
    raise SystemExit('[ERROR] Invalid build directory')
dist = project / 'frontend' / dist_name
if dist.is_symlink() or not (dist / 'BUILD_ID').is_file():
    raise SystemExit('[ERROR] A completed, non-symlink frontend build is required')
manifest = json.loads((dist / 'routes-manifest.json').read_text())
rewrites = manifest['rewrites']
if isinstance(rewrites, dict):
    rewrites = [item for group in rewrites.values() for item in group]
if not any(item['source'] == '/api/:path*' and item['destination'] == 'http://127.0.0.1:8010/api/:path*' for item in rewrites):
    raise SystemExit('[ERROR] Build must route API requests to the Mac backend')
pids = runtime / 'pids'
identity_path = pids / 'frontend.identity.json'
pid_path = pids / 'frontend.pid'
identity = json.loads(identity_path.read_text())
pid = int(pid_path.read_text())
if pid <= 1:
    raise SystemExit('[ERROR] Invalid managed PID')
def start_time(pid):
    return subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True, env={**os.environ, 'LC_ALL': 'C'}).strip()

def verify_frontend_process(pid):
    command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()
    cwd = subprocess.check_output(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], text=True).splitlines()
    if not command.startswith('next-server (v') or 'n' + str((project / 'frontend').resolve()) not in cwd:
        raise SystemExit('[ERROR] Managed PID is not the project frontend')
    listeners = subprocess.check_output(['lsof', '-t', '-nP', '-iTCP:3000', '-sTCP:LISTEN'], text=True).split()
    addresses = subprocess.check_output(['lsof', '-a', '-p', str(pid), '-nP', '-iTCP:3000', '-sTCP:LISTEN', '-Fn'], text=True).splitlines()
    if set(listeners) != {str(pid)} or {line for line in addresses if line.startswith('n')} != {'n127.0.0.1:3000'}:
        raise SystemExit('[ERROR] Port3000 must belong only to the project frontend on loopback')

actual_start = start_time(pid)
if identity != {'pid': pid, 'service': 'frontend', 'start': actual_start}:
    raise SystemExit('[ERROR] Frontend identity changed; no signal sent')
verify_frontend_process(pid)
os.kill(pid, signal.SIGTERM)
for _ in range(150):
    if subprocess.run(['ps', '-p', str(pid)], stdout=subprocess.DEVNULL).returncode != 0:
        break
    time.sleep(.1)
else:
    raise SystemExit('[ERROR] Frontend did not stop; no replacement or forced kill')
with (runtime / 'logs' / 'frontend.log').open('ab') as log:
    child = subprocess.Popen(['/bin/bash', str(project / 'ops/runtime/run-mac-service.sh'), 'frontend', str(runtime)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
start = start_time(child.pid)
temporary = identity_path.with_suffix('.json.partial')
temporary.write_text(json.dumps({'pid': child.pid, 'service': 'frontend', 'start': start}))
temporary.replace(identity_path)
for _ in range(60):
    if child.poll() is not None:
        raise SystemExit('[ERROR] Frontend exited; inspect its managed log')
    try:
        with urllib.request.urlopen('http://127.0.0.1:3000/skin-photo', timeout=2) as response:
            if response.status == 200:
                if start_time(child.pid) != start:
                    raise SystemExit('[ERROR] Replacement process identity changed')
                verify_frontend_process(child.pid)
                print('[READY] Frontend reloaded; backend and GPU processes untouched')
                break
    except (OSError, ValueError):
        pass
    time.sleep(.5)
else:
    raise SystemExit('[ERROR] Frontend readiness timed out')
PY

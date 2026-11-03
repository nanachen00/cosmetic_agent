#!/usr/bin/env python3
"""servers — start / stop / status for the two local servers Gate B needs on Colab. Stdlib only.

  kev   : `python -m kev.serve` from the jaredpalmer/kev checkout (its own uv venv), 127.0.0.1:8230 only (kevlib guard 8230-8239)
  llama : `llama-server` (llama.cpp, CUDA build) serving one Gemma GGUF, 127.0.0.1:8350 only (llmclient guard 8350-8359)

A pid file (pid + /proc start ticks + cmdline marker) is kept per server in --state; stop only signals a process whose
start ticks AND cmdline still match, so an unrelated listener is never killed. Logs: <state>/<name>.log
  python servers.py kev start --kev-dir /content/kev [--model jaredpalmer/kev-0.8b@v1.0] [--dtype auto|fp32|fp16|bf16]
  python servers.py llama start --bin /content/llama-bin/llama-server --gguf /content/models/x.gguf [--parallel 4 --ctx 16384]
  python servers.py {kev|llama} stop | status ;  python servers.py stop-all
"""
from __future__ import annotations
import argparse, json, os, signal, subprocess, sys, time, urllib.request

STATE = os.environ.get("GATEB_STATE", "/content/gateb_state")
KEV_PORT, LLAMA_PORT = 8230, 8350
FORBIDDEN = {8000, 8001, 8002, 8003, 8010}


def _ticks(pid):
    try: return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19]
    except Exception: return None


def _cmd(pid):
    try: return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception: return ""


def _pidfile(state, name): return os.path.join(state, f"{name}.pid.json")


def owned(state, name, marker):
    p = _pidfile(state, name)
    if not os.path.exists(p): return None
    s = json.load(open(p))
    if _ticks(s["pid"]) == s["ticks"] and marker in _cmd(s["pid"]): return s
    return None


def _get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r: return r.status, r.read()
    except Exception as e:
        return getattr(e, "code", 0) or 0, b""


def gpu_cc():
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=name,compute_cap,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        name, cc, mem = [x.strip() for x in o.split(",")]
        return name, float(cc), mem
    except Exception:
        return None, None, None


def _start(state, name, marker, argv, env, cwd, ready_url, timeout, ready_ok=lambda st, body: st == 200):
    os.makedirs(state, exist_ok=True)
    if owned(state, name, marker):
        print(f"[{name}] already running (ours): {json.load(open(_pidfile(state, name)))['pid']}"); return 0
    port = int(ready_url.split(":")[2].split("/")[0])
    st, _ = _get(ready_url, 1)
    if st:
        sys.exit(f"[{name}] [REFUSED] something not started by us already answers on :{port}; stop it first")
    log = open(os.path.join(state, f"{name}.log"), "ab")
    log.write(f"\n===== {time.strftime('%F %T')} start: {' '.join(argv)}\n".encode()); log.flush()
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(0.5)
    json.dump({"pid": p.pid, "ticks": _ticks(p.pid), "argv": argv, "started": time.time()}, open(_pidfile(state, name), "w"))
    t0 = time.time(); last = 0
    while time.time() - t0 < timeout:
        if p.poll() is not None:
            print(open(os.path.join(state, f"{name}.log"), errors="replace").read()[-3000:])
            sys.exit(f"[{name}] exited with code {p.returncode} before becoming ready (log above: {state}/{name}.log)")
        st, body = _get(ready_url, 3)
        if ready_ok(st, body):
            print(f"[{name}] ready on :{port} after {time.time() - t0:.0f}s (pid {p.pid}, log {state}/{name}.log)"); return 0
        if time.time() - last > 30:
            last = time.time(); print(f"[{name}] waiting for {ready_url} … {time.time() - t0:.0f}s (first start downloads weights)", flush=True)
        time.sleep(2)
    stop(state, name, marker)
    sys.exit(f"[{name}] not ready after {timeout}s — see {state}/{name}.log")


def stop(state, name, marker):
    s = owned(state, name, marker)
    if not s:
        print(f"[{name}] not running (or not ours)")
        try: os.remove(_pidfile(state, name))
        except OSError: pass
        return 0
    try: os.killpg(s["pid"], signal.SIGTERM)
    except Exception:
        try: os.kill(s["pid"], signal.SIGTERM)
        except Exception: pass
    for _ in range(40):
        if _ticks(s["pid"]) != s["ticks"]: break
        time.sleep(0.5)
    else:
        try: os.killpg(s["pid"], signal.SIGKILL)
        except Exception: pass
    try: os.remove(_pidfile(state, name))
    except OSError: pass
    print(f"[{name}] stopped (pid {s['pid']})"); return 0


def status(state, name, marker, url):
    s = owned(state, name, marker); st, _ = _get(url, 2)
    print(f"[{name}] {'OWNED_RUNNING pid ' + str(s['pid']) if s else ('FOREIGN_LISTENER' if st else 'STOPPED')} · {url} -> {st or 'no answer'}")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("name", choices=["kev", "llama", "stop-all"]); ap.add_argument("action", nargs="?", default="status", choices=["start", "stop", "status"])
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--kev-dir", default="/content/kev"); ap.add_argument("--model", default="jaredpalmer/kev-0.8b@v1.0")
    ap.add_argument("--dtype", default="auto", help="auto = fp32 on GPUs without bf16 (T4, cc<8.0), bf16 otherwise (Kev serving default)")
    ap.add_argument("--cuda-graphs", default="auto", choices=["auto", "0", "1"]); ap.add_argument("--kev-port", type=int, default=KEV_PORT)
    ap.add_argument("--bin", default="/content/llama-bin/llama-server"); ap.add_argument("--gguf"); ap.add_argument("--llama-port", type=int, default=LLAMA_PORT)
    ap.add_argument("--parallel", type=int, default=4); ap.add_argument("--ctx", type=int, default=16384); ap.add_argument("--ngl", type=int, default=99)
    ap.add_argument("--timeout", type=int, default=1800)
    A = ap.parse_args()
    if A.kev_port not in range(8230, 8240) or A.llama_port not in range(8350, 8360) or {A.kev_port, A.llama_port} & FORBIDDEN:
        sys.exit("[REFUSED] Kev must use 8230-8239 and llama-server 8350-8359")
    kev_url, llama_url = f"http://127.0.0.1:{A.kev_port}/v1/models", f"http://127.0.0.1:{A.llama_port}/health"
    if A.name == "stop-all":
        stop(A.state, "kev", "kev.serve"); stop(A.state, "llama", "llama-server"); return
    if A.name == "kev":
        if A.action == "status": return status(A.state, "kev", "kev.serve", kev_url)
        if A.action == "stop": return stop(A.state, "kev", "kev.serve")
        name, cc, _ = gpu_cc(); dtype = A.dtype
        if dtype == "auto": dtype = "bf16" if (cc or 0) >= 8.0 else "fp32"
        env = dict(os.environ, KEV_DTYPE=dtype, PYTHONUNBUFFERED="1")
        if A.cuda_graphs != "auto": env["KEV_CUDA_GRAPHS"] = A.cuda_graphs
        py = os.path.join(A.kev_dir, ".venv", "bin", "python")
        if not os.path.exists(py): sys.exit(f"[kev] {py} missing — run colab_setup.sh first")
        print(f"[kev] GPU={name} cc={cc} -> KEV_DTYPE={dtype} KEV_CUDA_GRAPHS={env.get('KEV_CUDA_GRAPHS', 'default')} model={A.model}")
        return _start(A.state, "kev", "kev.serve", [py, "-m", "kev.serve", "--run", A.model, "--host", "127.0.0.1", "--port", str(A.kev_port), "--device", "cuda"],
                      env, A.kev_dir, kev_url, A.timeout)
    if A.action == "status": return status(A.state, "llama", "llama-server", llama_url)
    if A.action == "stop": return stop(A.state, "llama", "llama-server")
    if not A.gguf or not os.path.exists(A.gguf): sys.exit(f"[llama] --gguf {A.gguf!r} not found")
    if not os.path.exists(A.bin): sys.exit(f"[llama] {A.bin} missing — run colab_setup.sh first")
    if not os.access(A.bin, os.X_OK):                       # a copy from the Drive mount can arrive without the exec bit
        os.chmod(A.bin, os.stat(A.bin).st_mode | 0o755); print(f"[llama] restored exec permission on {A.bin}")
    argv = [A.bin, "-m", A.gguf, "--host", "127.0.0.1", "--port", str(A.llama_port), "-ngl", str(A.ngl), "-c", str(A.ctx), "-np", str(A.parallel),
            "--jinja", "--reasoning", "off", "--reasoning-budget", "0", "--seed", "0", "-fa", "auto", "--no-webui", "--metrics"]
    return _start(A.state, "llama", "llama-server", argv, dict(os.environ), None, llama_url, A.timeout,
                  ready_ok=lambda st, body: st == 200 and b"ok" in body)


if __name__ == "__main__":
    sys.exit(main())

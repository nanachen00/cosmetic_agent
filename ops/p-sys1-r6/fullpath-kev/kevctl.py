#!/usr/bin/env python3
"""kevctl — lifecycle of the Kev server (System One, port 8230 ONLY).  Takes over what run_gate1.sh / gate8_kev.py did by hand.

  kevctl.py status | start | stop | restart | adopt | probe [--slots] | logs
Safety: fixed port 8230; a process is only signalled when its (pid, /proc start-ticks) matches our pid file AND its cmdline still
contains `kev.serve`; a foreign listener on 8230 is never killed (use `adopt` once you have checked it is the old Kev).
The Gate1/Gate2 harness refuses to start when 8230 is busy ("owned Kev port already in use") — run `fp.sh harness ...`, which stops our Kev first.
"""
import argparse, json, os, signal, socket, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("BLINK_BENCH_ROOT", "/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark"))
PORT = 8230
STATE = HERE / ".state" / "kev.json"
LOG = HERE / "logs" / "kev-service.log"


def ticks(pid): return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
def cmdline(pid):
    try: return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError: return ""
def port_busy():
    s = socket.socket(); s.settimeout(0.3)
    try: return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally: s.close()
def models():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/v1/models", timeout=2) as r: return json.loads(r.read().decode()) if r.status == 200 else None
    except Exception: return None
def listener_pid():
    """pid listening on 127.0.0.1:8230 via /proc/net/tcp inode -> /proc/*/fd scan (needs same user or root)."""
    inode = None
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try: lines = Path(f).read_text().splitlines()[1:]
        except OSError: continue
        for ln in lines:
            p = ln.split()
            if p[3] == "0A" and int(p[1].rsplit(":", 1)[1], 16) == PORT: inode = p[9]
    if not inode: return None
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                for fd in (d / "fd").iterdir():
                    if os.readlink(fd) == f"socket:[{inode}]": return int(d.name)
            except OSError: continue
    return None
def load():
    try: return json.loads(STATE.read_text())
    except Exception: return None
def owned():
    s = load()
    if not s: return None
    try:
        if ticks(s["pid"]) == s["ticks"] and "kev.serve" in cmdline(s["pid"]): return s
    except OSError: pass
    return None
def save(pid, how):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"pid": pid, "ticks": ticks(pid), "port": PORT, "how": how, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")


def cmd_status(a=None):
    o = owned(); busy = port_busy(); m = models()
    if o and busy: st = "OWNED_RUNNING"
    elif o and not busy: st = "OWNED_STARTING_OR_HUNG"
    elif busy: st = "FOREIGN_LISTENER"
    else: st = "STOPPED"
    lp = listener_pid() if busy else None
    print(json.dumps({"status": st, "port": PORT, "owned": o, "listener_pid": lp, "listener_cmd": cmdline(lp)[:200] if lp else None,
                      "models_ok": m is not None}, ensure_ascii=False, indent=1))
    return {"OWNED_RUNNING": 0, "STOPPED": 3}.get(st, 4)


def cmd_start(a):
    if port_busy(): sys.exit(f"[REFUSED] port {PORT} already in use ({'ours' if owned() else 'FOREIGN'}); `kevctl.py status`, then `adopt` or stop it")
    py = ROOT / ".runtime/kev-py312/bin/python"; adapter = ROOT / ".models/kev-0.8b-9a45d25eb2ab"
    if not py.is_file() or not adapter.is_dir(): sys.exit(f"[ERROR] Kev runtime/model missing: {py} / {adapter}")
    for d in (ROOT / ".cache/tmp", ROOT / ".cache/huggingface", ROOT / ".cache/xdg", LOG.parent): d.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "vendor/kev") + (":" + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
               HF_HOME=str(ROOT / ".cache/huggingface"), XDG_CACHE_HOME=str(ROOT / ".cache/xdg"), TMPDIR=str(ROOT / ".cache/tmp"),
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", KEV_DTYPE="bf16", KEV_FUSED="0", KEV_CUDA_GRAPHS="0", TOKENIZERS_PARALLELISM="false")
    log = open(LOG, "ab")
    p = subprocess.Popen([str(py), "-m", "kev.serve", "--run", str(adapter), "--host", "127.0.0.1", "--port", str(PORT)],
                         stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(ROOT), start_new_session=True)
    save(p.pid, "start")
    deadline = time.time() + 240
    while time.time() < deadline:
        if p.poll() is not None: sys.exit(f"[ERROR] Kev exited early (rc={p.returncode}); see {LOG}")
        m = models()
        if m is not None:
            (HERE / ".state" / "kev-models.json").write_text(json.dumps(m, indent=1)); print(f"READY pid={p.pid} port={PORT}"); return 0
        time.sleep(1)
    cmd_stop(a); sys.exit("[ERROR] readiness timeout (240 s); stopped it")


def cmd_stop(a=None):
    o = owned()
    if not o:
        if port_busy(): sys.exit(f"[REFUSED] port {PORT} is held by a process we do not own; run `adopt` after checking `status`")
        print("already stopped"); STATE.unlink(missing_ok=True); return 0
    pid = o["pid"]; os.killpg(pid, signal.SIGTERM) if os.getpgid(pid) == pid else os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        if not Path(f"/proc/{pid}").exists() or "kev.serve" not in cmdline(pid): break
        time.sleep(0.5)
    else:
        if ticks(pid) == o["ticks"]: os.kill(pid, signal.SIGKILL); time.sleep(1)       # same process still, escalate
    for _ in range(20):
        if not port_busy(): break
        time.sleep(0.5)
    STATE.unlink(missing_ok=True)
    print("STOPPED" if not port_busy() else "[WARN] port still busy"); return 0 if not port_busy() else 5


def cmd_adopt(a):
    if owned(): print("already owned"); return 0
    pid = listener_pid()
    if not pid: sys.exit("[ERROR] no listener found on 8230 (or /proc not readable by this user — run as the user that started Kev)")
    cl = cmdline(pid)
    if "kev.serve" not in cl or f"--port {PORT}" not in cl: sys.exit(f"[REFUSED] listener pid {pid} is not `kev.serve --port {PORT}`: {cl[:160]}")
    save(pid, "adopt"); print(f"ADOPTED pid={pid}: {cl[:160]}"); return 0


QUESTIONS = {
    "route": {"type": "choice", "instructions": "依使用者真正要完成的主要任務選一個路由；資訊不足時選 clarify。",
              "criteria": {"product": "商品推薦/搜尋", "knowledge": "成分與保養知識", "skin_tracking": "膚況紀錄/追蹤", "conversation": "一般聊天/結束語", "safety": "安全、刺激、禁忌或交互作用", "clarify": "資訊不足需釐清"}},
    "needs_clarification": {"type": "noul", "instructions": "是否必須先追問才能安全決定主要任務？", "criteria": {"true": "需要追問", "false": "可直接分派"}},
    "safety_sensitive": {"type": "noul", "instructions": "是否涉及過敏、刺激、禁忌、懷孕、交互作用或其他需要優先安全處理的內容？", "criteria": {"true": "安全敏感", "false": "沒有明顯安全風險"}},
}


def cmd_probe(a):
    sys.path.insert(0, str(HERE / "payload")); import importlib.util
    q = dict(QUESTIONS)
    if a.slots:
        spec = importlib.util.spec_from_file_location("kd", HERE / "payload/kev_dispatch.py")
        # reuse the slot questions without importing the package-bound module
        src = (HERE / "payload/kev_dispatch.py").read_text()
        ns = {}; start = src.index("_SLOT_Q = {"); end = src.index("_CAT_RX"); exec(src[start:end], ns); q.update(ns["_SLOT_Q"])
    text = a.text; body = json.dumps({"state": text, "model": "kev-latest", "questions": q}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/systemone", data=body, headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=20) as r: out = json.loads(r.read().decode())
    except Exception as e: sys.exit(f"[ERROR] probe failed: {type(e).__name__}: {e}")
    wall = (time.perf_counter() - t) * 1000
    print(json.dumps({"wall_ms": round(wall, 1), "server_latency_ms": out.get("latency_ms"), "answers": out.get("answers")}, ensure_ascii=False, indent=1))
    ok = {"route", "needs_clarification", "safety_sensitive"} <= set(out.get("answers", {}))
    if a.slots:
        miss = [k for k in ("skin_type", "product_category", "concern", "texture") if k not in out.get("answers", {})]
        print("SLOT_QUESTIONS:", "ALL ANSWERED" if not miss else f"NOT ANSWERED: {miss}  -> kev_slots arm unusable until Kev supports these")
        ok = ok and not miss
    return 0 if ok else 6


def cmd_logs(a): os.execvp("tail", ["tail", "-n", "80", str(LOG)])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="c", required=True)
    for n in ("status", "start", "stop", "restart", "adopt", "logs"): sp.add_parser(n)
    p = sp.add_parser("probe"); p.add_argument("--slots", action="store_true"); p.add_argument("--text", default="請推薦適合混合肌、預算1000元以下的清爽保濕乳液。")
    a = ap.parse_args()
    if a.c == "restart": cmd_stop(); return cmd_start(a)
    return {"status": cmd_status, "start": cmd_start, "stop": cmd_stop, "adopt": cmd_adopt, "probe": cmd_probe, "logs": cmd_logs}[a.c](a) or 0


if __name__ == "__main__":
    sys.exit(main())

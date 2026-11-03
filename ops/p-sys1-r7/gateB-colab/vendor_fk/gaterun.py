#!/usr/bin/env python3
"""gaterun — checkpoint/resume + partial-summary helper shared by gate0/gateA. Writes only under the run dir (default .runs/<gate>-<ts>)."""
from __future__ import annotations
import json, os, signal, sys, time, statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs: return None
    k = (len(xs) - 1) * p / 100; f = int(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def lat(xs):
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99), "max": max(xs) if xs else None, "mean": st.mean(xs) if xs else None}


def gpu_snapshot():
    """{util_pct, vram_used_gib, vram_total_gib} from rocm-smi (preferred) or nvidia-smi; None if neither works. Read-only, 5 s cap."""
    import shutil, subprocess
    try:
        if shutil.which("rocm-smi"):
            o = subprocess.run(["rocm-smi", "--showuse", "--showmeminfo", "vram", "--json"], capture_output=True, text=True, timeout=5).stdout
            j = json.loads(o[o.index("{"):]); c = j.get("card0") or next(iter(j.values()))
            g = lambda sub: next((float(v) for k, v in c.items() if sub in k and str(v).replace('.', '', 1).isdigit()), None)
            used, tot = g("VRAM Total Used Memory"), g("VRAM Total Memory")
            return {"util_pct": g("GPU use"), "vram_used_gib": used / 2**30 if used else None, "vram_total_gib": tot / 2**30 if tot else None}
        if shutil.which("nvidia-smi"):
            o = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout.splitlines()[0]
            u, m, t = [float(x) for x in o.split(",")]
            return {"util_pct": u, "vram_used_gib": m / 1024, "vram_total_gib": t / 1024}
    except Exception:
        return None
    return None


class GpuWatch:
    """Samples the GPU every `every` s while a step runs: keeps start / end / max util so a latency spike can be matched to GPU load."""
    def __init__(self, every=3.0):
        self.every, self.max_util, self.max_vram, self.start, self.end, self._stop = every, None, None, None, None, None

    def __enter__(self):
        import threading
        self.start = gpu_snapshot(); self._note(self.start); self._stop = threading.Event()
        def loop():
            while not self._stop.wait(self.every): self._note(gpu_snapshot())
        self._t = threading.Thread(target=loop, daemon=True); self._t.start(); return self

    def _note(self, s):
        if not s: return
        if s.get("util_pct") is not None: self.max_util = max(self.max_util or 0, s["util_pct"])
        if s.get("vram_used_gib") is not None: self.max_vram = max(self.max_vram or 0, s["vram_used_gib"])

    def __exit__(self, *a):
        self._stop.set(); self.end = gpu_snapshot(); self._note(self.end)

    def result(self):
        if not (self.start or self.end): return None
        return {"start": self.start, "end": self.end, "max_util_pct": self.max_util, "max_vram_gib": self.max_vram}


class Bar:
    """Progress bar: in-place on a TTY, one line per 10% otherwise (so logs/nohup stay readable)."""
    def __init__(self, total, label="", width=28):
        self.total, self.label, self.width, self.t0, self.n, self.last = max(1, total), label, width, time.time(), 0, -1
        self.tty = sys.stderr.isatty()

    def update(self, n=None, extra=""):
        self.n = self.n + 1 if n is None else n
        frac = min(1.0, self.n / self.total); el = time.time() - self.t0
        eta = f"{el / frac * (1 - frac):.0f}s" if frac > 0.02 else "…"
        line = f"{self.label} [{'█' * int(frac * self.width)}{'░' * (self.width - int(frac * self.width))}] {self.n}/{self.total} {frac * 100:3.0f}%  {el:.0f}s eta {eta} {extra}"
        if self.tty:
            sys.stderr.write("\r" + line[:200].ljust(120)); sys.stderr.flush()
        elif int(frac * 10) != self.last:
            self.last = int(frac * 10); print(line, file=sys.stderr, flush=True)

    def done(self):
        if self.tty: sys.stderr.write("\n"); sys.stderr.flush()


class Run:
    def __init__(self, gate, out=None, resume=False, nsteps=0):
        self.gate = gate; self.nsteps = nsteps; self.k = 0
        self.dir = out or os.path.join(HERE, ".runs", f"{gate}-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(self.dir, exist_ok=True)
        self.p_done = os.path.join(self.dir, "done.json")
        self.done = json.load(open(self.p_done)) if (resume and os.path.exists(self.p_done)) else {}
        self.interrupted = False
        self.render = lambda run: ""
        signal.signal(signal.SIGTERM, self._sig); signal.signal(signal.SIGINT, self._sig)
        self.status("RUNNING")

    def _sig(self, *a):
        self.interrupted = True
        raise KeyboardInterrupt

    def status(self, s):
        open(os.path.join(self.dir, "STATUS.txt"), "w").write(s + "\n")

    def save(self):
        t = self.p_done + ".tmp"; json.dump(self.done, open(t, "w"), ensure_ascii=False, indent=1); os.replace(t, self.p_done)

    def step(self, name, fn):
        self.k += 1
        tag = f"[{self.gate} step {self.k}/{self.nsteps}]" if self.nsteps else f"[{self.gate}]"
        if self.nsteps: Bar(self.nsteps, f"{self.gate} overall").update(self.k - 1)
        if name in self.done:
            print(f"{tag} reuse {name}", flush=True); return self.done[name]
        print(f"{tag} run   {name}", flush=True)
        t0 = time.time()
        with GpuWatch() as gw: r = fn()
        r = json.loads(json.dumps(r, default=str)); r["_secs"] = round(time.time() - t0, 1); r["_gpu"] = gw.result()
        self.done[name] = r; self.save(); self.write_summary(); return r

    def write_summary(self):
        open(os.path.join(self.dir, "SUMMARY.md"), "w").write(self.render(self) + self.gpu_table())

    def gpu_table(self):
        rows = [(k, v.get("_gpu"), v.get("_secs")) for k, v in self.done.items() if isinstance(v, dict)]
        if not any(r[1] for r in rows): return "\n## GPU context\n\n_no rocm-smi / nvidia-smi reading available — latency cannot be matched to GPU load._\n"
        f = lambda s, k, d=0: "–" if not s or s.get(k) is None else f"{s[k]:.{d}f}"
        L = ["\n## GPU context per step (the GPU is shared with whatever else runs on it)\n", "|step|secs|util% start|util% end|util% max during|VRAM GiB start|VRAM GiB end|VRAM GiB max|", "|--|--|--|--|--|--|--|--|"]
        for k, g_, secs in rows:
            if not g_: L.append(f"|{k}|{secs}|–|–|–|–|–|–|"); continue
            L.append(f"|{k}|{secs}|{f(g_['start'],'util_pct')}|{f(g_['end'],'util_pct')}|{'–' if g_['max_util_pct'] is None else format(g_['max_util_pct'],'.0f')}|{f(g_['start'],'vram_used_gib',1)}|{f(g_['end'],'vram_used_gib',1)}|{'–' if g_['max_vram_gib'] is None else format(g_['max_vram_gib'],'.1f')}|")
        L.append("\nHigh util% or a VRAM jump during a step means another workload (e.g. :8000/:8010) was active: treat that step's latency as measured under contention.\n")
        return "\n".join(L)

    def finish(self, interrupted=False):
        self.write_summary()
        self.status("INTERRUPTED (partial results valid: see SUMMARY.md; --resume continues)" if interrupted else "COMPLETE")
        print(f"[{self.gate}] {'INTERRUPTED' if interrupted else 'COMPLETE'} -> {self.dir}/SUMMARY.md", flush=True)


def main_guard(run, body):
    try:
        body(); run.finish(False); return 0
    except KeyboardInterrupt:
        run.finish(True); return 130

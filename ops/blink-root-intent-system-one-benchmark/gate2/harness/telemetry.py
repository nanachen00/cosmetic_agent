"""Low-frequency host/whole-GPU telemetry. No per-request GPU attribution claims."""
from __future__ import annotations
import csv
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from common import event,stats,utc

_NUM=r'(\d+(?:\.\d+)?)'
GPU_ROW=re.compile(r'^\s*(\d+)\s+(?:\d+\s+)?'+_NUM+r'\s*W\s+'+_NUM+r'\s*°?C\s+'+_NUM+r'\s*°?C\s+'+_NUM+r'\s*MHz\s+'+_NUM+r'\s*%\s+'+_NUM+r'\s*%\s+(?:N/A|\d+(?:\.\d+)?\s*%)\s+(?:N/A|\d+(?:\.\d+)?\s*%)\s+'+_NUM+r'\s*/\s*'+_NUM+r'\s*(GB|MB)\s*$')

def parse_gpu(text):
    for line in text.splitlines():
        m=GPU_ROW.match(line)
        if m and int(m[1])==0:
            x=[float(m[i]) for i in range(2,10)]
            if m[10]=='MB': x[-2:]=[n/1024 for n in x[-2:]]
            return dict(zip(['gpu_power_w','gpu_temp_c','gpu_memory_temp_c','gpu_clock_mhz','gpu_util_pct','gpu_memory_util_pct','vram_used_gib_approx','vram_total_gib_approx'],x))
    return {}

def cpu_times():
    fields=Path('/proc/stat').read_text().splitlines()[0].split()[1:9]
    v=list(map(int,fields)); return sum(v),v[3]+v[4]

def ram():
    v={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split())>=2}
    return (v['MemTotal']-v.get('MemAvailable',v.get('MemFree',0)))/1048576

class Sampler:
    def __init__(self, directory: Path, interval: float=2):
        self.directory=directory; self.interval=interval; self.stop_event=threading.Event()
        self.thread=None; self.rows=[]; self.prev=None; self.amd=shutil.which('amd-smi')
    def sample(self,phase):
        now=cpu_times(); cpu=None
        if self.prev and now[0]>self.prev[0]: cpu=100*(1-(now[1]-self.prev[1])/(now[0]-self.prev[0]))
        self.prev=now
        row={'timestamp':utc(),'phase':phase,'monotonic_s':time.monotonic(),'cpu_host_pct':cpu,'ram_host_used_gib':ram()}
        if self.amd:
            start=time.monotonic()
            try:
                r=subprocess.run([self.amd,'monitor','-g','0'],capture_output=True,text=True,timeout=5,
                                 env={**os.environ,'LC_ALL':'C.UTF-8'})
                event(self.directory/'gpu-raw.jsonl','gpu_sample',stdout=r.stdout[:20000],returncode=r.returncode)
                row.update(parse_gpu(r.stdout))
                if not row.get('vram_total_gib_approx'): row['gpu_error']='output_not_parsed'
            except (subprocess.TimeoutExpired,OSError): row['gpu_error']='command_failed_or_timed_out'
            row['gpu_query_ms']=(time.monotonic()-start)*1000
        else: row['gpu_error']='amd-smi_not_available'
        self.rows.append(row)
    def start(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        self.sample('before')
        self.thread=threading.Thread(target=self.loop,daemon=True); self.thread.start()
    def loop(self):
        while not self.stop_event.wait(self.interval):
            try: self.sample('active')
            except Exception as exc: event(self.directory/'errors.jsonl','telemetry_error',type=type(exc).__name__)
    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=8)
        if self.thread and self.thread.is_alive(): raise RuntimeError('Telemetry sampler did not stop')
        self.sample('after')
        fields=sorted({k for row in self.rows for k in row})
        with (self.directory/'telemetry.csv').open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(self.rows)
        for row in self.rows: event(self.directory/'telemetry.jsonl','telemetry',**row)
    def summary(self):
        active=[x for x in self.rows if x['phase']=='active']
        keys=['cpu_host_pct','gpu_util_pct','gpu_memory_util_pct','gpu_power_w','vram_used_gib_approx','ram_host_used_gib']
        return {'scope':'host CPU and whole GPU0; includes other processes',
                'active_samples':len(active),'gpu_valid_active_samples':sum('gpu_util_pct' in x for x in active),
                'status':'complete' if active and all('gpu_util_pct' in x for x in active) else 'partial_or_too_short',
                'stats':{k:stats(x.get(k) for x in active) for k in keys}}

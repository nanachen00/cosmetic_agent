"""Read-only process epochs; production drift is evidence, not cleanup failure.
Periodic samples cannot prove absence of sub-interval outages or DB schema changes.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from common import atomic_json, event, sha_bytes, utc, http_json, Refuse
import process_control as pc


def observe_ports(ports: list[int]) -> dict:
    ss = shutil.which('ss')
    if not ss: return {str(p):{'state':'unknown','reason':'ss-unavailable'} for p in ports}
    filt='( '+ ' or '.join('sport = :'+str(p) for p in ports)+' )'
    try:
        r=subprocess.run([ss,'-H','-ltnp',filt],capture_output=True,text=True,timeout=2,
                         check=False,env={**os.environ,'LC_ALL':'C'})
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {str(p):{'state':'unknown','reason':type(exc).__name__} for p in ports}
    if r.returncode:
        return {str(p):{'state':'unknown','reason':'ss-nonzero'} for p in ports}
    found={p:set() for p in ports}; occupied=set()
    for line in r.stdout.splitlines():
        cols=line.split()
        if len(cols)<4: continue
        m=re.search(r':(\d+)$',cols[3])
        if not m or int(m.group(1)) not in found: continue
        port=int(m.group(1)); occupied.add(port)
        found[port].update(map(int,re.findall(r'pid=(\d+)',line)))
    result={}
    for p in ports:
        if p not in occupied:
            result[str(p)]={'state':'not-listening'}; continue
        if len(found[p])!=1:
            result[str(p)]={'state':'unknown','reason':'no-single-readable-owner'}; continue
        try:
            rec=pc.inspect_pid(next(iter(found[p])))
            # Do not publish env, argv, or credential hashes. Epoch hash is process
            # identity/config metadata and not a measure of model weights.
            identity={k:rec[k] for k in ('pid','start_ticks','boot_id','exe','cwd','argv')}
            import json
            digest=sha_bytes(json.dumps(identity,sort_keys=True).encode())
            meta={'state':'listening','pid':rec['pid'],'start_ticks':rec['start_ticks'], 'epoch':digest}
            if p in (8200,8220):
                meta.update(max_num_seqs=pc.flag_value(rec['argv'],'--max-num-seqs'),
                            gpu_memory_utilization=pc.flag_value(rec['argv'],'--gpu-memory-utilization'))
            result[str(p)]=meta
        except (OSError,ValueError,Refuse):
            result[str(p)]={'state':'unknown','reason':'process-changed-or-unreadable'}
    return result


class Observer:
    def __init__(self, log: Path, ports, interval=2.0, observe=observe_ports):
        self.log=log; self.ports=sorted(set(ports)); self.interval=float(interval)
        self.observe=observe; self.stop_event=threading.Event(); self.lock=threading.Lock()
        self.rows=[]; self.thread=None
    def sample(self):
        with self.lock:
            started=time.monotonic()
            state=self.observe(self.ports)
            row={'timestamp':utc(),'monotonic':started,'completed_monotonic':time.monotonic(),'ports':state}
            self.rows.append(row); event(self.log,'deployment_sample',**row)
            return row
    def start(self):
        self.sample()
        def loop():
            while not self.stop_event.wait(self.interval):
                try: self.sample()
                except Exception as exc:
                    event(self.log,'deployment_observer_error',kind=type(exc).__name__)
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=5)
        self.sample()
    def begin(self, relevant):
        row=self.sample()
        return {'start':row['monotonic'],'relevant_ports':sorted(set(relevant)), 'start_sample':row}
    def end(self, token):
        self.sample()
        with self.lock:
            rows=[r for r in self.rows if r['monotonic']>=token['start']]
        return assess(rows,token['relevant_ports'],self.interval)
    def summary(self):
        with self.lock: rows=list(self.rows)
        return assess(rows,self.ports,self.interval)


def assess(rows, relevant, interval=2.0):
    unknown=[]; absent=[]; changed=[]; gaps=[]
    for port in relevant:
        states=[r.get('ports',{}).get(str(port),{'state':'unknown'}) for r in rows]
        if not states or any(s['state']=='unknown' for s in states): unknown.append(port)
        if any(s['state']=='not-listening' for s in states): absent.append(port)
        epochs={s.get('epoch') for s in states if s['state']=='listening'}
        if len(epochs)>1: changed.append(port)
    for a,b in zip(rows,rows[1:]):
        if b['monotonic']-a['completed_monotonic']>max(interval*3,interval+5):gaps.append(True)
    status='DEPENDENCY_DRIFT' if changed or absent else 'OBSERVATION_PARTIAL' if unknown or gaps or not rows else 'NO_OBSERVED_DEPLOYMENT_DRIFT'
    return {'status':status,'relevant_ports':list(relevant),'changed_ports':changed,'unavailable_ports':absent,
            'unknown_ports':unknown,'sample_count':len(rows),'large_sampling_gap':bool(gaps),
            'performance_eligible':status=='NO_OBSERVED_DEPLOYMENT_DRIFT',
            'scope':'sampled process epochs only; not proof of stable DB contents, model weights, or exclusive GPU'}


def wait_endpoint(port, path='/v1/models', timeout=60):
    """Read-only readiness polling before a batch, not retries of chat requests."""
    deadline=time.monotonic()+timeout; attempts=0; last=None
    while True:
        attempts+=1
        try:
            code,data,_=http_json(f'http://127.0.0.1:{port}'+path,timeout=min(3,max(.1,deadline-time.monotonic())))
            if code==200 and (path!='/v1/models' or 'data' in data):
                return {'ready':True,'attempts':attempts,'port':port}
            last='http-'+str(code)
        except Exception as exc:last=type(exc).__name__
        if time.monotonic()>=deadline:return {'ready':False,'attempts':attempts,'port':port,'last_error':last}
        time.sleep(min(1,max(0,deadline-time.monotonic())))

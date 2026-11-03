#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import re
import resource
import shutil
import statistics
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import urllib.request

DEFAULT_VUS = [1,2,4,8,16,32,64,128,256,512,1024,2048]
HARD_MAX_VU = 2048

def iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")

def percentile(xs,p):
    vals=sorted(float(x) for x in xs if x is not None)
    if not vals:
        return None
    if len(vals)==1:
        return vals[0]
    k=(len(vals)-1)*p
    lo,hi=math.floor(k),math.ceil(k)
    return vals[lo] if lo==hi else vals[lo]+(vals[hi]-vals[lo])*(k-lo)

def stat_block(xs):
    vals=[float(x) for x in xs if x is not None]
    if not vals:
        return {"mean":None,"p50":None,"p95":None,"p99":None,"max":None,"min":None}
    return {
      "mean":round(statistics.mean(vals),4),
      "p50":round(percentile(vals,.50),4),
      "p95":round(percentile(vals,.95),4),
      "p99":round(percentile(vals,.99),4),
      "max":round(max(vals),4),
      "min":round(min(vals),4),
    }

def ratio(a,b):
    if a is None or b in (None,0):
        return None
    return round(a/b,4)

def parse_vus(text):
    vals=[]
    for raw in text.split(","):
        raw=raw.strip()
        if not raw:
            continue
        v=int(raw)
        if v<1 or v>HARD_MAX_VU:
            raise ValueError(f"VU must be within 1..{HARD_MAX_VU}, got {v}")
        if v not in vals:
            vals.append(v)
    if not vals:
        raise ValueError("empty VU list")
    return vals

def sync_get_json(url,timeout=10):
    try:
        with urllib.request.urlopen(url,timeout=timeout) as r:
            return r.status,json.loads(r.read().decode())
    except Exception as e:
        return None,{"error":f"{type(e).__name__}: {e}"}

def listener_inventory(port):
    cp=subprocess.run(["ss","-ltnp"],capture_output=True,text=True)
    lines=[x for x in cp.stdout.splitlines() if re.search(rf":{port}\b",x)]
    info={"port":port,"listener_lines":lines}
    m=re.search(r"pid=(\d+)", "\n".join(lines))
    if m:
        pid=int(m.group(1)); info["pid"]=pid
        try:
            cmd=Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0",b" ").decode()
            info["cmdline"]=cmd
            for flag,cast in (
                ("max-num-seqs",int),
                ("gpu-memory-utilization",float),
                ("max-model-len",int),
            ):
                mm=re.search(rf"--{re.escape(flag)}(?:=|\s+)([^\s]+)",cmd)
                if mm:
                    try: info[flag.replace("-","_")]=cast(mm.group(1))
                    except Exception: pass
            info["enforce_eager"]="--enforce-eager" in cmd
        except Exception:
            pass
    return info

def read_cpu_times():
    nums=[int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    return sum(nums), nums[3]+(nums[4] if len(nums)>4 else 0)

def cpu_util(prev,cur):
    dt=cur[0]-prev[0]; di=cur[1]-prev[1]
    return None if dt<=0 else max(0.0,min(100.0,100.0*(dt-di)/dt))

def read_meminfo():
    vals={}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k,rest=line.split(":",1); vals[k]=int(rest.strip().split()[0])
    total=vals.get("MemTotal",0); avail=vals.get("MemAvailable",0)
    used=max(0,total-avail)
    return {
      "ram_used_gb":used/1024/1024,
      "ram_total_gb":total/1024/1024,
      "ram_available_gb":avail/1024/1024,
      "ram_used_pct":used/total*100 if total else None,
    }

_GPU_RE=re.compile(
    r"^\s*(?P<gpu>\d+)\s+"
    r"(?P<power>\d+(?:\.\d+)?)\s*W\s+"
    r"(?P<gpu_t>\d+(?:\.\d+)?)\s*°?\s*C\s+"
    r"(?P<mem_t>\d+(?:\.\d+)?)\s*°?\s*C\s+"
    r"(?P<gfx_clk>\d+(?:\.\d+)?)\s*MHz\s+"
    r"(?P<gfx>\d+(?:\.\d+)?)\s*%\s+"
    r"(?P<mem>\d+(?:\.\d+)?)\s*%\s+"
    r"(?:N/A|\d+(?:\.\d+)?\s*%)\s+"
    r"(?:N/A|\d+(?:\.\d+)?\s*%)\s+"
    r"(?P<used>\d+(?:\.\d+)?)\s*/\s*(?P<total>\d+(?:\.\d+)?)\s*GB\s*$"
)

def parse_gpu(text,gpu):
    for line in text.splitlines():
        m=_GPU_RE.match(line)
        if m and int(m.group("gpu"))==gpu:
            d=m.groupdict()
            return {
              "gpu_power_w":float(d["power"]),
              "gpu_temp_c":float(d["gpu_t"]),
              "gpu_mem_temp_c":float(d["mem_t"]),
              "gpu_gfx_clock_mhz":float(d["gfx_clk"]),
              "gpu_gfx_util_pct":float(d["gfx"]),
              "gpu_mem_util_pct":float(d["mem"]),
              "vram_used_gb":float(d["used"]),
              "vram_total_gb":float(d["total"]),
            }
    return None

class Telemetry:
    def __init__(self,outdir,gpu=0,interval=1.0):
        self.outdir=Path(outdir); self.gpu=gpu; self.interval=interval
        self.amd=shutil.which("amd-smi")
        self.samples=[]; self.errors=[]
        self.stop_evt=threading.Event(); self.thread=None; self.prev_cpu=None

    def one(self):
        cur=read_cpu_times()
        sample={"timestamp":iso_now(),"cpu_util_pct":cpu_util(self.prev_cpu,cur) if self.prev_cpu else None,**read_meminfo()}
        self.prev_cpu=cur
        sample.update({
          "gpu_power_w":None,"gpu_temp_c":None,"gpu_mem_temp_c":None,
          "gpu_gfx_clock_mhz":None,"gpu_gfx_util_pct":None,"gpu_mem_util_pct":None,
          "vram_used_gb":None,"vram_total_gb":None,
        })
        if self.amd:
            try:
                cp=subprocess.run([self.amd,"monitor","-g",str(self.gpu)],capture_output=True,text=True,timeout=15)
                if cp.returncode:
                    raise RuntimeError(cp.stderr.strip()[:500])
                g=parse_gpu(cp.stdout,self.gpu)
                if g is None:
                    raise RuntimeError("unable to parse amd-smi monitor output")
                sample.update(g)
            except Exception as e:
                self.errors.append(f"{iso_now()}: {type(e).__name__}: {e}")
        self.samples.append(sample)

    def loop(self):
        next_at=time.monotonic()
        while not self.stop_evt.is_set():
            self.one()
            next_at += self.interval
            self.stop_evt.wait(max(0,next_at-time.monotonic()))

    def start(self):
        self.outdir.mkdir(parents=True,exist_ok=True)
        self.prev_cpu=read_cpu_times()
        self.thread=threading.Thread(target=self.loop,daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=max(5,self.interval+2))
        try: self.one()
        except Exception: pass
        self.write()

    def write(self):
        (self.outdir/"telemetry.jsonl").write_text(
            "".join(json.dumps(x,ensure_ascii=False)+"\n" for x in self.samples),encoding="utf-8")
        fields=sorted({k for x in self.samples for k in x})
        with (self.outdir/"telemetry.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
            for x in self.samples: w.writerow(x)

    def summary(self):
        def vals(k): return [x.get(k) for x in self.samples if x.get(k) is not None]
        vr=vals("vram_used_gb")
        return {
          "sample_count":len(self.samples),"errors":self.errors,
          "cpu_util_pct":stat_block(vals("cpu_util_pct")),
          "ram_used_gb":stat_block(vals("ram_used_gb")),
          "ram_available_gb":stat_block(vals("ram_available_gb")),
          "gpu_gfx_util_pct":stat_block(vals("gpu_gfx_util_pct")),
          "gpu_mem_util_pct":stat_block(vals("gpu_mem_util_pct")),
          "gpu_power_w":stat_block(vals("gpu_power_w")),
          "gpu_gfx_clock_mhz":stat_block(vals("gpu_gfx_clock_mhz")),
          "gpu_temp_c":stat_block(vals("gpu_temp_c")),
          "gpu_mem_temp_c":stat_block(vals("gpu_mem_temp_c")),
          "vram_used_gb":{
             **stat_block(vr),
             "start":round(vr[0],4) if vr else None,
             "end":round(vr[-1],4) if vr else None,
             "delta":round(vr[-1]-vr[0],4) if len(vr)>=2 else None,
          },
          "vram_total_gb":vals("vram_total_gb")[-1] if vals("vram_total_gb") else None,
        }

class AsyncHTTP11:
    def __init__(self,base_url,timeout):
        u=urlparse(base_url)
        if u.scheme!="http":
            raise ValueError("This high-concurrency harness intentionally supports local HTTP endpoints only")
        self.host=u.hostname or "127.0.0.1"
        self.port=u.port or 80
        prefix=u.path.rstrip("/")
        self.path=(prefix if prefix else "")+"/v1/chat/completions"
        self.timeout=timeout
        self.reader=None; self.writer=None

    async def connect(self):
        self.reader,self.writer=await asyncio.wait_for(
            asyncio.open_connection(self.host,self.port),timeout=min(self.timeout,15)
        )

    async def close(self):
        if self.writer:
            self.writer.close()
            try: await self.writer.wait_closed()
            except Exception: pass
        self.reader=self.writer=None

    async def _read_response(self):
        line=await self.reader.readline()
        if not line:
            raise ConnectionError("connection closed before status line")
        parts=line.decode(errors="replace").strip().split(None,2)
        if len(parts)<2:
            raise ConnectionError(f"bad status line: {line!r}")
        http_version=parts[0].upper()
        status=int(parts[1])
        headers={}
        while True:
            h=await self.reader.readline()
            if h in (b"\r\n",b"\n",b""):
                break
            s=h.decode(errors="replace").rstrip("\r\n")
            if ":" in s:
                k,v=s.split(":",1); headers[k.strip().lower()]=v.strip()

        if headers.get("transfer-encoding","").lower()=="chunked":
            body=bytearray()
            while True:
                szline=(await self.reader.readline()).decode().strip().split(";",1)[0]
                size=int(szline,16)
                if size==0:
                    while True:
                        trailer=await self.reader.readline()
                        if trailer in (b"\r\n",b"\n",b""):
                            break
                    break
                body.extend(await self.reader.readexactly(size))
                await self.reader.readexactly(2)
            raw=bytes(body)
        elif "content-length" in headers:
            raw=await self.reader.readexactly(int(headers["content-length"]))
        else:
            raw=await self.reader.read()
            await self.close()

        connection_header=headers.get("connection","").lower()

        # Python BaseHTTPRequestHandler/ThreadingHTTPServer commonly responds with
        # HTTP/1.0 and closes the socket after every response unless it explicitly
        # advertises Connection: keep-alive.  :8000 uses that style in the current
        # Blue implementation.  Do not reuse such a socket for the next chat turn.
        #
        # vLLM :8200 normally speaks HTTP/1.1 and can keep the connection alive.
        if (
            connection_header=="close"
            or (http_version=="HTTP/1.0" and connection_header!="keep-alive")
        ):
            await self.close()

        return status,headers,raw

    async def post(self,payload):
        async def _do():
            if (
                self.writer is None
                or self.writer.is_closing()
                or self.reader is None
                or self.reader.at_eof()
            ):
                await self.close()
                await self.connect()
            raw=json.dumps(payload,ensure_ascii=False).encode()
            request=(
                f"POST {self.path} HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\n"
                "Connection: keep-alive\r\n\r\n"
            ).encode()+raw
            t0=time.perf_counter()
            self.writer.write(request)
            await self.writer.drain()
            status,headers,body=await self._read_response()
            return status,round(time.perf_counter()-t0,4),body

        try:
            return await asyncio.wait_for(_do(),timeout=self.timeout)
        except asyncio.TimeoutError:
            await self.close()
            return None,round(self.timeout,4),b"CLIENT_TIMEOUT"
        except Exception as e:
            await self.close()
            return None,None,f"{type(e).__name__}: {e}".encode()

def validate(rule,content,nonce):
    if not rule: return True
    typ=rule.get("type")
    if typ=="contains": return rule["value"] in content
    if typ=="nonempty": return bool(content.strip())
    if typ=="exact_nonce": return content.strip()==nonce
    return False

async def one_conversation(base_url,timeout,scenario,model,vu_id,wave,event,max_tokens):
    nonce=f"VU{vu_id:04d}-W{wave:03d}-{int(time.time())}"
    messages=[{"role":"system","content":scenario["system"]}]
    rows=[]
    client=AsyncHTTP11(base_url,timeout)
    await event.wait()
    try:
        for turn_idx,spec in enumerate(scenario["turns"],1):
            user=spec["user"].replace("{{NONCE}}",nonce)
            messages.append({"role":"user","content":user})
            payload={"model":model,"messages":messages,"temperature":0.0,"top_p":1.0,"max_tokens":max_tokens}
            status,lat,body=await client.post(payload)
            row={
              "vu_id":vu_id,"wave":wave,"turn":turn_idx,
              "status":status,"latency_s":lat,"validation_ok":False,
              "prompt_tokens":None,"completion_tokens":None,"total_tokens":None,
              "content":None,"error":None,
            }
            if status==200:
                try:
                    data=json.loads(body.decode(errors="replace"))
                    content=data["choices"][0]["message"]["content"]
                    usage=data.get("usage") or {}
                    row.update(
                      content=content,
                      prompt_tokens=usage.get("prompt_tokens"),
                      completion_tokens=usage.get("completion_tokens"),
                      total_tokens=usage.get("total_tokens"),
                      validation_ok=validate(spec.get("validation"),content,nonce)
                    )
                    messages.append({"role":"assistant","content":content})
                except Exception as e:
                    row["error"]=f"DECODE:{type(e).__name__}:{e}"
            else:
                text=body.decode(errors="replace") if isinstance(body,(bytes,bytearray)) else str(body)
                row["error"]="CLIENT_TIMEOUT" if text=="CLIENT_TIMEOUT" else text[:500]
            rows.append(row)
            if status!=200:
                break
    finally:
        await client.close()
    return rows

async def run_wave(base_url,vu,timeout,scenario,model,wave,max_tokens):
    event=asyncio.Event()
    tasks=[asyncio.create_task(one_conversation(base_url,timeout,scenario,model,i+1,wave,event,max_tokens)) for i in range(vu)]
    await asyncio.sleep(0)
    event.set()
    results=await asyncio.gather(*tasks,return_exceptions=True)
    rows=[]
    for x in results:
        if isinstance(x,Exception):
            rows.append({
              "vu_id":None,"wave":wave,"turn":None,"status":None,"latency_s":None,
              "validation_ok":False,"prompt_tokens":None,"completion_tokens":None,
              "total_tokens":None,"content":None,
              "error":f"WORKER:{type(x).__name__}:{x}"
            })
        else:
            rows.extend(x)
    return rows

def host_available_gb():
    return read_meminfo()["ram_available_gb"]

def endpoint_healthy(url):
    st,_=sync_get_json(url.rstrip("/")+"/v1/models",timeout=10)
    return st==200

def wait_gpu_idle(gpu=0,threshold=10,consecutive=2,timeout=90):
    amd=shutil.which("amd-smi")
    if not amd:
        time.sleep(2); return False
    good=0; deadline=time.time()+timeout
    while time.time()<deadline:
        try:
            cp=subprocess.run([amd,"monitor","-g",str(gpu)],capture_output=True,text=True,timeout=10)
            g=parse_gpu(cp.stdout,gpu)
            if g and g["gpu_gfx_util_pct"]<=threshold:
                good+=1
                if good>=consecutive:
                    return True
            else:
                good=0
        except Exception:
            good=0
        time.sleep(1)
    return False

def run_level(name,url,vu,scenario,model,max_tokens,timeout,min_conversations,outdir,gpu):
    waves=max(1,math.ceil(min_conversations/vu))
    all_rows=[]
    tel=Telemetry(outdir,gpu=gpu,interval=1.0)
    tel.start()
    t0=time.perf_counter()
    try:
        for wave in range(1,waves+1):
            all_rows.extend(asyncio.run(run_wave(url,vu,timeout,scenario,model,wave,max_tokens)))
    finally:
        wall=time.perf_counter()-t0
        tel.stop()

    attempted=len(all_rows)
    ok=[r for r in all_rows if r.get("status")==200]
    lats=[r.get("latency_s") for r in ok if r.get("latency_s") is not None]
    conv_keys={(r.get("wave"),r.get("vu_id")) for r in all_rows if r.get("vu_id") is not None}
    completed=0
    for key in conv_keys:
        rr=[r for r in all_rows if (r.get("wave"),r.get("vu_id"))==key]
        if len(rr)==len(scenario["turns"]) and all(r.get("status")==200 for r in rr):
            completed+=1
    errors=Counter((r.get("error") or f"HTTP_{r.get('status')}") for r in all_rows if r.get("status")!=200)
    completion_tokens=sum(r.get("completion_tokens") or 0 for r in ok)
    total_tokens=sum(r.get("total_tokens") or 0 for r in ok)
    val=sum(1 for r in ok if r.get("validation_ok"))
    health=endpoint_healthy(url)

    summary={
      "endpoint":name,"url":url,"vus":vu,"waves":waves,
      "planned_conversations":vu*waves,
      "attempted_requests":attempted,"http_200":len(ok),
      "request_success_rate":round(len(ok)/attempted,6) if attempted else 0,
      "completed_conversations":completed,
      "conversation_completion_rate":round(completed/(vu*waves),6),
      "validation_pass_rate":round(val/len(ok),6) if ok else 0,
      "wall_s":round(wall,4),
      "request_rps":round(len(ok)/wall,4) if wall else None,
      "completion_tokens_per_s":round(completion_tokens/wall,4) if wall else None,
      "total_tokens_per_s":round(total_tokens/wall,4) if wall else None,
      "latency_s":stat_block(lats),
      "error_counts":dict(errors),
      "completion_tokens_total":completion_tokens,
      "prompt_tokens_total":sum(r.get("prompt_tokens") or 0 for r in ok),
      "endpoint_healthy_after":health,
      "telemetry":tel.summary(),
      "skipped":False,
    }
    outdir.mkdir(parents=True,exist_ok=True)
    (outdir/"rows.json").write_text(json.dumps(all_rows,ensure_ascii=False,indent=2),encoding="utf-8")
    (outdir/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    return summary

def skipped_summary(name,url,vu,reason):
    return {
      "endpoint":name,"url":url,"vus":vu,"skipped":True,"skip_reason":reason,
      "request_success_rate":None,"conversation_completion_rate":None,
      "validation_pass_rate":None,"request_rps":None,"completion_tokens_per_s":None,
      "latency_s":{"mean":None,"p50":None,"p95":None,"p99":None,"max":None,"min":None},
      "error_counts":{},"telemetry":{}
    }

def sustainable(s,p95,success,conv):
    return (
      not s.get("skipped") and
      s.get("request_success_rate",0)>=success and
      s.get("conversation_completion_rate",0)>=conv and
      s.get("latency_s",{}).get("p95") is not None and
      s["latency_s"]["p95"]<=p95
    )

def hard_failed(s,success_floor,conv_floor):
    return (
      not s.get("skipped") and (
        not s.get("endpoint_healthy_after",True) or
        s.get("request_success_rate",0)<success_floor or
        s.get("conversation_completion_rate",0)<conv_floor
      )
    )

def write_reports(root,blue,green,slo_p95,success_floor,conversation_floor):
    bmap={x["vus"]:x for x in blue}; gmap={x["vus"]:x for x in green}
    levels=sorted(set(bmap)|set(gmap))
    rows=[]
    for vu in levels:
        b=bmap.get(vu); g=gmap.get(vu)
        rows.append({
          "vus":vu,
          "blue_skipped":None if not b else b.get("skipped",False),
          "green_skipped":None if not g else g.get("skipped",False),
          "blue_success":None if not b else b.get("request_success_rate"),
          "green_success":None if not g else g.get("request_success_rate"),
          "blue_conv_complete":None if not b else b.get("conversation_completion_rate"),
          "green_conv_complete":None if not g else g.get("conversation_completion_rate"),
          "blue_rps":None if not b else b.get("request_rps"),
          "green_rps":None if not g else g.get("request_rps"),
          "rps_gain":ratio(None if not g else g.get("request_rps"),None if not b else b.get("request_rps")),
          "blue_tok_s":None if not b else b.get("completion_tokens_per_s"),
          "green_tok_s":None if not g else g.get("completion_tokens_per_s"),
          "tok_s_gain":ratio(None if not g else g.get("completion_tokens_per_s"),None if not b else b.get("completion_tokens_per_s")),
          "blue_p95_s":None if not b else b.get("latency_s",{}).get("p95"),
          "green_p95_s":None if not g else g.get("latency_s",{}).get("p95"),
          "p95_speedup_blue_over_green":ratio(None if not b else b.get("latency_s",{}).get("p95"),None if not g else g.get("latency_s",{}).get("p95")),
          "blue_sustainable":False if not b else sustainable(b,slo_p95,success_floor,conversation_floor),
          "green_sustainable":False if not g else sustainable(g,slo_p95,success_floor,conversation_floor),
        })

    bcap=max((r["vus"] for r in rows if r["blue_sustainable"]),default=None)
    gcap=max((r["vus"] for r in rows if r["green_sustainable"]),default=None)
    bpeak=max((x.get("completion_tokens_per_s") or 0 for x in blue if not x.get("skipped")),default=0)
    gpeak=max((x.get("completion_tokens_per_s") or 0 for x in green if not x.get("skipped")),default=0)

    summary={
      "slo":{"p95_s":slo_p95,"request_success_floor":success_floor,"conversation_completion_floor":conversation_floor},
      "blue_sustainable_max_vu":bcap,
      "green_sustainable_max_vu":gcap,
      "sustainable_capacity_gain":ratio(gcap,bcap),
      "blue_peak_completion_tok_s":bpeak,
      "green_peak_completion_tok_s":gpeak,
      "peak_completion_throughput_gain":ratio(gpeak,bpeak),
      "levels":rows,
    }
    (root/"comparison.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    if rows:
        with (root/"comparison.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

    md=[
      "# :8000 vs :8200 Multi-VU Multi-Turn Capacity",
      "",
      f"SLO: success ≥ {success_floor:.0%}, conversation completion ≥ {conversation_floor:.0%}, p95 ≤ {slo_p95}s",
      "",
      "| VU | Blue success | Green success | Blue RPS | Green RPS | RPS gain | Blue tok/s | Green tok/s | tok/s gain | Blue p95 | Green p95 |",
      "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    ]
    def fmtp(v): return "SKIP" if v is None else f"{v:.1%}"
    def fmtn(v,n=3): return "NA" if v is None else f"{v:.{n}f}"
    for r in rows:
        md.append(
          f"| {r['vus']} | {fmtp(r['blue_success'])} | {fmtp(r['green_success'])} | "
          f"{fmtn(r['blue_rps'])} | {fmtn(r['green_rps'])} | {fmtn(r['rps_gain'],2)}× | "
          f"{fmtn(r['blue_tok_s'],2)} | {fmtn(r['green_tok_s'],2)} | {fmtn(r['tok_s_gain'],2)}× | "
          f"{fmtn(r['blue_p95_s'])} | {fmtn(r['green_p95_s'])} |"
        )
    md += [
      "",
      f"- Blue sustainable max VU: **{bcap}**",
      f"- Green sustainable max VU: **{gcap}**",
      f"- Sustainable-capacity gain: **{summary['sustainable_capacity_gain']}×**",
      f"- Blue peak completion throughput: **{bpeak:.2f} tok/s**",
      f"- Green peak completion throughput: **{gpeak:.2f} tok/s**",
      f"- Peak completion-throughput gain: **{summary['peak_completion_throughput_gain']}×**",
      "",
      "Higher VU than the active sequence limit measures scheduler/queue/backpressure capacity, not simultaneous decoding of every VU."
    ]
    (root/"REPORT.md").write_text("\n".join(md)+"\n",encoding="utf-8")
    return summary

def maybe_raise_nofile(target):
    soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
    before=[soft,hard]
    if soft<target:
        try: resource.setrlimit(resource.RLIMIT_NOFILE,(min(target,hard),hard))
        except Exception: pass
    return {"before":before,"after":list(resource.getrlimit(resource.RLIMIT_NOFILE))}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--blue-url",default="http://127.0.0.1:8000")
    ap.add_argument("--green-url",default="http://127.0.0.1:8200")
    ap.add_argument("--model",default="taide-12b")
    ap.add_argument("--scenario",default=str(Path(__file__).with_name("multiturn_scenario.json")))
    ap.add_argument("--vu-list",default=",".join(map(str,DEFAULT_VUS)))
    ap.add_argument("--request-timeout",type=float,default=30.0)
    ap.add_argument("--max-tokens",type=int,default=64)
    ap.add_argument("--min-conversations-per-level",type=int,default=8)
    ap.add_argument("--cooldown-s",type=float,default=2.0)
    ap.add_argument("--gpu",type=int,default=0)
    ap.add_argument("--slo-p95",type=float,default=15.0)
    ap.add_argument("--success-floor",type=float,default=.99)
    ap.add_argument("--conversation-floor",type=float,default=.95)
    ap.add_argument("--hard-stop-success",type=float,default=.50)
    ap.add_argument("--hard-stop-conversation",type=float,default=.25)
    ap.add_argument("--min-host-available-gb",type=float,default=8.0)
    ap.add_argument("--output-dir",required=True)
    a=ap.parse_args()

    vus=parse_vus(a.vu_list)
    scenario=json.loads(Path(a.scenario).read_text(encoding="utf-8"))
    out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)

    for name,url in (("blue",a.blue_url),("green",a.green_url)):
        if not endpoint_healthy(url):
            raise SystemExit(f"{name} endpoint not healthy: {url}")

    inventory={
      "created":iso_now(),
      "blue":listener_inventory(urlparse(a.blue_url).port or 80),
      "green":listener_inventory(urlparse(a.green_url).port or 80),
      "nofile":maybe_raise_nofile(16384),
      "vu_levels":vus,
      "request_timeout_s":a.request_timeout,
      "scenario":scenario,
    }
    (out/"inventory.json").write_text(json.dumps(inventory,ensure_ascii=False,indent=2),encoding="utf-8")

    results={"blue":[],"green":[]}
    active={"blue":True,"green":True}
    stop_reason={"blue":None,"green":None}

    for idx,vu in enumerate(vus):
        order=[("blue",a.blue_url),("green",a.green_url)] if idx%2==0 else [("green",a.green_url),("blue",a.blue_url)]
        for name,url in order:
            if not active[name]:
                s=skipped_summary(name,url,vu,stop_reason[name] or "previous hard failure")
                results[name].append(s)
                continue

            if host_available_gb()<a.min_host_available_gb:
                stop_reason[name]=f"host RAM available below {a.min_host_available_gb} GB"
                active[name]=False
                results[name].append(skipped_summary(name,url,vu,stop_reason[name]))
                continue

            print(f"\n===== VU={vu} endpoint={name} =====",flush=True)
            wait_gpu_idle(a.gpu)
            time.sleep(a.cooldown_s)
            s=run_level(
                name,url,vu,scenario,a.model,a.max_tokens,a.request_timeout,
                a.min_conversations_per_level,out/name/f"vu{vu}",a.gpu
            )
            results[name].append(s)
            print(json.dumps({
              "endpoint":name,"vus":vu,
              "success":s["request_success_rate"],
              "conversation_complete":s["conversation_completion_rate"],
              "rps":s["request_rps"],
              "completion_tok_s":s["completion_tokens_per_s"],
              "p95":s["latency_s"]["p95"],
              "errors":s["error_counts"],
              "healthy_after":s["endpoint_healthy_after"],
            },ensure_ascii=False),flush=True)

            if hard_failed(s,a.hard_stop_success,a.hard_stop_conversation):
                active[name]=False
                stop_reason[name]=(
                  f"hard-stop after VU={vu}: success={s['request_success_rate']}, "
                  f"conversation={s['conversation_completion_rate']}, healthy={s['endpoint_healthy_after']}"
                )
                print(f"[HARD STOP] {name}: {stop_reason[name]}",flush=True)
            wait_gpu_idle(a.gpu)

    blue=sorted(results["blue"],key=lambda x:x["vus"])
    green=sorted(results["green"],key=lambda x:x["vus"])
    (out/"blue_summary.json").write_text(json.dumps(blue,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"green_summary.json").write_text(json.dumps(green,ensure_ascii=False,indent=2),encoding="utf-8")
    final=write_reports(out,blue,green,a.slo_p95,a.success_floor,a.conversation_floor)
    final["stop_reason"]=stop_reason
    (out/"comparison.json").write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n===== FINAL =====")
    print(json.dumps(final,ensure_ascii=False,indent=2))
    print(f"Artifacts: {out}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())

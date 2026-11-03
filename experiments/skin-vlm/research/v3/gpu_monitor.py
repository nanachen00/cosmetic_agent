"""Record AMD GPU telemetry while running one research command."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from .facial_research import write_json
except ImportError:
    from facial_research import write_json


def _numbers(value: Any, prefix: str="") -> Dict[str,float]:
    result={}
    if isinstance(value,dict):
        for key,item in value.items(): result.update(_numbers(item,(prefix+"."+str(key)).strip(".")))
    elif isinstance(value,list):
        for i,item in enumerate(value): result.update(_numbers(item,prefix+"[%d]"%i))
    elif isinstance(value,(int,float)) and math.isfinite(float(value)): result[prefix]=float(value)
    elif isinstance(value,str):
        try:
            cleaned=value.replace("%","").replace("W","").replace("C","").strip()
            result[prefix]=float(cleaned)
        except ValueError: pass
    return result


def _cli_sample() -> Dict[str,Any]:
    commands=[]
    if shutil.which("amd-smi"): commands.append(["amd-smi","metric","--gpu","all","--json"])
    if shutil.which("rocm-smi"): commands.append(["rocm-smi","--showuse","--showmemuse","--showtemp","--showpower","--json"])
    errors=[]
    for command in commands:
        try:
            run=subprocess.run(command,capture_output=True,text=True,timeout=8,check=False)
            if run.returncode==0:
                raw=json.loads(run.stdout)
                return {"source":command[0],"metrics":_numbers(raw)}
            errors.append(command[0]+" exit "+str(run.returncode))
        except Exception as exc: errors.append(command[0]+": "+type(exc).__name__)
    return {"source":"unavailable","metrics":{},"errors":errors or ["amd-smi/rocm-smi not found"]}


def _torch_sample() -> Dict[str,Any]:
    try:
        import torch
        if not torch.cuda.is_available(): return {"available":False}
        free,total=torch.cuda.mem_get_info()
        return {"available":True,"device":torch.cuda.get_device_name(0),"free_bytes":int(free),
                "total_bytes":int(total),"process_allocated_bytes":int(torch.cuda.memory_allocated()),
                "process_reserved_bytes":int(torch.cuda.memory_reserved()),
                "process_peak_allocated_bytes":int(torch.cuda.max_memory_allocated()),"rocm":torch.version.hip}
    except Exception as exc:return {"available":False,"error":type(exc).__name__+": "+str(exc)}


def sample(start: float) -> Dict[str,Any]:
    return {"timestamp_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
            "elapsed_seconds":round(time.monotonic()-start,3),"device_cli":_cli_sample(),"torch":_torch_sample()}


def summarize(rows: Sequence[Mapping[str,Any]], command: Sequence[str], returncode: int) -> Dict[str,Any]:
    series={}
    for row in rows:
        for key,value in row.get("device_cli",{}).get("metrics",{}).items(): series.setdefault(key,[]).append(float(value))
    useful={key:{"mean":statistics.mean(values),"max":max(values),"min":min(values)}
            for key,values in series.items() if any(word in key.lower() for word in ("util","use","memory","power","temp"))}
    return {"command":list(command),"returncode":returncode,"samples":len(rows),
            "duration_seconds":rows[-1]["elapsed_seconds"] if rows else 0,
            "telemetry_series":useful,
            "note":"Metric names are preserved from amd-smi/rocm-smi because ROCm CLI schemas vary by version."}


def run(command: Sequence[str], output: Path, interval: float) -> int:
    if not command: raise ValueError("training command is required after --")
    output.parent.mkdir(parents=True,exist_ok=True);start=time.monotonic();rows=[]
    process=subprocess.Popen(list(command))
    with output.open("w",encoding="utf-8") as handle:
        while True:
            row=sample(start);rows.append(row);handle.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+"\n");handle.flush()
            code=process.poll()
            if code is not None: break
            time.sleep(interval)
    summary=summarize(rows,command,process.returncode)
    write_json(output.with_suffix(".summary.json"),summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2));return int(process.returncode)


def main() -> None:
    p=argparse.ArgumentParser();p.add_argument("--output",type=Path,required=True)
    p.add_argument("--interval",type=float,default=2.0);p.add_argument("command",nargs=argparse.REMAINDER)
    a=p.parse_args();command=a.command[1:] if a.command and a.command[0]=="--" else a.command
    if not .5<=a.interval<=60: raise ValueError("interval must be 0.5..60 seconds")
    raise SystemExit(run(command,a.output,a.interval))


if __name__=="__main__":main()


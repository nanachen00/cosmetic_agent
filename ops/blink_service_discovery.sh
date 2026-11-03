#!/usr/bin/env bash
set -Eeuo pipefail

# BLINK AI / Shadow A-B service discovery
#
# Known project context:
#   - working tree / harness root:
#       /var/lib/jenkins/tomod/ops/local-llm-path-benchmark
#   - result root:
#       /var/lib/jenkins/tomod/ops/results
#   - active comparison paths include :8000 and :8200
#   - this script DISCOVERS actual topology from process/config/source/runtime evidence.
#     It does not assume that either port is itself the raw model server.
#
# Usage:
#   chmod +x blink_service_discovery.sh
#   ./blink_service_discovery.sh
#
# Optional:
#   ./blink_service_discovery.sh my-run-name
#   RESULT_ROOT=/var/lib/jenkins/tomod/ops/results ./blink_service_discovery.sh
#
# Output:
#   /var/lib/jenkins/tomod/ops/results/<run-id>/service_discovery.json
#   plus raw evidence files in the same run directory.
#
# This script intentionally does NOT use /tmp or /temp.

RESULT_ROOT="${RESULT_ROOT:-/var/lib/jenkins/tomod/ops/results}"
BENCH_ROOT="${BENCH_ROOT:-/var/lib/jenkins/tomod/ops/local-llm-path-benchmark}"
RUN_LABEL="${1:-service-discovery}"
TS="$(date +%Y%m%d-%H%M%S)"
HOST="$(hostname -s 2>/dev/null || hostname)"
RUN_ID="${RUN_LABEL}-${TS}-${HOST}"
RUN_DIR="${RESULT_ROOT%/}/${RUN_ID}"
RAW_DIR="${RUN_DIR}/raw"
JSON_OUT="${RUN_DIR}/service_discovery.json"
JSONL="${RUN_DIR}/records.jsonl"

mkdir -p "$RAW_DIR"
: > "$JSONL"

if [[ "$RUN_DIR" != "${RESULT_ROOT%/}/"* ]]; then
  echo "ERROR: RUN_DIR escaped RESULT_ROOT: $RUN_DIR" >&2
  exit 2
fi

have() { command -v "$1" >/dev/null 2>&1; }

if ! have python3; then
  echo "ERROR: python3 is required to build JSON." >&2
  exit 3
fi

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&2
}

record_value() {
  local key="$1"
  local value="$2"
  KEY="$key" VALUE="$value" python3 - <<'PY' >> "$JSONL"
import json, os
print(json.dumps(
    {"key": os.environ["KEY"], "value": os.environ["VALUE"]},
    ensure_ascii=False
))
PY
}

capture() {
  local key="$1"
  shift
  local cmd="$*"
  local safe="${key//[^A-Za-z0-9_.-]/_}"
  local out="${RAW_DIR}/${safe}.stdout.txt"
  local err="${RAW_DIR}/${safe}.stderr.txt"
  local rc=0

  set +e
  bash -lc "$cmd" >"$out" 2>"$err"
  rc=$?
  set -e

  KEY="$key" CMD="$cmd" RC="$rc" OUT="$out" ERR="$err" python3 - <<'PY' >> "$JSONL"
import json, os
from pathlib import Path

def read(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

print(json.dumps({
    "key": os.environ["KEY"],
    "command": os.environ["CMD"],
    "exit_code": int(os.environ["RC"]),
    "stdout": read(os.environ["OUT"]),
    "stderr": read(os.environ["ERR"]),
    "stdout_file": os.environ["OUT"],
    "stderr_file": os.environ["ERR"],
}, ensure_ascii=False))
PY
}

log "Run directory: $RUN_DIR"

# ---------------------------------------------------------------------------
# 0. Metadata / project roots
# ---------------------------------------------------------------------------

record_value "metadata.generated_at" "$(date --iso-8601=seconds 2>/dev/null || date)"
record_value "metadata.host" "$HOST"
record_value "metadata.user" "$(id -un)"
record_value "metadata.cwd" "$(pwd)"
record_value "metadata.result_root" "$RESULT_ROOT"
record_value "metadata.run_dir" "$RUN_DIR"
record_value "metadata.benchmark_root" "$BENCH_ROOT"
record_value "metadata.script_version" "blink-shadow-discovery-v2"
record_value "metadata.known_compare_ports" "8000,8200"
record_value "metadata.note" "Topology is discovered from runtime/config/source evidence; :8000 and :8200 are not assumed to be raw model servers."

# ---------------------------------------------------------------------------
# 1. Host / OS / kernel / resource baseline
# ---------------------------------------------------------------------------

log "Collecting host baseline"

capture "host.os_release" "cat /etc/os-release 2>/dev/null || true"
capture "host.uname" "uname -a"
capture "host.kernel" "uname -r"
capture "host.arch" "uname -m"
capture "host.cpu" "lscpu 2>/dev/null || cat /proc/cpuinfo"
capture "host.memory" "free -h; echo; cat /proc/meminfo"
capture "host.disk" "df -hT"
capture "host.load" "uptime"
capture "host.clock" "date --iso-8601=seconds 2>/dev/null || date"

# ---------------------------------------------------------------------------
# 2. AMD GPU / ROCm / kernel driver
# ---------------------------------------------------------------------------

log "Collecting AMD GPU / ROCm evidence"

capture "gpu.pci" "lspci -nnk 2>/dev/null | grep -EA5 -i 'VGA|Display|3D|AMD|ATI' || true"
capture "gpu.modules" "lsmod | grep -Ei 'amdgpu|radeon' || true"
capture "gpu.amdgpu_modinfo" "modinfo amdgpu 2>/dev/null || true"
capture "gpu.drm" "for x in /sys/class/drm/card*/device; do echo ==== \$x ====; cat \$x/vendor \$x/device 2>/dev/null; done"

capture "rocm.paths" "ls -ld /opt/rocm* 2>/dev/null || true"
capture "rocm.version" "cat /opt/rocm/.info/version 2>/dev/null || cat /opt/rocm/.info/version-dev 2>/dev/null || hipcc --version 2>/dev/null || true"
capture "rocm.rocminfo" "command -v rocminfo >/dev/null && rocminfo || true"
capture "rocm.hipcc" "command -v hipcc >/dev/null && hipcc --version || true"
capture "rocm.rocm_smi" "command -v rocm-smi >/dev/null && rocm-smi || true"
capture "rocm.rocm_smi_product" "command -v rocm-smi >/dev/null && rocm-smi --showproductname || true"
capture "rocm.rocm_smi_memory" "command -v rocm-smi >/dev/null && rocm-smi --showmeminfo vram --showuse || true"
capture "rocm.amd_smi_version" "command -v amd-smi >/dev/null && amd-smi version || true"
capture "rocm.amd_smi_static" "command -v amd-smi >/dev/null && amd-smi static || true"
capture "rocm.amd_smi_metric" "command -v amd-smi >/dev/null && amd-smi metric || true"

# ---------------------------------------------------------------------------
# 3. Runtime processes and exact command lines
# ---------------------------------------------------------------------------

log "Collecting process topology"

capture "runtime.process_tree" "ps -eo pid,ppid,user,lstart,%cpu,%mem,args --forest"
capture "runtime.candidate_processes" \
"ps -eo pid,ppid,user,lstart,%cpu,%mem,args | grep -Ei 'python|uvicorn|gunicorn|fastapi|vllm|sglang|ollama|llama|transformers|ray|langgraph|langchain|celery|redis|node|jenkins' | grep -v grep || true"

capture "runtime.candidate_cmdlines" \
"for pid in \$(pgrep -f 'python|uvicorn|gunicorn|vllm|sglang|ollama|llama|ray|celery|node' 2>/dev/null | sort -n -u); do
  echo ==== PID:\$pid ====
  tr '\0' ' ' < /proc/\$pid/cmdline 2>/dev/null || true
  echo
done"

# Selected environment variables only; secrets are excluded.
capture "runtime.selected_env" \
"for pid in \$(pgrep -f 'python|uvicorn|gunicorn|vllm|sglang|ollama|llama|ray|celery|node' 2>/dev/null | sort -n -u); do
  echo ==== PID:\$pid ====
  tr '\0' '\n' < /proc/\$pid/environ 2>/dev/null |
  grep -E '^(ROCM|HIP|HSA|CUDA_VISIBLE_DEVICES|PYTHONPATH|VIRTUAL_ENV|CONDA_PREFIX|MODEL|MODEL_|VLLM|SGLANG|TRANSFORMERS|HF_HOME|HUGGINGFACE|TOKENIZERS_PARALLELISM|OMP_NUM_THREADS|MKL_NUM_THREADS|PORT|HOST|WORKERS|WEB_CONCURRENCY|BATCH|MAX_NUM|MAX_MODEL|DTYPE|QUANT|TENSOR_PARALLEL|PIPELINE_PARALLEL|LANGGRAPH|LANGCHAIN)=' || true
done"

# ---------------------------------------------------------------------------
# 4. Listening sockets and ownership
# ---------------------------------------------------------------------------

log "Collecting network topology"

capture "network.listeners" "ss -lntup 2>/dev/null || netstat -lntup 2>/dev/null || true"
capture "network.ports_8000_8200" "ss -lntp 2>/dev/null | grep -E ':(8000|8200)\\b' || true"
capture "network.local_connections" "ss -ntp 2>/dev/null | grep -E '127\\.0\\.0\\.1|::1' || true"

for port in 8000 8200; do
  capture "network.port_${port}_owner" \
  "ss -lntp 2>/dev/null | grep -E ':${port}\\b' || true"

  capture "http.${port}.root" \
  "curl -i -sS --max-time 8 http://127.0.0.1:${port}/ 2>&1 || true"

  capture "http.${port}.health" \
  "for p in /health /health/live /health/ready /ready /live; do
     echo ==== \$p ====
     curl -i -sS --max-time 5 http://127.0.0.1:${port}\$p 2>&1 || true
     echo
   done"

  capture "http.${port}.openapi" \
  "curl -sS --max-time 8 http://127.0.0.1:${port}/openapi.json 2>&1 || true"

  capture "http.${port}.docs_headers" \
  "curl -I -sS --max-time 5 http://127.0.0.1:${port}/docs 2>&1 || true"

  capture "http.${port}.metrics" \
  "curl -sS --max-time 8 http://127.0.0.1:${port}/metrics 2>&1 | head -n 1000 || true"

  # This is discovery only. Failure is expected if the endpoint is not OpenAI-compatible.
  capture "http.${port}.v1_models_probe" \
  "curl -i -sS --max-time 8 http://127.0.0.1:${port}/v1/models 2>&1 || true"
done

# ---------------------------------------------------------------------------
# 5. Python runtime and installed AI / Agent stack
# ---------------------------------------------------------------------------

log "Collecting Python / framework versions"

capture "python.executables" "which -a python3 python pip3 pip 2>/dev/null || true"
capture "python.version" "python3 --version 2>&1 || true"
capture "python.runtime" \
"python3 - <<'PY'
import json, os, sys, platform
print(json.dumps({
  'executable': sys.executable,
  'version': sys.version,
  'prefix': sys.prefix,
  'base_prefix': sys.base_prefix,
  'virtual_env': os.environ.get('VIRTUAL_ENV'),
  'conda_prefix': os.environ.get('CONDA_PREFIX'),
  'platform': platform.platform(),
}, ensure_ascii=False, indent=2))
PY"

capture "python.ai_packages" \
"python3 -m pip list 2>/dev/null | grep -Ei 'torch|vllm|sglang|transformers|triton|flash|accelerate|deepspeed|bitsandbytes|awq|gptq|onnx|fastapi|uvicorn|gunicorn|langchain|langgraph|ray|celery|redis|tokenizers|huggingface|sentence|pydantic|httpx|aiohttp' || true"

capture "python.framework_versions" \
"python3 - <<'PY'
import json, importlib.metadata
pkgs = [
 'torch','vllm','sglang','transformers','triton','accelerate','deepspeed',
 'bitsandbytes','autoawq','auto-gptq','onnxruntime','fastapi','uvicorn',
 'gunicorn','langchain','langgraph','ray','celery','redis','tokenizers',
 'huggingface-hub','sentence-transformers','pydantic','httpx','aiohttp'
]
out={}
for p in pkgs:
    try: out[p]=importlib.metadata.version(p)
    except Exception: out[p]=None
print(json.dumps(out, ensure_ascii=False, indent=2))
PY"

capture "python.torch_rocm" \
"python3 - <<'PY'
import json
r={}
try:
 import torch
 r['torch_version']=torch.__version__
 r['hip_version']=getattr(torch.version,'hip',None)
 r['cuda_version']=getattr(torch.version,'cuda',None)
 r['cuda_api_available']=torch.cuda.is_available()
 r['device_count']=torch.cuda.device_count()
 r['devices']=[]
 for i in range(torch.cuda.device_count()):
  p=torch.cuda.get_device_properties(i)
  r['devices'].append({
   'index':i,
   'name':p.name,
   'total_memory_bytes':p.total_memory,
   'major':getattr(p,'major',None),
   'minor':getattr(p,'minor',None),
  })
except Exception as e:
 r['error']=repr(e)
print(json.dumps(r,ensure_ascii=False,indent=2))
PY"

# ---------------------------------------------------------------------------
# 6. Container / service manager topology
# ---------------------------------------------------------------------------

log "Collecting service/container evidence"

capture "systemd.ai_units" \
"systemctl list-units --type=service --all --no-pager 2>/dev/null | grep -Ei 'tomod|blink|llm|model|agent|api|uvicorn|gunicorn|vllm|sglang|ollama|ray|redis|celery' || true"

capture "systemd.ai_unit_files" \
"systemctl list-unit-files --type=service --no-pager 2>/dev/null | grep -Ei 'tomod|blink|llm|model|agent|api|uvicorn|gunicorn|vllm|sglang|ollama|ray|redis|celery' || true"

capture "docker.version" "docker version 2>&1 || true"
capture "docker.ps" "docker ps --no-trunc --format '{{json .}}' 2>&1 || true"
capture "docker.compose" "docker compose ls 2>&1 || true"
capture "podman.ps" "podman ps --no-trunc 2>&1 || true"
capture "k8s.context" "kubectl config current-context 2>&1 || true"
capture "k8s.pods" "kubectl get pods -A -o wide 2>&1 || true"

# ---------------------------------------------------------------------------
# 7. Project repository / benchmark harness discovery
# ---------------------------------------------------------------------------

log "Inspecting benchmark and service source"

capture "project.root_listing" \
"if [ -d '$BENCH_ROOT' ]; then find '$BENCH_ROOT' -maxdepth 2 -mindepth 1 -printf '%y %p\n' | sort; else echo 'MISSING: $BENCH_ROOT'; fi"

capture "project.git_status" \
"if [ -d '$BENCH_ROOT/.git' ]; then
   git -C '$BENCH_ROOT' status --short --branch
   echo
   git -C '$BENCH_ROOT' log -n 12 --date=iso --pretty=format:'%H%x09%ad%x09%s'
 fi"

capture "project.config_files" \
"if [ -d '$BENCH_ROOT' ]; then
 find '$BENCH_ROOT' -type f \
   \\( -name '*.yaml' -o -name '*.yml' -o -name '*.json' -o -name '*.toml' \
      -o -name '*.ini' -o -name '*.cfg' -o -name '*.env.example' \
      -o -name 'Dockerfile*' -o -name 'docker-compose*' -o -name '*.service' \
      -o -name '*.sh' -o -name '*.py' \\) \
   -printf '%p\n' | sort
 fi"

# Grep only structure-relevant literals. Do not dump .env or credentials.
capture "project.topology_refs" \
"if [ -d '$BENCH_ROOT' ]; then
 grep -RInE \
   --exclude-dir=.git \
   --exclude='*.log' \
   --exclude='*.tar.gz' \
   --exclude='*.zip' \
   --exclude='*.env' \
   --exclude='*secret*' \
   --exclude='*credential*' \
   '(localhost|127\\.0\\.0\\.1|:8000|:8200|BASE_URL|MODEL_URL|LLM_URL|AGENT|router|parser|shadow|ablation|benchmark|vllm|sglang|ollama|transformers|completion|chat|invoke|stream|token|batch|concurr|semaphore|queue)' \
   '$BENCH_ROOT' 2>/dev/null | head -n 20000 || true
 fi"

capture "project.entrypoints" \
"if [ -d '$BENCH_ROOT' ]; then
 grep -RInE \
   --include='*.py' --include='*.sh' --include='*.yaml' --include='*.yml' --include='*.toml' \
   --exclude-dir=.git \
   '(if __name__ == .__main__.|uvicorn.run|FastAPI\\(|APIRouter\\(|@app\\.|@router\\.|langgraph|StateGraph|workflow|orchestr|parser|shadow|ablation)' \
   '$BENCH_ROOT' 2>/dev/null | head -n 20000 || true
 fi"

# ---------------------------------------------------------------------------
# 8. Existing Shadow A/B and Gate evidence
# ---------------------------------------------------------------------------

log "Collecting existing Shadow A/B evidence"

capture "shadow.existing_artifacts" \
"find '$BENCH_ROOT' '$RESULT_ROOT' -maxdepth 4 -type f 2>/dev/null |
 grep -Ei 'gate8|shadow|ablation|8000|8200|timeline|benchmark|evidence|diagnostic|summary|report' |
 sort | tail -n 5000 || true"

capture "shadow.recent_results_dirs" \
"find '$RESULT_ROOT' -maxdepth 2 -mindepth 1 -type d -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' 2>/dev/null |
 sort -r | head -n 200 || true"

# ---------------------------------------------------------------------------
# 9. Model files/configuration discovery
# ---------------------------------------------------------------------------

log "Collecting model/config evidence"

capture "model.cache_roots" \
"for d in \
  \"\${HF_HOME:-}\" \
  \"\$HOME/.cache/huggingface\" \
  /root/.cache/huggingface \
  /var/lib/jenkins/.cache/huggingface \
  /models /model
do
  [ -n \"\$d\" ] && [ -e \"\$d\" ] && echo ==== \$d ==== && du -sh \"\$d\" 2>/dev/null
done"

capture "model.config_candidates" \
"for base in \
  \"\${HF_HOME:-}\" \
  \"\$HOME/.cache/huggingface\" \
  /root/.cache/huggingface \
  /var/lib/jenkins/.cache/huggingface \
  /models /model
do
  [ -n \"\$base\" ] && [ -d \"\$base\" ] || continue
  find \"\$base\" -maxdepth 6 -type f \
    \\( -name config.json -o -name tokenizer_config.json -o -name generation_config.json -o -name '*.gguf' -o -name '*.safetensors' \\) \
    -printf '%p\n' 2>/dev/null | head -n 5000
done"

# ---------------------------------------------------------------------------
# 10. Logs: discover paths, do not blindly dump secrets
# ---------------------------------------------------------------------------

log "Discovering logs"

capture "logs.candidates" \
"find '$BENCH_ROOT' /var/log /var/lib/jenkins/tomod -maxdepth 5 -type f 2>/dev/null |
 grep -Ei '\\.(log|jsonl|ndjson)$|stdout|stderr|journal|trace|timing|metric' |
 sort | tail -n 5000 || true"

capture "logs.systemd_recent" \
"journalctl --since '2 hours ago' --no-pager 2>/dev/null |
 grep -Ei 'tomod|blink|8000|8200|llm|model|agent|shadow|parser|vllm|sglang|ollama|uvicorn|gunicorn' |
 tail -n 5000 || true"

# ---------------------------------------------------------------------------
# 11. Build a machine-readable topology summary from collected evidence
# ---------------------------------------------------------------------------

log "Building final JSON"

JSONL_PATH="$JSONL" JSON_OUT="$JSON_OUT" python3 - <<'PY'
import json, os, re
from pathlib import Path

src = Path(os.environ["JSONL_PATH"])
dst = Path(os.environ["JSON_OUT"])

root = {}

def put(obj, dotted, value):
    parts = dotted.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value

records = []
for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
    line=line.strip()
    if not line:
        continue
    try:
        rec=json.loads(line)
    except Exception:
        continue
    records.append(rec)
    key=rec.pop("key")
    if list(rec.keys()) == ["value"]:
        put(root,key,rec["value"])
    else:
        put(root,key,rec)

# Derived summary: intentionally conservative.
all_text = "\n".join(
    str(r.get("stdout","")) for r in records if isinstance(r,dict)
)

derived = {
    "ports_detected": {},
    "framework_hints": [],
    "agent_hints": [],
    "model_serving_hints": [],
}

for port in ("8000","8200"):
    m = re.findall(rf".*:{port}\b.*", all_text)
    derived["ports_detected"][port] = m[:100]

for name in ["vllm","sglang","ollama","transformers","torch","ray"]:
    if re.search(rf"\b{name}\b", all_text, re.I):
        derived["framework_hints"].append(name)

for name in ["langgraph","langchain","StateGraph","orchestrator","router","parser"]:
    if re.search(re.escape(name), all_text, re.I):
        derived["agent_hints"].append(name)

for pattern in [
    r"--model\s+\S+",
    r"--dtype\s+\S+",
    r"--quantization\s+\S+",
    r"--tensor-parallel-size\s+\S+",
    r"--pipeline-parallel-size\s+\S+",
    r"--max-model-len\s+\S+",
    r"--max-num-seqs\s+\S+",
]:
    hits = re.findall(pattern, all_text, re.I)
    derived["model_serving_hints"].extend(hits[:50])

root["derived"] = derived

dst.write_text(
    json.dumps(root, ensure_ascii=False, indent=2),
    encoding="utf-8"
)
PY

# ---------------------------------------------------------------------------
# 12. Human-readable manifest
# ---------------------------------------------------------------------------

cat > "${RUN_DIR}/MANIFEST.txt" <<EOF
BLINK AI / Shadow A-B service discovery

Run ID:
  $RUN_ID

Benchmark root:
  $BENCH_ROOT

Result directory:
  $RUN_DIR

Primary JSON:
  $JSON_OUT

Known comparison paths:
  :8000
  :8200

Important:
  - This run discovers actual process/config/source topology.
  - It does NOT assume :8000 or :8200 is the raw model server.
  - No /tmp or /temp working directory is used.
  - Secret-like environment variables are intentionally excluded.
EOF

log "Completed"
printf '%s\n' "$JSON_OUT"

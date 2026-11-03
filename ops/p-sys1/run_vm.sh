#!/usr/bin/env bash
# P+Sys1 — VM 執行入口（在 $REPO/ops/p-sys1 下執行）。每個子命令都可以單獨重跑；不碰 :8010/:8200/vLLM/.env/production 目錄。
#
#   ./run_vm.sh where                    # 唯讀：python / conda env / GPU / 磁碟 / HF / vLLM / 埠（handoff §2「先判斷你在哪」）＋本機複現模擬／略過清單
#   ./run_vm.sh venv                     # 建 /opt/venv-sys1：預設以映像內建 py_3.10 為基底、沿用它的 ROCm torch（見下 VENV_MODE）
#   ./run_vm.sh selftest                 # 全部離線自測（gateB、sys1、train、shadow-sys1）；有 torch 時含 tiny-random-bert 路徑
#   ./run_vm.sh data                     # 產生共用訓練集 gateB-colab/sys1/train/data/v1（寫一次，不覆蓋）
#   ./run_vm.sh train <id> [seed…]       # id ∈ sys1/train/bases.json；預設 budget.json 的 3 個 seed；DEVICE=cpu|cuda（cuda 先檢查顯存）
#   ./run_vm.sh serve <id> <seed> [port] # 前景服務（127.0.0.1:8232-8239）
#   ./run_vm.sh contract <id> <seed> [port]   # 只驗契約（不產生評比資料、不需簽核）
#   ./run_vm.sh s2 <id> <seed> [port]    # 需負責人簽核來源：collect gate_data(all) + router_gold → analyze_cand（門檻寫一次凍結）
#   ./run_vm.sh laya-ctx                 # Laya 題組 token 數估算（ModernBERT 512 / mmBERT 1024）
#   ./run_vm.sh laya-venv                # 建 /opt/venv-laya（同 py_3.10 基底；torch/numpy/transformers 鎖在基底版本）＋ laya[serve]
#   ./run_vm.sh laya-serve [port]        # 前景服務 Laya（127.0.0.1:8231；sys1/laya_entry.py 補 /v1/models 與 latency_ms）
#   ./run_vm.sh laya-contract <id> [port]   # laya-mmbert｜laya-typed：契約＋noul 方向＋延遲；不碰 hold-out
#   ./run_vm.sh laya-s2 <id> [port]      # 同 s2：gate_data(all) → router_gold → analyze_cand（Laya 零樣本，沒有訓練步驟）
#   ./run_vm.sh kev-venv                 # Kev 對照：jaredpalmer/kev@1f04cfa2 ＋ Python 3.12 ＋ ROCm torch 2.8（$KEV_DIR/.venv）
#   ./run_vm.sh kev-gateb [run]          # Colab Gate B 的 P+K 部分搬到 VM（KEV_SIZE=0.8b|4b，KEV_DTYPE=bf16|fp32）
# 環境變數：PY（預設 /opt/venv-sys1/bin/python；venv 不存在時只有 where 會退回 PATH 的 python3）· DEVICE（預設 cpu）· MIN_FREE_VRAM_GB（預設 24）
#   venv：VENV_MODE=system-torch（預設）＋ BASE_PY（預設 /opt/conda/envs/py_3.10/bin/python）——10/20 重建後映像內建、
#           這台 VM 上唯一驗證過能用 GPU 的 torch 2.6.0 ROCm；venv 用 --system-site-packages 唯讀引用基底，不改基底。
#         VENV_MODE=wheel ＋ BASE_PY=<python3.11>（例如 /opt/conda/envs/blink-backend/bin/python3.11）：獨立 venv，pip 安裝
#           TORCH_SPEC / TORCH_INDEX（預設 torch==2.8.0 / rocm6.4；這個組合在重建後的容器上沒有驗證過）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; G="$HERE/gateB-colab"; S="$HERE/shadow-sys1"
VENV=/opt/venv-sys1; PY_SET="${PY:+1}"; PY="${PY:-$( [ -x $VENV/bin/python ] && echo $VENV/bin/python || echo python3 )}"
BASE_PY="${BASE_PY:-/opt/conda/envs/py_3.10/bin/python}"
DEVICE="${DEVICE:-cpu}"; DATA="$G/sys1/train/data/v1"; CK="$HERE/ckpt"; RUNS="$HERE/runs"
LAYA_VENV=/opt/venv-laya; KEV_DIR="${KEV_DIR:-/opt/kev-1f04cfa2}"; KEV_COMMIT=1f04cfa28e6a708485b28a9585010b9e436b776e
die() { echo "[run_vm] $*" >&2; exit 1; }
port_ok() { case "$1" in 823[0-9]) ;; *) die "埠 $1 不在 127.0.0.1:8230-8239（Sys1／Laya／Kev 只能用這段）";; esac; }
wait_up() { local i; for i in $(seq 1 "${2:-600}"); do curl -sf "$1" >/dev/null && return 0; sleep 1; done; return 1; }
free_vram_gb() { rocm-smi --showmeminfo vram --json 2>/dev/null | "$PY" -c "import json,sys;d=json.load(sys.stdin);c=next(iter(d.values()));t=int(c['VRAM Total Memory (B)']);u=int(c['VRAM Total Used Memory (B)']);print((t-u)//2**30)" || echo 0; }
need_laya() { [ -x $LAYA_VENV/bin/python ] || die "先 ./run_vm.sh laya-venv"; }
base_guard() {   # 新 venv 的基底：必須存在、不是 vLLM 環境、有可用 GPU 的 ROCm torch
  [ -x "$BASE_PY" ] || die "BASE_PY=$BASE_PY 不存在（./run_vm.sh where 會列出 conda env）"
  case "$(readlink -f "$BASE_PY")" in /opt/venv-llm/*|/workspace/llm-src/*) die "不准用 /opt/venv-llm 當基底";; esac
  "$BASE_PY" -c "import sys,torch;assert torch.version.hip,'不是 ROCm 版 torch';assert torch.cuda.is_available(),'看不到 GPU';print('[run_vm] 基底',sys.prefix,'python',sys.version.split()[0],'torch',torch.__version__,'hip',torch.version.hip)" \
    || die "$BASE_PY 沒有可用 GPU 的 ROCm torch"
}
s2_collect() {   # id port R：probe → gate_data(all) → 10 秒 → router_gold → analyze_cand；服務由呼叫者啟動與停止
  local id="$1" port="$2" R="$3"
  cd "$G"
  "$PY" sys1/run_cand.py probe   --cand "$id" --kev-url "http://127.0.0.1:$port"
  "$PY" sys1/run_cand.py collect --cand "$id" --run "$R/gd"   --split all --kev-url "http://127.0.0.1:$port" --vus 1
  echo "[run_vm] 下一步會第一次看 router_gold hold-out（之後不可再改門檻/題組/資料/超參數；handoff §4-2）。10 秒內 Ctrl-C 可取消。"; sleep 10
  "$PY" sys1/run_cand.py collect --cand "$id" --run "$R/gold" --dataset router_gold --kev-url "http://127.0.0.1:$port" --vus 1
  "$PY" sys1/analyze_cand.py --cand "$id" --cal-run "$R/gd" --hold-run "$R/gold" --kev-ref-run sys1/reference/gateB-t4-001 || true
  echo "[run_vm] → $R/gold/sys1_$id/SYS1_HOLDOUT.md（router_gold 已被看過：結果只能拿來淘汰）"
}
port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
sys1_start() {   # id seed port：背景啟動 serve_sys1，EXIT 時停掉；先確認埠是空的，啟動後確認埠上回應的就是這個 id 與 seed
  local id="$1" s="$2" port="$3" ck="$CK/$1-s$2" i
  [ -e "$ck/meta.json" ] || die "沒有 $ck/meta.json（先 ./run_vm.sh train $id $s）"
  port_free "$port" || die "127.0.0.1:$port 已有服務（可能是之前沒停掉的 serve_sys1）。用 ss -ltnp | grep :$port 查 pid，確認命令列後再停；或換埠"
  # ( … exec python ) &：背景子程序直接換成 python，$! 就是 python 本身，kill 才停得到（r8 以前停到的是外層 shell，python 會留著佔埠）
  ( cd "$G" && exec "$PY" -I sys1/train/serve_sys1.py --ckpt "$ck" --name "$id" --port "$port" --device "$DEVICE" >"$ck/serve.log" 2>&1 ) &
  SYS1_PID=$!; echo "$SYS1_PID" >"$CK/.serve-$port.pid"
  trap 'kill "$SYS1_PID" 2>/dev/null; wait "$SYS1_PID" 2>/dev/null || true' EXIT
  for i in $(seq 1 240); do
    kill -0 "$SYS1_PID" 2>/dev/null || die "serve_sys1 已結束；看 $ck/serve.log"
    curl -sf "127.0.0.1:$port/v1/models" >/dev/null && break; sleep 0.5
  done
  curl -sf "127.0.0.1:$port/v1/models" | "$PY" -c "import json,sys;d=json.load(sys.stdin)['data'][0];ok=d['id']==sys.argv[1] and str(d.get('seed'))==sys.argv[2];print('[run_vm] 服務',d);sys.exit(0 if ok else 3)" "$id" "$s"     || die "127.0.0.1:$port 回應的不是 $id seed $s：不量"
}
laya_run() {   # port [exec]：在 gateB-colab 下用 /opt/venv-laya 跑 sys1/laya_entry.py；LAYA_API_KEY 一律不帶
  local dev; dev=$( [ "$DEVICE" = cpu ] && echo cpu || echo cuda )
  # GPU 上的精度：LAYA_CUDA_AMP（預設 bf16 = 檢查點自帶的 amp_dtype；寫明是為了和 Kev bf16 對齊，並記進 /v1/models）
  cd "$G" && ${2:-} env -u LAYA_API_KEY LAYA_DEVICE="$dev" LAYA_CUDA_AMP="${LAYA_CUDA_AMP:-bf16}" LAYA_PRELOAD=1 LAYA_MODELS="${LAYA_MODELS:-multilingual,typed-decisions}" \
    HF_HUB_DISABLE_TELEMETRY=1 $LAYA_VENV/bin/python -I sys1/laya_entry.py --port "$1"
}
laya_start() {   # port：背景啟動（自己的 pid，EXIT 時停掉），等 /v1/models
  mkdir -p "$RUNS"; local log="$RUNS/laya-serve-$1.log"
  port_free "$1" || die "127.0.0.1:$1 已有服務；用 ss -ltnp | grep :$1 查 pid，確認命令列後再停；或換埠"
  ( laya_run "$1" exec >"$log" 2>&1 & echo $! >"$RUNS/.laya-$1.pid" )   # exec：$! 就是 python 本身
  trap "kill \"\$(cat '$RUNS/.laya-$1.pid')\" 2>/dev/null || true" EXIT
  echo "[run_vm] Laya 啟動中（第一次會從 Hugging Face 下載檢查點）；log: $log"
  wait_up "http://127.0.0.1:$1/v1/models" 1800 || die "Laya 30 分鐘內沒有起來；看 $log"
}
# PATH 上的 python3 是映像內建環境（production embedding 也用它）：除了 where，沒有 venv 又沒明確設 PY 就不跑
need_venv() { [ -n "$PY_SET" ] || [ -x $VENV/bin/python ] || die "先 ./run_vm.sh venv（或明確設 PY=…）；不拿 PATH 上的 python3 跑"; }
base_of() { "$PY" -c "import json,sys;b=json.load(open('$G/sys1/train/bases.json'))['bases'];c=b.get(sys.argv[1]);print('' if c is None else c['hf']+' '+str(int(c['trust_remote_code'])))" "$1"; }

case "${1:-}" in
where)
  echo "== host";      hostname; uname -r
  echo "== python"
  for c in python3 python3.10 python3.11; do printf '  %-11s %s\n' "$c" "$(command -v $c || echo '-')"; done
  python3 -c "import sys;print('  python3 =', sys.version.split()[0], sys.prefix)" 2>/dev/null || true
  echo "  PY（本腳本會用）= $PY$( [ -x $VENV/bin/python ] && echo '' || echo '  ← /opt/venv-sys1 還不存在：先 ./run_vm.sh venv')"
  echo "  BASE_PY（venv 基底）= $BASE_PY$( [ -x "$BASE_PY" ] && echo '' || echo '  ← 不存在')"
  echo "== conda envs"; ls /opt/conda/envs 2>/dev/null | sed 's/^/  /' || echo "  （沒有 /opt/conda/envs）"
  echo "== GPU";       (command -v rocm-smi >/dev/null && rocm-smi --showproductname --showmeminfo vram 2>/dev/null | sed -n 1,40p) || echo "rocm-smi 不存在 → 不是 GPU 主機：只做離線工作"
  echo "== 磁碟";      df -h /opt /data /root 2>/dev/null | sed 's/^/  /' || true
  echo "== Hugging Face（cache ${HF_HOME:-$HOME/.cache/huggingface}）"
  curl -sI -m 5 https://huggingface.co 2>/dev/null | head -1 | sed 's/^/  /' | grep . || echo "  連不到 huggingface.co：train 前要先把基座放進 HF cache"
  echo "== /opt";      ls /opt 2>/dev/null || true
  echo "== vLLM :8200 (唯讀)"; curl -s -m 3 127.0.0.1:8200/v1/models | head -c 400 || echo "(沒有回應)"; echo
  echo "== Sys1 埠 8230-8239"; for p in $(seq 8230 8239); do (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null && echo "  :$p 使用中" || true; done
  echo "== torch"
  for p in "$PY" "$BASE_PY"; do
    [ -x "$p" ] || command -v "$p" >/dev/null || continue
    "$p" -c "import sys,torch,transformers;print(' ',sys.prefix,'| torch',torch.__version__,'hip',getattr(torch.version,'hip',None),'cuda_avail',torch.cuda.is_available(),'| transformers',transformers.__version__)" 2>/dev/null || echo "  $p 沒有 torch/transformers"
  done
  LV="$HERE/local-validation/SIMULATION_MANIFEST.json"
  if [ -e "$LV" ]; then
    echo "== 本機複現（Windows）模擬／略過／未覆蓋的模組 → 在 VM 上必須用真實服務驗證"
    "$PY" -c "import json,sys;m=json.load(open(sys.argv[1],encoding='utf-8'));print('  本機 tomod 基底',m.get('bundle'),'＋ 本 bundle 的來源（sync-dev）· 產生',m.get('refreshed') or m.get('generated'));[print(f'  [{c}] {k}: {v}') for c in ('simulated','skipped','not_exercised') for k,v in m.get(c,{}).items()]" "$LV"
    echo "  證據：local-validation/LOCAL_SIM_EVIDENCE.md · LOCAL_S3_REPORT_*.md（本機 Sys1 = np-tiny-local，只驗證接線）"
  fi
  ;;
venv)
  mode="${VENV_MODE:-system-torch}"; [ "$mode" = system-torch ] || [ "$mode" = wheel ] || die "VENV_MODE 只能是 system-torch 或 wheel"
  [ -x "$BASE_PY" ] || die "BASE_PY=$BASE_PY 不存在（./run_vm.sh where 會列出 conda env）"
  case "$(readlink -f "$BASE_PY")" in /opt/venv-llm/*|/workspace/llm-src/*) die "不准用 /opt/venv-llm 當基底";; esac
  if [ -e $VENV/pyvenv.cfg ]; then             # 重跑可以（例如 pip 中途斷線）；換模式或換基底不行
    want=$( [ "$mode" = system-torch ] && echo true || echo false )
    grep -qiE "^include-system-site-packages *= *$want" $VENV/pyvenv.cfg && grep -qE "^home *= *$(dirname "$BASE_PY")/?$" $VENV/pyvenv.cfg \
      || die "$VENV 已存在，但它的基底或 site-packages 設定和這次（VENV_MODE=$mode BASE_PY=$BASE_PY）不同；不覆蓋，要換請先和負責人確認（$VENV/pyvenv.cfg）"
  fi
  if [ "$mode" = system-torch ]; then
    "$BASE_PY" -c "import sys,torch;assert torch.version.hip,'不是 ROCm 版 torch';assert torch.cuda.is_available(),'看不到 GPU';print('[run_vm] 基底',sys.prefix,'python',sys.version.split()[0],'torch',torch.__version__,'hip',torch.version.hip)" \
      || die "$BASE_PY 沒有可用 GPU 的 ROCm torch：改用 VENV_MODE=wheel BASE_PY=<python3.11>"
    [ -x $VENV/bin/python ] || "$BASE_PY" -m venv --system-site-packages $VENV
    # 鎖住基底的 torch/numpy：缺的套件只裝進 venv，不會升級或覆寫基底已有的版本
    "$BASE_PY" -c "import importlib.metadata as m;[print(f'{p}=={m.version(p)}') for p in ('torch','numpy')]" >$VENV/sys1-constraints.txt
    $VENV/bin/pip install -c $VENV/sys1-constraints.txt "transformers>=4.48" numpy scipy sentencepiece protobuf
  else
    [ -x $VENV/bin/python ] || "$BASE_PY" -m venv $VENV
    $VENV/bin/pip install "${TORCH_SPEC:-torch==2.8.0}" --index-url "${TORCH_INDEX:-https://download.pytorch.org/whl/rocm6.4}"
    $VENV/bin/pip install "transformers>=4.48" numpy scipy sentencepiece protobuf
  fi
  $VENV/bin/python - "$mode" "$BASE_PY" "$VENV/sys1-venv.json" <<'PYX'
import json, sys, time, torch, transformers
rec = {"mode": sys.argv[1], "base_python": sys.argv[2], "created": time.strftime("%F %T %z"), "python": sys.version.split()[0],
       "torch": torch.__version__, "torch_file": torch.__file__, "hip": getattr(torch.version, "hip", None),
       "gpu": torch.cuda.is_available(), "transformers": transformers.__version__}
p = sys.argv[3]
try: rec["created"] = json.load(open(p))["created"]
except Exception: pass
json.dump(rec, open(p, "w"), indent=1); print("[run_vm] venv", json.dumps(rec, ensure_ascii=False))
if not rec["gpu"]: print("[run_vm] 注意：venv 裡看不到 GPU（DEVICE=cpu 仍可訓練）")
PYX
  ;;
selftest)
  need_venv
  ( cd "$G" && "$PY" holdout.py | grep -q "FROZEN ✓" && echo "FROZEN ✓" )
  ( cd "$G" && bash selftest.sh | tail -1 && bash sys1/selftest.sh | tail -1 && "$PY" sys1/policy.py list )
  ( cd "$G" && "$PY" -m unittest discover -s sys1/train/tests 2>&1 | tail -3 )
  ( cd "$G" && PATH="$(dirname "$PY"):$PATH" bash sys1/train/selftest_train.sh | tail -1 )
  ( cd "$S" && bash selftest.sh | tail -1 )
  ( cd "$G" && "$PY" sys1/signoff.py verify )
  ;;
data)
  need_venv; [ -e "$DATA/train.jsonl" ] && die "$DATA 已存在（不覆蓋；要重做請換版本號並在報告記錄）"
  ( cd "$G" && "$PY" -I sys1/train/make_train_data.py --out "$DATA" ) ;;
train)
  need_venv; id="${2:?id}"; shift 2 || true; read -r hf trc <<<"$(base_of "$id")"; [ -n "$hf" ] || die "$id 不在 bases.json"
  seeds=("$@"); [ ${#seeds[@]} -gt 0 ] || mapfile -t seeds < <("$PY" -c "import json;[print(s) for s in json.load(open('$G/sys1/train/budget.json'))['seeds']]")
  [ -e "$DATA/SHA256SUMS" ] || die "先跑 ./run_vm.sh data"
  ( cd "$G" && "$PY" sys1/signoff.py verify ) || die "budget.json／gates.json 與 S0 簽核不符（sys1/SIGNOFF.json）：不訓練"
  if [ "$DEVICE" != cpu ]; then
    free=$(free_vram_gb)
    [ "$free" -ge "${MIN_FREE_VRAM_GB:-24}" ] || die "GPU 剩餘顯存 ${free}GB < ${MIN_FREE_VRAM_GB:-24}GB：不在 GPU 上訓練（會影響 TAIDE；handoff §8.8）。改 DEVICE=cpu 或先問負責人"
  fi
  for s in "${seeds[@]}"; do
    out="$CK/$id-s$s"; [ -e "$out/meta.json" ] && { echo "[run_vm] $out 已存在，略過"; continue; }
    ( cd "$G" && "$PY" -I sys1/train/train_head.py --base "$hf" --data "$DATA" --seed "$s" --out "$out" --device "$DEVICE" $( [ "$trc" = 1 ] && echo --trust-remote-code ) )
  done ;;
serve)
  need_venv; id="${2:?id}"; s="${3:?seed}"; port="${4:-8232}"
  ( cd "$G" && exec "$PY" -I sys1/train/serve_sys1.py --ckpt "$CK/$id-s$s" --name "$id" --port "$port" --device "$DEVICE" ) ;;
contract)
  need_venv; id="${2:?id}"; s="${3:?seed}"; port="${4:-8232}"
  sys1_start "$id" "$s" "$port"
  rc=0; ( cd "$G" && "$PY" -I sys1/train/contract_check.py --url "http://127.0.0.1:$port" --name "$id" ) || rc=$?
  exit $rc ;;
s2)
  need_venv; id="${2:?id}"; s="${3:?seed}"; port="${4:-8232}"; R="$RUNS/$id-s$s"
  ( cd "$G" && "$PY" sys1/policy.py check "$id" ) || die "policy 拒絕 $id（負責人尚未簽核來源）；只能跑 ./run_vm.sh contract"
  ( cd "$G" && "$PY" sys1/signoff.py verify ) || die "budget.json／gates.json 與 S0 簽核不符（sys1/SIGNOFF.json）：不評比"
  sys1_start "$id" "$s" "$port"
  s2_collect "$id" "$port" "$R" ;;
laya-ctx)
  need_venv; ( cd "$G" && "$PY" sys1/laya_ctx.py --tokenizer jhu-clsp/mmBERT-base --ctx 1024; "$PY" sys1/laya_ctx.py --tokenizer answerdotai/ModernBERT-large --ctx 512 ) ;;
laya-venv)
  base_guard
  if [ -e $LAYA_VENV/pyvenv.cfg ]; then
    grep -qiE "^include-system-site-packages *= *true" $LAYA_VENV/pyvenv.cfg && grep -qE "^home *= *$(dirname "$BASE_PY")/?$" $LAYA_VENV/pyvenv.cfg \
      || die "$LAYA_VENV 已存在但基底不同；不覆蓋"
  fi
  [ -x $LAYA_VENV/bin/python ] || "$BASE_PY" -m venv --system-site-packages $LAYA_VENV
  # 鎖在基底版本：Laya 和 Sys1 候選用同一套 torch／transformers（laya 要求 torch>=2.0、transformers>=4.48），比較才公平
  "$BASE_PY" -c "import importlib.metadata as m;[print(f'{p}=={m.version(p)}') for p in ('torch','numpy','transformers')]" >$LAYA_VENV/laya-constraints.txt
  $LAYA_VENV/bin/pip install -c $LAYA_VENV/laya-constraints.txt "${LAYA_SPEC:-laya[serve]==0.4.21}"
  $LAYA_VENV/bin/python - "$LAYA_VENV/laya-venv.json" <<'PYX'
import importlib.metadata as m, json, sys, time, torch
rec = {"created": time.strftime("%F %T %z"), "python": sys.version.split()[0], "torch": torch.__version__, "hip": getattr(torch.version, "hip", None),
       "gpu": torch.cuda.is_available(), **{p: m.version(p) for p in ("laya", "transformers", "fastapi", "uvicorn")}}
json.dump(rec, open(sys.argv[1], "w"), indent=1); print("[run_vm] laya venv", json.dumps(rec))
PYX
  ;;
laya-serve)
  need_laya; port="${2:-8231}"; port_ok "$port"; laya_run "$port" exec ;;
laya-contract)
  need_venv; need_laya; id="${2:?id（laya-mmbert｜laya-typed）}"; port="${3:-8231}"; port_ok "$port"
  case "$id" in laya-*) ;; *) die "laya-contract 只接受 laya-*";; esac
  name=$("$PY" -c "import json,sys;print({c['id']:c for c in json.load(open(sys.argv[2],encoding='utf-8'))['candidates']}[sys.argv[1]]['model_field'])" "$id" "$G/sys1/registry.json")
  laya_start "$port"
  ( cd "$G" && "$PY" -I sys1/train/contract_check.py --url "http://127.0.0.1:$port" --name "$name" ) ;;
laya-s2)
  need_venv; need_laya; id="${2:?id（laya-mmbert｜laya-typed）}"; port="${3:-8231}"; port_ok "$port"
  case "$id" in laya-*) ;; *) die "laya-s2 只接受 laya-*";; esac
  ( cd "$G" && "$PY" sys1/policy.py check "$id" ) || die "policy 拒絕 $id"
  ( cd "$G" && "$PY" sys1/signoff.py verify ) || die "budget.json／gates.json 與 S0 簽核不符：不評比"
  echo "[run_vm] $id：來源未查證（owner override，2026-11-03）；報告要照這樣標示"
  laya_start "$port"; s2_collect "$id" "$port" "$RUNS/$id" ;;
kev-venv)
  # Kev 1.0 程式（jaredpalmer/kev@1f04cfa2）要求 Python>=3.12、torch>=2.6,<2.9、transformers>=5.17：映像的 py_3.10 跑不了。
  # 用 uv 裝獨立的 Python 3.12；torch 用 ROCm wheel（不是 uv.lock 的 CUDA wheel）。Kev 是 Qwen 基座，只當對照。
  base_guard
  if [ "$(git -C "$KEV_DIR" rev-parse HEAD 2>/dev/null || true)" != "$KEV_COMMIT" ]; then
    [ ! -e "$KEV_DIR" ] || [ -z "$(ls -A "$KEV_DIR" 2>/dev/null)" ] || die "$KEV_DIR 存在但不是 kev@${KEV_COMMIT:0:8}；不覆蓋"
    git init -q "$KEV_DIR" && git -C "$KEV_DIR" remote add origin https://github.com/jaredpalmer/kev
    GIT_LFS_SKIP_SMUDGE=1 git -C "$KEV_DIR" fetch -q --depth 1 origin "$KEV_COMMIT"
    GIT_LFS_SKIP_SMUDGE=1 git -C "$KEV_DIR" checkout -q FETCH_HEAD
  fi
  UVT=/opt/uv-tool; [ -x $UVT/bin/uv ] || { "$BASE_PY" -m venv $UVT && $UVT/bin/pip install -q "uv==${UV_VERSION:-0.9.5}"; }
  export UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_CACHE_DIR="${UV_CACHE_DIR:-/opt/uv-cache}"
  KP="$KEV_DIR/.venv/bin/python"
  [ -x "$KP" ] || { $UVT/bin/uv python install 3.12 && $UVT/bin/uv venv --python 3.12 "$KEV_DIR/.venv"; }
  "$KP" -c "import torch;assert torch.version.hip" 2>/dev/null || \
    $UVT/bin/uv pip install --python "$KP" "${KEV_TORCH_SPEC:-torch==2.8.0}" --index-url "${KEV_TORCH_INDEX:-https://download.pytorch.org/whl/rocm6.4}"
  # 鎖住剛裝好的 ROCm torch；其他照 Kev 的 uv.lock（transformers 5.17.0、accelerate 1.15.0）
  { "$KP" -c "import importlib.metadata as m;print('torch=='+m.version('torch'))"; echo transformers==5.17.0; echo accelerate==1.15.0; } >"$KEV_DIR/.venv/kev-constraints.txt"
  $UVT/bin/uv pip install --python "$KP" -c "$KEV_DIR/.venv/kev-constraints.txt" -e "$KEV_DIR[serve]"
  "$KP" - "$KEV_DIR/.venv/kev-venv.json" "$KEV_COMMIT" <<'PYX'
import importlib.metadata as m, json, sys, time, torch
rec = {"created": time.strftime("%F %T %z"), "kev_commit": sys.argv[2], "python": sys.version.split()[0], "torch": torch.__version__,
       "hip": getattr(torch.version, "hip", None), "gpu": torch.cuda.is_available(), **{p: m.version(p) for p in ("transformers", "accelerate", "fastapi", "uvicorn")}}
json.dump(rec, open(sys.argv[1], "w"), indent=1); print("[run_vm] kev venv", json.dumps(rec))
assert rec["hip"] and rec["gpu"], "Kev venv 的 torch 不是可用 GPU 的 ROCm 版"
PYX
  ;;
kev-gateb)
  # = Colab GateB_PK_vs_Gemma.ipynb 的 P+K 部分（servers.py kev start → gateb kev-collect → latency → stop → analyze），
  # 再補 router_gold hold-out（用凍結門檻 holdout/frozen_thresholds.json 的 by_kev[<size>]，不調參）。Gemma 臂不在這裡（要 llama.cpp 的 HIP 版）。
  #   KEV_SIZE  0.8b（預設）｜4b：jaredpalmer/kev-<size>@v1.0（Qwen3.5 基座＋LoRA）；凍結門檻只有這兩個大小
  #   KEV_DTYPE bf16（預設，Kev 服務的精度）｜fp32（= Colab 收集與凍結門檻的精度）
  need_venv; [ -x "$KEV_DIR/.venv/bin/python" ] || die "先 ./run_vm.sh kev-venv"
  size="${KEV_SIZE:-0.8b}"; dtype="${KEV_DTYPE:-bf16}"
  case "$size" in 0.8b) need=8;; 4b) need=24;; *) die "KEV_SIZE 只接受 0.8b、4b（holdout/frozen_thresholds.json 只有這兩個）";; esac
  case "$dtype" in bf16|fp32) ;; *) die "KEV_DTYPE 只接受 bf16、fp32";; esac
  free=$(free_vram_gb)      # kev-4b model card：bf16 常駐約 14.3 GB；fp32 約兩倍
  [ "$free" -ge "${MIN_FREE_VRAM_GB:-$need}" ] || die "GPU 剩餘顯存 ${free}GB < ${MIN_FREE_VRAM_GB:-$need}GB：不啟動 Kev-$size（會影響 TAIDE）。讀不到 rocm-smi 時會是 0；用 rocm-smi 確認後可設 MIN_FREE_VRAM_GB"
  RUN="${2:-$RUNS/kev-gateb-$(date +%Y%m%d)-$size-$dtype}"
  export GATEB_STATE="$RUNS/.gateb_state"; mkdir -p "$RUNS"; cd "$G"
  "$PY" servers.py kev start --kev-dir "$KEV_DIR" --model "jaredpalmer/kev-$size@v1.0" --dtype "$dtype" --kev-port 8230
  trap '"$PY" "$G/servers.py" kev stop || true' EXIT
  # gateb 的 gpu_facts 只認 nvidia-smi（kev-collect 還會把 gpu 覆寫成 null），所以 GPU 名稱另存在 rocm_gpu
  pn=$(rocm-smi --showproductname 2>/dev/null || true)   # 先取 Card Series（產品名），沒有才退到 Card Model；SKU（如 M3000100）不是產品名
  gpu=$( { printf '%s
' "$pn" | grep -m1 -ioE "card series:.*" || printf '%s
' "$pn" | grep -m1 -ioE "card model:.*"; } | cut -d: -f2- | xargs || true)
  for ds in gate_data router_gold; do
    r=$( [ "$ds" = gate_data ] && echo "$RUN" || echo "$RUN-gold" )
    "$PY" gateb.py --run "$r" --dataset "$ds" --set "host=$(hostname)" --set "rocm_gpu=${gpu:-UNKNOWN}" --set "kev_size=$size" \
      --set "kev_model=jaredpalmer/kev-$size@v1.0" --set "kev_dtype=$dtype" --set "kev_commit=$KEV_COMMIT" env >/dev/null
  done
  "$PY" gateb.py --run "$RUN" --kev-label "$size" kev-collect
  "$PY" gateb.py --run "$RUN" --kev-label "$size" latency --target kev --levels 1,2,4
  echo "[run_vm] 下一步收集 router_gold hold-out（Kev 是對照，門檻已凍結，不調參）。10 秒內 Ctrl-C 可取消。"; sleep 10
  "$PY" gateb.py --run "$RUN-gold" --dataset router_gold --kev-label "$size" kev-collect
  echo "[run_vm] → $RUN/SUMMARY.md（gate_data test＋延遲階梯）· $RUN-gold/HOLDOUT_SUMMARY.md（hold-out）"
  echo "[run_vm] 報告標題的「Colab」「T4」「GPU None」是凍結的 gateb.py 寫死的；實際硬體與設定見 $RUN/meta.json 的 env" ;;
*) sed -n 2,23p "$0"; exit 2 ;;
esac

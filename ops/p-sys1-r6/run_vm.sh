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
# 環境變數：PY（預設 /opt/venv-sys1/bin/python；venv 不存在時只有 where 會退回 PATH 的 python3）· DEVICE（預設 cpu）· MIN_FREE_VRAM_GB（預設 24）
#   venv：VENV_MODE=system-torch（預設）＋ BASE_PY（預設 /opt/conda/envs/py_3.10/bin/python）——10/20 重建後映像內建、
#           這台 VM 上唯一驗證過能用 GPU 的 torch 2.6.0 ROCm；venv 用 --system-site-packages 唯讀引用基底，不改基底。
#         VENV_MODE=wheel ＋ BASE_PY=<python3.11>（例如 /opt/conda/envs/blink-backend/bin/python3.11）：獨立 venv，pip 安裝
#           TORCH_SPEC / TORCH_INDEX（預設 torch==2.8.0 / rocm6.4；這個組合在重建後的容器上沒有驗證過）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; G="$HERE/gateB-colab"; S="$HERE/shadow-sys1"
VENV=/opt/venv-sys1; PY_SET="${PY:+1}"; PY="${PY:-$( [ -x $VENV/bin/python ] && echo $VENV/bin/python || echo python3 )}"
BASE_PY="${BASE_PY:-/opt/conda/envs/py_3.10/bin/python}"
DEVICE="${DEVICE:-cpu}"; DATA="$G/sys1/train/data/v1"; CK="$HERE/ckpt"
die() { echo "[run_vm] $*" >&2; exit 1; }
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
    free=$(rocm-smi --showmeminfo vram --json 2>/dev/null | "$PY" -c "import json,sys;d=json.load(sys.stdin);c=next(iter(d.values()));t=int(c['VRAM Total Memory (B)']);u=int(c['VRAM Total Used Memory (B)']);print((t-u)//2**30)" || echo 0)
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
  ( cd "$G" && "$PY" -I sys1/train/serve_sys1.py --ckpt "$CK/$id-s$s" --name "$id" --port "$port" --device "$DEVICE" >"$CK/$id-s$s/serve.log" 2>&1 & echo $! >"$CK/.serve.pid" )
  for i in $(seq 1 240); do curl -sf "127.0.0.1:$port/v1/models" >/dev/null && break; sleep 0.5; done
  ( cd "$G" && "$PY" -I sys1/train/contract_check.py --url "http://127.0.0.1:$port" --name "$id" ); rc=$?
  kill "$(cat "$CK/.serve.pid")" 2>/dev/null || true; exit $rc ;;
s2)
  need_venv; id="${2:?id}"; s="${3:?seed}"; port="${4:-8232}"; R="$HERE/runs/$id-s$s"
  ( cd "$G" && "$PY" sys1/policy.py check "$id" ) || die "policy 拒絕 $id（負責人尚未簽核來源）；只能跑 ./run_vm.sh contract"
  ( cd "$G" && "$PY" sys1/signoff.py verify ) || die "budget.json／gates.json 與 S0 簽核不符（sys1/SIGNOFF.json）：不評比"
  ( cd "$G" && "$PY" -I sys1/train/serve_sys1.py --ckpt "$CK/$id-s$s" --name "$id" --port "$port" --device "$DEVICE" >"$CK/$id-s$s/serve.log" 2>&1 & echo $! >"$CK/.serve.pid" )
  trap 'kill "$(cat "$CK/.serve.pid")" 2>/dev/null || true' EXIT
  for i in $(seq 1 240); do curl -sf "127.0.0.1:$port/v1/models" >/dev/null && break; sleep 0.5; done
  cd "$G"
  "$PY" sys1/run_cand.py probe   --cand "$id" --kev-url "http://127.0.0.1:$port"
  "$PY" sys1/run_cand.py collect --cand "$id" --run "$R/gd"   --split all --kev-url "http://127.0.0.1:$port" --vus 1
  echo "[run_vm] 下一步會第一次看 router_gold hold-out（之後不可再改門檻/題組/資料/超參數；handoff §4-2）。10 秒內 Ctrl-C 可取消。"; sleep 10
  "$PY" sys1/run_cand.py collect --cand "$id" --run "$R/gold" --dataset router_gold --kev-url "http://127.0.0.1:$port" --vus 1
  "$PY" sys1/analyze_cand.py --cand "$id" --cal-run "$R/gd" --hold-run "$R/gold" --kev-ref-run sys1/reference/gateB-t4-001 || true
  echo "[run_vm] → $R/gold/sys1_$id/SYS1_HOLDOUT.md（router_gold 已被看過：結果只能拿來淘汰）" ;;
laya-ctx)
  need_venv; ( cd "$G" && "$PY" sys1/laya_ctx.py --tokenizer jhu-clsp/mmBERT-base --ctx 1024; "$PY" sys1/laya_ctx.py --tokenizer answerdotai/ModernBERT-large --ctx 512 ) ;;
*) sed -n 2,19p "$0"; exit 2 ;;
esac

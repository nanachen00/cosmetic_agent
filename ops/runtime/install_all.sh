#!/usr/bin/env bash
# MI300X model-host bootstrap. It never installs the Mac backend or databases.
#   tmux new -s restore
#   export HF_TOKEN=hf_xxx                    # TAIDE repos are gated
#   bash /var/lib/jenkins/tomod/ops/runtime/install_all.sh 2>&1 | tee /var/lib/jenkins/tomod/restore.log
# Model selection defaults only to BGE-M3. Historical TAIDE/vLLM setup requires
# ENABLE_TAIDE=1 and an explicit REQUESTED_MODELS selection for its weights.
# Run a single step with:
#   ENABLE_TAIDE=1 bash install_all.sh 40
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/runtime.conf"
[[ "$ENABLE_TAIDE" =~ ^[01]$ ]] || die "ENABLE_TAIDE 必須是 0 或 1"
ONLY="${1:-}"
[[ "$ONLY" != 40* || "$ENABLE_TAIDE" == "1" ]] || die "vLLM 安裝步驟需要明確指定 ENABLE_TAIDE=1"
chmod +x "$HERE"/*.sh "$HERE"/install/*.sh
"$HERE/verify-mi300x-host.sh"
"$HERE/verify-mi300x-mount.sh"

STEPS=(10_system_packages 30_embedding_env)
[[ "$ENABLE_TAIDE" == "1" ]] && STEPS+=(40_vllm_env)
STEPS+=(50_download_models)
for step in "${STEPS[@]}"; do
  [[ -n "$ONLY" && "$step" != "$ONLY"* ]] && continue
  bash "$HERE/install/$step.sh"
done

log "MI300X 模型主機安裝完成。下一步："
cat <<EOF
  1. 只下載額外模型時明確指定，例如：REQUESTED_MODELS=reranker bash $HERE/install/50_download_models.sh
  2. 啟動模型：BLINK_RUNTIME_ROLE=mi300x bash $HERE/start-production.sh
  3. Mac 端先執行 verify-mi300x-ssh.sh，再執行 start-ssh-tunnel.sh
  4. Mac 的唯一 Backend/Auth 設定檔是 cosmetic-agent/.env
EOF

#!/usr/bin/env bash
# One-shot platform restore for the MI300X container. Run inside tmux:
#   tmux new -s restore
#   export HF_TOKEN=hf_xxx                    # TAIDE repos are gated
#   bash /var/lib/jenkins/tomod/ops/runtime/install_all.sh 2>&1 | tee /var/lib/jenkins/tomod/restore.log
# Each step is idempotent; re-running skips finished work. Run a single step with:
#   bash install_all.sh 40
set -Eeuo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
source "$HERE/runtime.conf"
chmod +x "$HERE"/*.sh "$HERE"/install/*.sh

STEPS=(10_system_packages 20_backend_env 30_embedding_env 38_fix_vllm_rocm_c 40_vllm_env 50_download_models 60_init_database)
ONLY="${1:-}"
for step in "${STEPS[@]}"; do
  [[ -n "$ONLY" && "$step" != "$ONLY"* ]] && continue
  bash "$HERE/install/$step.sh"
done

log "安裝完成。下一步："
cat <<EOF
  1. 填寫 $ROOT/cosmetic-agent/.env 中標示 [REQUIRED] 的項目
     （DATABASE_URL / REDIS_URL 已自動寫入；NEO4J、ZEP、OPENAI、OAuth、SMTP 需人工填）
  2. 若膚況標籤目錄尚未重建：bash $HERE/install/60_init_database.sh
  3. 啟動：bash $HERE/start-production.sh
  4. 驗收：bash $HERE/verify_platform.sh
  A/B 模型：ENABLE_AB_MODEL=1 bash $HERE/start-production.sh
EOF

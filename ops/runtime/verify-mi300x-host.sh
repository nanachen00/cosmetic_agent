#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/runtime.conf"

[[ "$(uname -s)" == "Linux" ]] || die "模型服務只能在 Linux MI300X 節點啟動"
[[ -e /dev/kfd ]] || die "找不到 /dev/kfd；目前環境未暴露 ROCm GPU"

gpu_info=""
if command -v amd-smi >/dev/null 2>&1; then
  # `list` can succeed while returning only BDF/UUID, without a GPU model.
  gpu_info="$(amd-smi static --asic 2>&1 || true)"
fi
if ! grep -Eqi 'MI300X|gfx942' <<<"$gpu_info" && command -v rocminfo >/dev/null 2>&1; then
  gpu_info="$(rocminfo 2>&1 || true)"
fi
if ! command -v amd-smi >/dev/null 2>&1 && ! command -v rocminfo >/dev/null 2>&1; then
  die "找不到 amd-smi 或 rocminfo，無法證明 SSH 目標是 MI300X 環境"
fi

grep -Eqi 'MI300X|gfx942' <<<"$gpu_info" \
  || die "GPU 資訊沒有 MI300X/gfx942；拒絕在未確認的 SSH 環境啟動"
printf '[OK] MI300X host=%s\n' "$(hostname -f 2>/dev/null || hostname)"
"$SCRIPT_DIR/verify-mi300x-mount.sh" --check-only

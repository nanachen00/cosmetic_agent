#!/usr/bin/env bash
# Prove that the explicit SSH target is a MI300X host with the persistent mount.
set -Eeuo pipefail

target="${MI300X_SSH_HOST:-}"
port="${MI300X_SSH_PORT:-22}"
[[ -n "$target" ]] || { echo '[ERROR] 必須明確設定 MI300X_SSH_HOST=user@ssh-host；不能用 Jupyter URL 推測' >&2; exit 2; }
[[ "$target" != *://* && "$target" != */* ]] \
  || { echo '[ERROR] MI300X_SSH_HOST 必須是 SSH host，不接受 Jupyter/HTTP URL' >&2; exit 2; }
[[ "$port" =~ ^[0-9]+$ ]] || { echo '[ERROR] MI300X_SSH_PORT 必須是數字' >&2; exit 2; }

ssh -T -p "$port" \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o StrictHostKeyChecking=yes \
  "$target" 'bash -se' <<'REMOTE'
set -Eeuo pipefail
[[ "$(uname -s)" == "Linux" ]] || { echo '[ERROR] not Linux' >&2; exit 2; }
[[ -e /dev/kfd ]] || { echo '[ERROR] /dev/kfd missing' >&2; exit 2; }
if command -v amd-smi >/dev/null 2>&1; then
  gpu_info="$(amd-smi static --asic 2>&1 || true)"
fi
if ! grep -Eqi 'MI300X|gfx942' <<<"${gpu_info:-}" && command -v rocminfo >/dev/null 2>&1; then
  gpu_info="$(rocminfo 2>&1 || true)"
fi
if ! command -v amd-smi >/dev/null 2>&1 && ! command -v rocminfo >/dev/null 2>&1; then
  echo '[ERROR] amd-smi/rocminfo missing' >&2
  exit 2
fi
grep -Eqi 'MI300X|gfx942' <<<"${gpu_info:-}" || { echo '[ERROR] target is not attested as MI300X' >&2; exit 2; }
[[ -d /mnt/goodagents ]] || { echo '[ERROR] /mnt/goodagents missing' >&2; exit 2; }
mountpoint -q /mnt/goodagents || { echo '[ERROR] /mnt/goodagents is not a mountpoint' >&2; exit 2; }
printf '[OK] ssh_host=%s gpu=MI300X mount=/mnt/goodagents\n' "$(hostname -f 2>/dev/null || hostname)"
REMOTE

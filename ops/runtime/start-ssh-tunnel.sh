#!/usr/bin/env bash
# Forward only loopback ports after the SSH target passes MI300X attestation.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Use the same feature flags as the backend; never print credentials.
if [[ -f "$PROJECT_ROOT/cosmetic-agent/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/cosmetic-agent/.env"
  set +a
fi
[[ "${ENABLE_TAIDE:-0}" =~ ^[01]$ ]] || { echo '[ERROR] ENABLE_TAIDE must be 0 or 1' >&2; exit 2; }
"$SCRIPT_DIR/verify-mi300x-ssh.sh"

target="$MI300X_SSH_HOST"
port="${MI300X_SSH_PORT:-22}"
local_taide="${TAIDE_LOCAL_PORT:-8000}"
local_embedding="${EMBEDDING_LOCAL_PORT:-8002}"
local_reranker="${RERANKER_LOCAL_PORT:-8006}"
local_photo="${PHOTO_MODEL_LOCAL_PORT:-8025}"

forward_args=(
  -L "127.0.0.1:$local_embedding:127.0.0.1:8002"
)
[[ "${ENABLE_TAIDE:-0}" == "1" ]] \
  && forward_args+=( -L "127.0.0.1:$local_taide:127.0.0.1:8000" )
[[ "${ENABLE_RERANKER:-0}" == "1" ]] \
  && forward_args+=( -L "127.0.0.1:$local_reranker:127.0.0.1:8006" )
[[ "${ENABLE_PHOTO_MODEL:-0}" == "1" ]] \
  && forward_args+=( -L "127.0.0.1:$local_photo:127.0.0.1:8025" )

ssh_args=(
  -N -T -p "$port"
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=20
  -o ServerAliveCountMax=3
  -o StrictHostKeyChecking=yes
  "${forward_args[@]}"
  "$target"
)
if [[ "${TUNNEL_BACKGROUND:-0}" == "1" ]]; then
  ssh -f "${ssh_args[@]}"
  printf '[OK] SSH tunnel started in background; model ports are local loopback only.\n'
else
  printf '[INFO] SSH tunnel running in foreground; Ctrl-C closes all forwards.\n'
  exec ssh "${ssh_args[@]}"
fi

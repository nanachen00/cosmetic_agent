#!/usr/bin/env bash
# Create a second, checksummed copy of one self-trained checkpoint.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/runtime.conf"
"$SCRIPT_DIR/verify-mi300x-host.sh"
"$SCRIPT_DIR/verify-mi300x-mount.sh"

source_path="${1:-}"
label="${2:-}"
[[ -n "$source_path" && -n "$label" ]] \
  || die "用法：backup-trained-checkpoint.sh <checkpoint-dir> <label>"
[[ "$label" =~ ^[A-Za-z0-9._-]+$ ]] || die "label 只能包含英數、點、底線與連字號"
source_path="$(cd "$source_path" 2>/dev/null && pwd)" || die "checkpoint 不存在：$source_path"
case "$source_path/" in
  "$CHECKPOINT_DIR"/*) ;;
  *) die "只允許備份 $CHECKPOINT_DIR 內的 checkpoint" ;;
esac
find "$source_path" -type f -name '*.safetensors' -print -quit | grep -q . \
  || die "checkpoint 沒有 safetensors；不建立空備份"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$CHECKPOINT_BACKUP_DIR/$label/$timestamp"
[[ ! -e "$destination" ]] || die "備份目標已存在：$destination"
mkdir -p "$(dirname "$destination")" "$destination"
rsync -a --checksum "$source_path/" "$destination/"
(
  cd "$destination"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS
)
printf '[OK] checkpoint backup=%s\n' "$destination"
printf '[NOTE] 此副本仍位於同一持久掛載；若要防掛載本身損毀，仍需校外／物件儲存備份。\n'

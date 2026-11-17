#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/runtime.conf"

[[ "$GOODAGENTS_ROOT" == "/mnt/goodagents" ]] \
  || die "GOODAGENTS_ROOT 必須是已確認的 /mnt/goodagents（目前：$GOODAGENTS_ROOT）"
[[ -d "$GOODAGENTS_ROOT" ]] \
  || die "$GOODAGENTS_ROOT 不存在；拒絕建立普通資料夾冒充持久掛載"
command -v mountpoint >/dev/null 2>&1 \
  || die "缺少 mountpoint，無法安全確認持久掛載"
mountpoint -q "$GOODAGENTS_ROOT" \
  || die "$GOODAGENTS_ROOT 不是獨立掛載點；停止啟動以避免把模型寫進容器層"

command -v findmnt >/dev/null 2>&1 \
  || die "缺少 findmnt，無法確認持久 NFS 來源；停止啟動"
actual_target="$(findmnt -n -o TARGET --target "$GOODAGENTS_ROOT" | head -n 1)"
[[ "$actual_target" == "$GOODAGENTS_ROOT" ]] \
  || die "findmnt 顯示的掛載目標不是 $GOODAGENTS_ROOT（實際：$actual_target）"
mount_source="$(findmnt -n -o SOURCE --target "$GOODAGENTS_ROOT" | head -n 1)"
mount_type="$(findmnt -n -o FSTYPE --target "$GOODAGENTS_ROOT" | head -n 1)"
mount_options="$(findmnt -n -o OPTIONS --target "$GOODAGENTS_ROOT" | head -n 1)"
[[ "$mount_type" == "nfs" || "$mount_type" == "nfs4" ]] \
  || die "掛載類型必須為 NFS（實際：$mount_type）"
[[ ",$mount_options," == *,rw,* ]] || die "持久 NFS 必須以 rw 掛載"
[[ "$mount_source" == "$GOODAGENTS_EXPECTED_SOURCE" ]] \
  || die "掛載來源不符：預期 $GOODAGENTS_EXPECTED_SOURCE，實際 $mount_source"
printf '[OK] persistent mount target=%s source=%s type=%s\n' \
  "$actual_target" "$mount_source" "$mount_type"

[[ -w "$GOODAGENTS_ROOT" ]] || die "$GOODAGENTS_ROOT 不可寫"

if [[ "${1:-}" != "--check-only" ]]; then
  mkdir -p \
    "$MODEL_DIR" \
    "$HF_HOME" \
    "$CHECKPOINT_DIR" \
    "$OUTPUT_DIR" \
    "$CHECKPOINT_BACKUP_DIR"
  printf '[OK] persistent directories prepared under %s\n' "$GOODAGENTS_ROOT"
fi

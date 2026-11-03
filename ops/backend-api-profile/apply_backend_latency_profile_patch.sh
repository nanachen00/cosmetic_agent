#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
PROFILE_DIR="${PROFILE_DIR:-$ROOT/ops/backend-api-profile}"
PATCH_DIR="${PATCH_DIR:-$PROFILE_DIR/patch}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_DIR="$PROFILE_DIR/backups/$STAMP"
LOG="$PROFILE_DIR/apply-$STAMP.log"

mkdir -p "$PROFILE_DIR" "$PATCH_DIR" "$BACKUP_DIR"
exec > >(tee -a "$LOG") 2>&1

declare -A TARGETS=(
  [observability.py]="$ROOT/cosmetic-agent/core/observability.py"
  [api_server.py]="$ROOT/cosmetic-agent/api/api_server.py"
  [orchestrator.py]="$ROOT/cosmetic-agent/agents/root/orchestrator.py"
)

# Exact hashes from the 2026-09-24 06:00 UTC source snapshot used to build
# the latency-profiling patch.  The script refuses to patch unknown source.
declare -A BASE_SHA=(
  [observability.py]="3465b06e9fb2388e92a00cbbaf9e20e83bb1534d42095de2095acb3ce2732cb0"
  [api_server.py]="20a3896df53110f78298eec6e036bcbb595b79e6d9f063a3405ce6c2299c40de"
  [orchestrator.py]="79723d74a583dade49b0ca3d73b71afefe2c045e822545c591121bc636212499"
)

declare -A PATCH_SHA=(
  [observability.py]="e39f1c75ad9e882ca00ecdf5dcaab74756f25e4f0f7ecb9f089aa74b89520b86"
  [api_server.py]="32b56de8f561eaa04931f6ee0a215ac11723b3acac4535df5f923e76a3f46302"
  [orchestrator.py]="b5d18e55a3de8e42163f5bf505639188eb499efde40cceddf04f81ace7793026"
)

sha() {
  sha256sum "$1" | awk '{print $1}'
}

echo "============================================================"
echo "BLINK backend latency-profiling patch apply"
echo "============================================================"
echo "ROOT=$ROOT"
echo "PATCH_DIR=$PATCH_DIR"
echo "BACKUP_DIR=$BACKUP_DIR"
echo

# ------------------------------------------------------------------
# Gate 1: every target and patch file must exist and have a known hash.
# No target is modified before this entire gate succeeds.
# ------------------------------------------------------------------
needs_apply=()

for name in observability.py api_server.py orchestrator.py; do
  target="${TARGETS[$name]}"
  patch="$PATCH_DIR/$name"

  [[ -f "$target" ]] || { echo "[ERROR] Missing target: $target"; exit 1; }
  [[ -f "$patch" ]] || { echo "[ERROR] Missing patch file: $patch"; exit 1; }

  target_sha="$(sha "$target")"
  patch_sha="$(sha "$patch")"

  echo "[$name]"
  echo "  target_sha=$target_sha"
  echo "  patch_sha =$patch_sha"

  if [[ "$patch_sha" != "${PATCH_SHA[$name]}" ]]; then
    echo "[ERROR] Patch file hash is not the reviewed artifact."
    exit 1
  fi

  if [[ "$target_sha" == "${PATCH_SHA[$name]}" ]]; then
    echo "  state=ALREADY_PATCHED"
  elif [[ "$target_sha" == "${BASE_SHA[$name]}" ]]; then
    echo "  state=READY_TO_PATCH"
    needs_apply+=("$name")
  else
    echo "[ERROR] Current server source differs from the reviewed baseline."
    echo "        Refusing to overwrite unknown source."
    exit 1
  fi
done

# ------------------------------------------------------------------
# Gate 2: syntax-only parse.  Do not import application modules.
# ------------------------------------------------------------------
python3 - "$PATCH_DIR" <<'PY'
from pathlib import Path
import ast
import sys

root = Path(sys.argv[1])
for name in ("observability.py", "api_server.py", "orchestrator.py"):
    path = root / name
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"[OK] AST parse: {name}")
PY

# ------------------------------------------------------------------
# Gate 3: preserve exact pre-change copies and audited diffs.
# ------------------------------------------------------------------
for name in observability.py api_server.py orchestrator.py; do
  target="${TARGETS[$name]}"
  cp -a -- "$target" "$BACKUP_DIR/$name.before"

  {
    echo "===== $name ====="
    diff -u -- "$target" "$PATCH_DIR/$name" || true
    echo
  } >> "$BACKUP_DIR/reviewed.diff"
done

if ((${#needs_apply[@]} == 0)); then
  echo
  echo "[OK] All three targets already match the reviewed profiling patch."
  exit 0
fi

# ------------------------------------------------------------------
# Apply each file atomically on the same filesystem.
# ------------------------------------------------------------------
for name in "${needs_apply[@]}"; do
  target="${TARGETS[$name]}"
  patch="$PATCH_DIR/$name"
  tmp="${target}.latency-profile-new.$$"

  cp -- "$patch" "$tmp"
  chown --reference="$target" "$tmp"
  chmod --reference="$target" "$tmp"
  mv -fT -- "$tmp" "$target"

  actual="$(sha "$target")"
  if [[ "$actual" != "${PATCH_SHA[$name]}" ]]; then
    echo "[ERROR] Post-write hash mismatch: $target"
    exit 1
  fi
  echo "[APPLIED] $target"
done

echo
echo "============================================================"
echo "POST-APPLY VERIFICATION"
echo "============================================================"

for name in observability.py api_server.py orchestrator.py; do
  target="${TARGETS[$name]}"
  actual="$(sha "$target")"
  printf '[OK] %-18s %s\n' "$name" "$actual"
done

echo
echo "No service was restarted by this script."
echo "No provider/routing/prompt/response/DB/queue configuration is changed."
echo "Backup: $BACKUP_DIR"
echo "Log:    $LOG"

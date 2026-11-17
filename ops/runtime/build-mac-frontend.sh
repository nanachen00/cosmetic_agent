#!/usr/bin/env bash
# Use the same local API routing at build and runtime.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FRONTEND_ROOT="$PROJECT_ROOT/frontend"
# An explicit shell selection supports building a candidate before cutover.
# Otherwise read only the selected value in a subshell, preserving all other
# caller variables and keeping backend credentials out of the build environment.
if [[ -z "${NEXT_DIST_DIR+x}" && -f "$PROJECT_ROOT/cosmetic-agent/.env" ]]; then
  NEXT_DIST_DIR="$(
    source "$PROJECT_ROOT/cosmetic-agent/.env"
    printf '%s' "${NEXT_DIST_DIR:-.next}"
  )"
fi
cd "$FRONTEND_ROOT"
export NEXT_DIST_DIR="${NEXT_DIST_DIR:-.next}"
[[ "$NEXT_DIST_DIR" =~ ^\.next(-[A-Za-z0-9][A-Za-z0-9_-]*)?$ ]] \
  || { echo '[ERROR] NEXT_DIST_DIR must be .next or a single .next-safe-name' >&2; exit 2; }
[[ ! -L "$NEXT_DIST_DIR" ]] \
  || { echo '[ERROR] NEXT_DIST_DIR must not be a symlink' >&2; exit 2; }
export LOCAL_BACKEND_OVERRIDE=1
export BACKEND_API_URL=http://127.0.0.1:8010
export MEMBER_AUTH_API_BASE_URL="$BACKEND_API_URL"
export NEXT_PUBLIC_LOCAL_API_BASE_URL=""

# Next writes the active distDir into whichever tsconfig it uses. Candidate
# builds are intentionally dated, so using the editor config would accumulate
# dead include paths and make later type checks inspect every retained build.
build_tsconfig="$(mktemp "$FRONTEND_ROOT/.tsconfig-build.XXXXXX")"
next_env_snapshot="$(mktemp "${TMPDIR:-/tmp}/blink-next-env.XXXXXX")"
next_env_existed=0
if [[ -f "$FRONTEND_ROOT/next-env.d.ts" ]]; then
  cp "$FRONTEND_ROOT/next-env.d.ts" "$next_env_snapshot"
  next_env_existed=1
fi
cleanup_build_tsconfig() {
  if [[ "$next_env_existed" == "1" ]]; then
    cp "$next_env_snapshot" "$FRONTEND_ROOT/next-env.d.ts"
  else
    rm -f -- "$FRONTEND_ROOT/next-env.d.ts"
  fi
  rm -f -- "$build_tsconfig"
  rm -f -- "$next_env_snapshot"
}
trap cleanup_build_tsconfig EXIT
cp "$FRONTEND_ROOT/tsconfig.json" "$build_tsconfig"
export NEXT_TSCONFIG_PATH="${build_tsconfig##*/}"
npm run build
node --input-type=module - "$NEXT_DIST_DIR/routes-manifest.json" <<'JS'
import fs from 'node:fs';
const manifest = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const rewrites = Array.isArray(manifest.rewrites) ? manifest.rewrites : Object.values(manifest.rewrites).flat();
if (!rewrites.some(r => r.source === '/api/:path*' && r.destination === 'http://127.0.0.1:8010/api/:path*')) {
  throw new Error('Built frontend does not route to the Mac backend');
}
console.log('[OK] frontend build routes to Mac backend');
JS

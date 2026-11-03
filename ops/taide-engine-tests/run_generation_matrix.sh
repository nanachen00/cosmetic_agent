#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${OUT:-$HERE/results-$(date -u +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"

declare -A ENGINES=(
  [blue]="http://127.0.0.1:8000"
  [sglang]="http://127.0.0.1:8100"
  [vllm]="http://127.0.0.1:8200"
)

for name in blue sglang vllm; do
  url="${ENGINES[$name]}"
  port="${url##*:}"
  if ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .; then
    echo "===== TEST $name $url ====="
    python3 "$HERE/test_openai_generate.py" \
      --url "$url" \
      --output "$OUT/$name.json" || true
  else
    echo "[SKIP] $name $url is not listening"
  fi
done

echo
echo "Reports: $OUT"

#!/usr/bin/env bash
# Gate B selftest — mocks only (rule-based fake Kev on :8239, fake llama-server on :8359). No GPU, no real model, no network.
# Checks: collection, interrupt + resume, torn-line recovery, offline analysis, latency ladder, port guards, quant-label guard.
set -u   # no pipefail: guard checks grep the output of commands that are SUPPOSED to exit non-zero
cd "$(dirname "$0")"
T=$(mktemp -d); R="$T/run"; PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; rm -rf "$T"; }
trap cleanup EXIT
fail() { echo "[selftest] FAIL: $*"; exit 1; }
for port in 8239 8359; do
  if (exec 3<>/dev/tcp/127.0.0.1/$port) 2>/dev/null; then echo "[selftest] :$port busy — skipping (stop whatever runs there)"; exit 0; fi
done
python3 -I vendor_fk/tests/mock_kev.py --port 8239 --per-q-ms 1 --err-rate 0.05 & PIDS+=($!)
python3 -I tests/mock_llama.py --port 8359 --ms-per-tok 0.2 --model-path /m/gemma-4-E2B-it-Q8_0.gguf --leak-thinking & LL=$!; PIDS+=($LL)
sleep 1
G="python3 -I gateb.py --run $R --kev-url http://127.0.0.1:8239 --llm-url http://127.0.0.1:8359"

echo "[1/8] python syntax";      python3 -I -m py_compile gateb.py gemma_arms.py llmclient.py servers.py fetch_gguf.py prod_replay.py holdout.py tests/mock_llama.py || fail compile
echo "[2/8] kev-collect";        $G kev-collect --no-analyze >/dev/null 2>&1 || fail kev-collect
echo "[3/8] gemma interrupt";    timeout -s TERM 2 $G gemma-collect --quant q8_0 --arms min_json,min_prompt --no-analyze >/dev/null 2>&1
grep -q INTERRUPTED "$R/STATUS.txt" || fail "no INTERRUPTED status after SIGTERM"
printf '{"id": "torn' >> "$R/gemma/q8_0/min_json.jsonl"
echo "[4/8] gemma resume";       $G gemma-collect --quant q8_0 --arms min_json,min_prompt,legacy_json,prod_json --no-analyze >/dev/null 2>&1 || fail resume
$G status | grep -q "min_json: test 101/101" || { $G status; fail "resume incomplete"; }
grep -q '"reasoning": "Thinking' "$R/gemma/q8_0/min_json.jsonl" || fail "leak not recorded"
kill $LL; sleep 0.5
python3 -I tests/mock_llama.py --port 8359 --ms-per-tok 0.2 --model-path /m/gemma-4-E2B-it-Q8_0.gguf & PIDS+=($!); sleep 1
$G gemma-collect --quant q8_0 --arms min_json --redo-thinking --no-analyze > "$T/redo.log" 2>&1; grep -q "leaked thinking" "$T/redo.log" || fail "redo-thinking"
grep -q '"reasoning": "Thinking' "$R/gemma/q8_0/min_json.jsonl" && fail "leaked rows still present"
[ -f "$R/gemma/q8_0/min_json.jsonl.thinking-leak.bak" ] || fail "no leak backup"
$G status | grep -q "min_json: test 101/101" || { $G status; tail -3 "$R/STATUS.txt"; fail "redo incomplete"; }
echo "[5/8] latency";            $G latency --target kev --levels 1,2 --n 8 --no-analyze >/dev/null 2>&1 || fail latency-kev
$G latency --target gemma --quant q8_0 --arms min_json --levels 1,2 --n 8 --no-analyze >/dev/null 2>&1 || fail latency-gemma
echo "[6/8] analyze";            $G analyze >/dev/null 2>&1 || fail analyze
grep -q "COMPLETE" "$R/SUMMARY.md" && grep -q "P+K" "$R/SUMMARY.md" && grep -q "Gemma q8_0 min_json" "$R/SUMMARY.md" || fail "summary content"
python3 -I -c "import json,sys; m=json.load(open('$R/metrics.json')); assert len(m['arms'])==8, len(m['arms']); assert m['thresholds_v2'] and len(m['safety_supplement'])>=3, m['safety_supplement'].keys()" || fail metrics
grep -q "P+K v2" "$R/SUMMARY.md" && grep -q "9b. 安全補充集" "$R/SUMMARY.md" || fail "v2 / supplement missing in summary"
grep -q "9a. Production 現行路徑" "$R/SUMMARY.md" || fail "production path missing"
(cd vendor_prod && sha256sum -c SHA256SUMS --quiet) || fail "vendor_prod checksum"
echo "[7/8] hold-out (router_gold)"
H="python3 -I gateb.py --dataset router_gold --run $T/ho --kev-url http://127.0.0.1:8239 --llm-url http://127.0.0.1:8359"
$H kev-collect --no-analyze >/dev/null 2>&1 || fail "holdout kev"
$H gemma-collect --quant q8_0 --arms min_json,prod_json --no-analyze > "$T/ho.log" 2>&1 || fail "holdout gemma"
grep -q "prod_json: 0 cached, 300 to ask" "$T/ho.log" && fail "holdout prod_json not limited to LLM-path sentences"
$H analyze >/dev/null 2>&1 || fail "holdout analyze"
grep -q "FROZEN ✓" "$T/ho/HOLDOUT_SUMMARY.md" || fail "holdout not FROZEN (a frozen file changed — see holdout/FREEZE.json)"
grep -q "P+K v2・Kev-0.8B" "$T/ho/HOLDOUT_SUMMARY.md" && grep -q "Production 現行路徑" "$T/ho/HOLDOUT_SUMMARY.md" || fail "holdout summary content"
python3 -I gateb.py --run "$T/ho" status 2>&1 | grep -q "holds dataset" || fail "dataset mix-up not refused"
echo "[8/8] guards"
python3 -I gateb.py --run "$T/x" --llm-url http://127.0.0.1:8000 status 2>&1 | grep -q REFUSED || fail "llm :8000 not refused"
python3 -I gateb.py --run "$T/x" --kev-url http://127.0.0.1:8010 status 2>&1 | grep -q REFUSED || fail "kev :8010 not refused"
python3 -I gateb.py --run "$T/x" --kev-url http://10.0.0.1:8230 status 2>&1 | grep -q REFUSED || fail "remote kev not refused"
[ ! -e "$T/x" ] || fail "run dir created before guard"
$G gemma-collect --quant q4_0 --arms min_json 2>&1 | grep -q "server has loaded" || fail "quant label guard"
python3 -I servers.py llama start --llama-port 8000 --gguf /dev/null 2>&1 | grep -q REFUSED || fail "servers port guard"
echo "[selftest] PASS (mock numbers are synthetic and meaningless)"

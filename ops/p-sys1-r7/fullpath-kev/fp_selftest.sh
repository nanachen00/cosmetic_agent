#!/usr/bin/env bash
# Mock-only tests. Touches only this folder (.selftest/) and 127.0.0.1:8211/8239 (mock servers it starts and stops itself).
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -m py_compile "$HERE"/*.py "$HERE"/payload/*.py "$HERE"/tests/*.py; bash -n "$HERE/fp.sh"
for p in 8211 8239; do (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && { echo "port $p busy — aborting selftest"; exit 1; } || true; done
T="$HERE/.selftest/$(date -u +%Y%m%d-%H%M%S)"; mkdir -p "$T"
echo "== [1/5] shadowpatch guards + overlay functional tests"
python3 -W ignore "$HERE/tests/test_shadowpatch.py" 2>&1 | tail -4
echo "== [2/5] load runner vs mock backend (+ SIGTERM, resume)"
GOLD="$HERE/../datasets/router_gold.json"; [[ -f "$GOLD" ]] || GOLD="$(ls "$HERE"/../*/datasets/router_gold.json 2>/dev/null | head -1)"
[[ -f "$GOLD" ]] || { echo "router_gold.json not found (install the vLLM bundle first)"; exit 1; }
echo '[{"name":"t","headers":{"Authorization":"Bearer TEST"}}]' > "$T/acct.json"
python3 - "$HERE/control/kev_dispatch.json" "$T/ctl.json" "$T/trace.jsonl" <<'PY'
import json,sys; d=json.load(open(sys.argv[1])); d.update(arm="off",trace=sys.argv[3],kev_url="http://127.0.0.1:8239"); json.dump(d,open(sys.argv[2],"w"))
PY
python3 "$HERE/tests/mock_kev.py" --port 8239 --delay-ms 10 & K=$!
python3 "$HERE/tests/mock_backend.py" --port 8211 --control "$T/ctl.json" 2>/dev/null & B=$!
trap 'kill $K $B 2>/dev/null || true' EXIT; sleep 1
LOAD=(python3 "$HERE/fp_load.py" --base-url http://127.0.0.1:8211 --accounts-file "$T/acct.json" --gold "$GOLD" --control "$T/ctl.json" --per-route 4)
"${LOAD[@]}" --levels 1,4 --turns 3 --out "$T/full" >/dev/null
test -s "$T/full/SUMMARY.md" && test -s "$T/full/per_vu.csv" && test -s "$T/full/turns.jsonl"
python3 - "$T/full" <<'PY'
import csv,json,sys; d=sys.argv[1]
r=list(csv.DictReader(open(d+"/levels.csv"))); assert {x["arm"] for x in r}=={"off","kev_route","kev_slots"}, r
off=[float(x["p50_s"]) for x in r if x["arm"]=="off"]; kev=[float(x["p50_s"]) for x in r if x["arm"]=="kev_route"]
assert all(float(x["kev_ms_p50"])>0 for x in r if x["arm"]!="off"), "kev timing missing"
assert len(list(csv.DictReader(open(d+"/per_vu.csv"))))==3*(1+4), "per-VU rows"
assert json.load(open(sys.argv[1]+"/../ctl.json"))["arm"]=="off", "control not restored to off"
print("  OK: 3 arms x 2 levels, per-VU rows, Kev time split present, control restored to off")
PY
"${LOAD[@]}" --levels 1,4,8 --turns 40 --out "$T/int" >/dev/null 2>&1 & L=$!
for _ in $(seq 1 100); do grep -q '"off|1"' "$T/int/done.json" 2>/dev/null && break; sleep 0.3; done
kill -TERM "$L"; set +e; wait "$L"; RC=$?; set -e
[[ "$RC" == 130 ]] || { echo "expected 130 got $RC"; exit 1; }
grep -q INTERRUPTED "$T/int/STATUS.txt" && test -s "$T/int/SUMMARY.md"
echo "  OK: SIGTERM -> exit 130, STATUS=INTERRUPTED, partial SUMMARY present"
"${LOAD[@]}" --levels 1,4,8 --turns 2 --out "$T/int" --resume > "$T/resume.log" 2>&1
grep -q "reuse off|1" "$T/resume.log" && grep -q COMPLETE "$T/resume.log" || { echo "resume failed"; cat "$T/resume.log"; exit 1; }
echo "  OK: resume reused finished unit and completed"
echo "== [3/5] port guards"
for u in http://127.0.0.1:8010 http://127.0.0.1:8000 http://127.0.0.1:8200; do
  if python3 "$HERE/fp_load.py" --base-url $u --accounts-file "$T/acct.json" --out "$T/g" >/dev/null 2>&1; then echo "guard failed for $u"; exit 1; fi
done; echo "  OK: 8000/8010/8200 refused as load targets"
echo "== [4/5] kevctl lifecycle on a fake Kev (port 8230 must be free)"
if (exec 3<>"/dev/tcp/127.0.0.1/8230") 2>/dev/null; then echo "  SKIPPED: :8230 is in use here (real Kev?) — not touching it"; else
  FR="$T/benchroot"; mkdir -p "$FR/.runtime/kev-py312/bin" "$FR/.models/kev-0.8b-9a45d25eb2ab" "$FR/vendor/kev"
  cp "$HERE/tests/fake_kev_python" "$FR/.runtime/kev-py312/bin/python"
  export FP_BUNDLE="$HERE"
  export BLINK_BENCH_ROOT="$FR"; KC=(python3 "$HERE/kevctl.py")
  has() { [[ "$1" == *"$2"* ]] || { echo "kevctl check failed: expected '$2' in: ${1:0:200}"; exit 1; }; }
  has "$("${KC[@]}" start)" READY; has "$("${KC[@]}" status)" OWNED_RUNNING
  has "$("${KC[@]}" probe)" '"route"'; has "$("${KC[@]}" probe --slots)" "ALL ANSWERED"
  if "${KC[@]}" start >/dev/null 2>&1; then echo "double start was not refused"; exit 1; fi
  rm -f "$HERE/.state/kev.json"                     # forget identity -> process becomes 'foreign'
  has "$("${KC[@]}" status || true)" FOREIGN_LISTENER
  if "${KC[@]}" stop >/dev/null 2>&1; then echo "stop of foreign listener was not refused"; exit 1; fi
  has "$("${KC[@]}" adopt)" ADOPTED; has "$("${KC[@]}" stop)" STOPPED; has "$("${KC[@]}" status || true)" STOPPED
  echo "  OK: start/ready/probe/slots-probe, double-start refused, foreign listener not killed, adopt -> stop"
fi
kill $K $B 2>/dev/null || true; sleep 1
echo "== [5/5] gate0 + gateA on mock Kev (:8239), guards, SIGTERM/resume"
python3 "$HERE/tests/mock_kev.py" --port 8239 --delay-ms 2 --err-rate 0.05 --workers 4 & K2=$!
trap 'kill $K2 2>/dev/null || true' EXIT; sleep 1
python3 "$HERE/gate0_probe.py" --base-url http://127.0.0.1:8239 --out "$T/g0" --reps 3 --levels 1,4 --requests-per-level 16 --facts-out "$T/KEV_FACTS.json" >/dev/null
grep -q "GATE 0:" "$T/g0/SUMMARY.md" && grep -q COMPLETE "$T/g0/STATUS.txt" && test -s "$T/KEV_FACTS.json"
grep -q "## GPU context" "$T/g0/SUMMARY.md"
echo "  OK: gate0 complete, KEV_FACTS.json written, GPU context section present"
python3 "$HERE/gateA.py" --base-url http://127.0.0.1:8239 --out "$T/ga" --facts "$T/KEV_FACTS.json" --latency-n 16 >/dev/null
for k in "## A1" "## A2" "## A3" "## A4" "## A5" "## A6" "## A7" "## A8"; do grep -q "$k" "$T/ga/SUMMARY.md" || { echo "gateA summary missing $k"; exit 1; }; done
python3 - "$T/ga" <<'PY'
import json,sys; d=json.load(open(sys.argv[1]+"/done.json"))
a1=d["A1"]["rows"]; v1,v2=[int(a1[k][i]) for k in [x for x in a1 if x.startswith("budget value correct")] for i in (0,1)]
assert v2>v1, ("parser v2 must beat v1 on budget", v1, v2)
assert d["A6"]["full_ok_final"]>d["A6"]["full_ok_parser"], "kev must improve end-to-end on the mock"
print(f"  OK: gateA all stages; budget v1 {v1} -> v2 {v2}; end-to-end {d['A6']['full_ok_parser']} -> {d['A6']['full_ok_final']}")
PY
python3 "$HERE/gateA.py" --base-url http://127.0.0.1:8239 --out "$T/ga2" --facts "$T/KEV_FACTS.json" --latency-n 16 >/dev/null 2>&1 & L=$!
for _ in $(seq 1 100); do [ -s "$T/ga2/kev_raw.jsonl" ] && break; sleep 0.1; done
kill -TERM "$L"; set +e; wait "$L"; RC=$?; set -e
[[ "$RC" == 130 || "$RC" == 0 ]] || { echo "gateA SIGTERM rc=$RC"; exit 1; }
if [[ "$RC" == 130 ]]; then grep -q INTERRUPTED "$T/ga2/STATUS.txt" && test -s "$T/ga2/SUMMARY.md"; fi
python3 "$HERE/gateA.py" --base-url http://127.0.0.1:8239 --out "$T/ga2" --facts "$T/KEV_FACTS.json" --latency-n 16 --resume >/dev/null
grep -q COMPLETE "$T/ga2/STATUS.txt"; echo "  OK: gateA SIGTERM (rc=$RC) -> partial SUMMARY, --resume completes"
for u in http://127.0.0.1:8000 http://127.0.0.1:8010 http://127.0.0.1:8200 http://127.0.0.1:8001 http://10.0.0.5:8230; do
  for g in gate0_probe gateA; do if python3 "$HERE/$g.py" --base-url $u --out "$T/gg" >/dev/null 2>&1; then echo "guard failed for $g $u"; exit 1; fi; done
done; echo "  OK: gate0/gateA refuse 8000/8001/8010/8200 and non-loopback targets"
echo "FP SELFTEST OK ($T)"

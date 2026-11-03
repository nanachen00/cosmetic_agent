#!/usr/bin/env bash
# fp.sh — single entry point for the full-path (Shadow :8210) Kev-dispatch experiment. Separate bundle from the vLLM suite.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH="${BLINK_BENCH_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
HARNESS="${HARNESS_DIR:-$BENCH/gate2/harness}"
PROD_SRC="${PROD_SRC:-/var/lib/jenkins/tomod/cosmetic-agent}"          # READ-ONLY use (anchor check) — never written
SP=(python3 "$HERE/shadowpatch.py")
SPEC_MAIN="$HERE/patches/kev-dispatch-v1.json"; SPEC_HOOK="$HERE/patches/kev-harness-hook-v1.json"
usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; cat <<'U'
commands:
  preflight                         read-only checks (ports, Kev state, production anchors still match) — writes nothing
  kev status|start|stop|restart|adopt|probe [--slots]|logs
  arm off|kev_route|kev_slots       switch the overlay arm at runtime (control/kev_dispatch.json); `off` = original LLM path
  patch-apply  --shadow-root DIR    apply Kev-dispatch overlay to a SHADOW copy (guards: not production, marked/allow-listed, no links)
  patch-revert --shadow-root DIR [--force] | patch-verify --shadow-root DIR | patch-status | revert-all
  hook-install | hook-uninstall | hook-status
                                    patch the (non-production) harness so every Gate cell copy gets the overlay automatically
  harness gate1|gate2 [args]        run the existing run.sh with the hook env; stops OUR Kev first (harness starts its own on :8230)
  run [0] [A] [all] [--resume] [--stop-kev] [-- extra gate args]
                                    one-shot: ensure the shadow Kev is up (starts it if stopped), run the chosen gates in order with progress bars,
                                    results in .runs/run-<ts>/{gate0,gateA}/SUMMARY.md.  `./fp.sh run all` = 0 then A.  --resume continues the latest run.
  gate0 [args]                      Kev capability/latency probe on the SHADOW Kev (127.0.0.1:8230); writes KEV_FACTS.json + .runs/gate0-*/SUMMARY.md
  gateA [args]                      module tests: Parser v1→v2, layer 1, Kev L1/L2/L3, error propagation, latency (shadow Kev only; --resume, --no-kev)
  load [fp_load.py args]            closed-loop VU load against a running Shadow backend (8210-8219)
  selftest                          mock-only tests (no GPU, no Kev model, no production access)
U
}
cmd="${1:-}"; [[ -n "$cmd" ]] && shift || { usage; exit 2; }
case "$cmd" in
  preflight)
    echo "== production anchors (read-only) =="; "${SP[@]}" anchors --spec "$SPEC_MAIN" --src "$PROD_SRC" || true
    echo "== harness anchors =="; "${SP[@]}" anchors --spec "$SPEC_HOOK" --src "$HARNESS" || true
    echo "== Kev =="; python3 "$HERE/kevctl.py" status || true
    echo "== listeners (info only) =="; for p in 8000 8010 8200 8203 8206 8210 8230; do (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && echo "  :$p open" || echo "  :$p closed"; done ;;
  kev)         exec python3 "$HERE/kevctl.py" "$@" ;;
  arm)         a="${1:?off|kev_route|kev_slots}"; [[ "$a" =~ ^(off|kev_route|kev_slots)$ ]] || { echo "bad arm" >&2; exit 2; }
               python3 - "$HERE/control/kev_dispatch.json" "$a" <<'PY'
import json,os,sys
p,a=sys.argv[1:]; d=json.load(open(p)); d["arm"]=a; t=p+".tmp"; open(t,"w").write(json.dumps(d,indent=1)); os.replace(t,p); print("arm =",a)
PY
               ;;
  patch-apply)  exec "${SP[@]}" apply  --spec "$SPEC_MAIN" "$@" ;;
  patch-revert) exec "${SP[@]}" revert --spec "$SPEC_MAIN" "$@" ;;
  patch-verify) exec "${SP[@]}" verify --spec "$SPEC_MAIN" "$@" ;;
  patch-status) exec "${SP[@]}" status "$@" ;;
  revert-all)   "${SP[@]}" revert-all "$@"; bash "$0" arm off ;;
  hook-install)   exec "${SP[@]}" apply  --spec "$SPEC_HOOK" --shadow-root "$HARNESS" ;;
  hook-uninstall) exec "${SP[@]}" revert --spec "$SPEC_HOOK" --shadow-root "$HARNESS" "$@" ;;
  hook-status)    exec "${SP[@]}" verify --spec "$SPEC_HOOK" --shadow-root "$HARNESS" ;;
  harness)
    [[ "${1:-}" =~ ^(gate1|gate2)$ ]] || { echo "usage: fp.sh harness gate1|gate2 --run-id ID [acks]" >&2; exit 2; }
    st="$(python3 "$HERE/kevctl.py" status | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])' || true)"
    case "$st" in OWNED_RUNNING|OWNED_STARTING_OR_HUNG) python3 "$HERE/kevctl.py" stop;; STOPPED) ;; *) echo "[REFUSED] port 8230 busy with a foreign process; run: fp.sh kev status / adopt" >&2; exit 4;; esac
    bash "$0" arm off
    export KEV_OVERLAY_SPEC="$SPEC_MAIN" KEV_OVERLAY_SHADOWPATCH="$HERE/shadowpatch.py" KEV_OVERLAY_CONTROL="$HERE/control/kev_dispatch.json" PROJECT_ROOT="$BENCH"
    "${SP[@]}" verify --spec "$SPEC_HOOK" --shadow-root "$HARNESS" >/dev/null 2>&1 || echo "[note] harness hook not installed: cell copies will NOT carry the overlay (fp.sh hook-install)" >&2
    exec "$BENCH/run.sh" "$@" ;;
  run)
    gates=(); resume=0; stopk=0; extra=()
    while [[ $# -gt 0 ]]; do case "$1" in
      0|gate0) gates+=(0);; A|a|gateA) gates+=(A);; all) gates=(0 A);; --resume) resume=1;; --stop-kev) stopk=1;; --) shift; extra=("$@"); break;;
      *) echo "usage: fp.sh run [0] [A] [all] [--resume] [--stop-kev] [-- extra args for the gates]" >&2; exit 2;; esac; shift; done
    [[ ${#gates[@]} -gt 0 ]] || gates=(0 A)
    if (exec 3<>/dev/tcp/127.0.0.1/8200) 2>/dev/null; then echo "[warn] :8200 is listening (vLLM?). If Kev shares its GPU, latency numbers will be perturbed — stop it first for clean Gate 0 results." >&2; fi
    st="$(python3 "$HERE/kevctl.py" status | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])' || true)"
    case "$st" in
      OWNED_RUNNING) echo "[kev] already running (owned)";;
      STOPPED) echo "[kev] starting shadow Kev on :8230 (up to 240 s)…"; python3 "$HERE/kevctl.py" start || exit $?;;
      *) echo "[REFUSED] Kev state is $st (port 8230 busy/foreign or hung). Run: ./fp.sh kev status" >&2; exit 4;;
    esac
    RD="$(ls -d "$HERE"/.runs/run-* 2>/dev/null | tail -1 || true)"
    if [[ $resume == 1 && -n "$RD" ]]; then echo "[run] resuming $RD"; else RD="$HERE/.runs/run-$(date +%Y%m%d-%H%M%S)"; fi
    mkdir -p "$RD"; rc=0; rflag=(); [[ $resume == 1 ]] && rflag=(--resume)
    for g in "${gates[@]}"; do
      echo; echo "################ Gate $g ################"
      if [[ $g == 0 ]]; then python3 "$HERE/gate0_probe.py" --out "$RD/gate0" "${rflag[@]}" "${extra[@]}" || { rc=$?; break; }
      else python3 "$HERE/gateA.py" --out "$RD/gateA" "${rflag[@]}" "${extra[@]}" || { rc=$?; break; }; fi
    done
    echo; echo "== results: $RD"; ls "$RD"/*/SUMMARY.md 2>/dev/null
    [[ $stopk == 1 ]] && python3 "$HERE/kevctl.py" stop
    [[ $rc == 130 ]] && echo "interrupted — continue with: ./fp.sh run ${gates[*]} --resume"
    exit $rc ;;
  gate0)       exec python3 "$HERE/gate0_probe.py" "$@" ;;
  gateA)       exec python3 "$HERE/gateA.py" "$@" ;;
  load)        exec python3 "$HERE/fp_load.py" "$@" ;;
  selftest)    exec bash "$HERE/fp_selftest.sh" ;;
  -h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac

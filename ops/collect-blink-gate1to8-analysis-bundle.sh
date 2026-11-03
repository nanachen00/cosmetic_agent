#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark"
RESULTS_ROOT="/var/lib/jenkins/tomod/ops/results"
RUN_ID="20260927-033801-657853"

if [[ ! -d "$ROOT" ]]; then
  echo "ERROR: workspace not found: $ROOT" >&2
  exit 1
fi

mkdir -p "$RESULTS_ROOT"

STAMP="$(date '+%Y%m%d-%H%M%S')"
STAGE="${RESULTS_ROOT}/.blink-intent-gate1to8-analysis-${STAMP}"
BUNDLE="${RESULTS_ROOT}/blink-intent-gate1to8-analysis-${STAMP}.tar.gz"

cleanup() {
  rm -rf "$STAGE"
}
trap cleanup EXIT

mkdir -p "$STAGE/workspace" "$STAGE/meta"

copy_rel() {
  local rel="$1"
  local src="$ROOT/$rel"
  local dst="$STAGE/workspace/$rel"

  if [[ ! -e "$src" && ! -L "$src" ]]; then
    echo "ERROR: required path missing: $rel" >&2
    exit 2
  fi

  mkdir -p "$(dirname "$dst")"
  cp -a "$src" "$dst"
}

copy_if_exists() {
  local rel="$1"
  local src="$ROOT/$rel"
  local dst="$STAGE/workspace/$rel"

  if [[ -e "$src" || -L "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
  fi
}

echo "[1/7] Collecting root Gate scripts and project metadata..."

for rel in \
  README.md \
  PROJECT_SCOPE.md \
  BUNDLE_NOTES.md \
  INTERPRETATION.md \
  DESIGN_NOTES.md \
  README_GATE1B.md \
  README_GATE2.md \
  pyproject.toml \
  config.example.json \
  LOCAL_VALIDATION.json \
  gate8_clean_rebuild_v4_validation.json \
  run.sh \
  gate1a_runtime_discovery.sh \
  gate1b_prepare_isolated_runtime.sh \
  gate2_direct_smoke.py \
  gate2_pinned_kev_direct_smoke.sh \
  gate2a_reference_vs_fused.sh \
  gate2a_reference_vs_fused_v2.sh \
  gate3_system_one_qualification.sh \
  gate4_hybrid_v2_qualification.sh \
  gate5_robust_v3_qualification.sh \
  gate6_v4_qualification_and_contract.sh \
  gate6_v4_qualification_and_contract_v2.sh \
  gate6_v4_qualification_and_contract_v3.sh \
  gate7_mappability_aware_qualification.sh \
  gate7_mappability_aware_qualification_r4.sh \
  gate7_mappability_aware_qualification_r5.sh \
  gate7_mappability_aware_qualification_r6.sh \
  gate7_mappability_aware_qualification_r7.sh \
  gate7_mappability_aware_qualification_r8.sh
do
  copy_if_exists "$rel"
done

for rel in src scripts tests data integration; do
  copy_rel "$rel"
done

echo "[2/7] Collecting the effective Gate1-Gate7 evidence..."

for rel in \
  "runs/${RUN_ID}/gate0" \
  "runs/${RUN_ID}/gate1a-runtime-discovery" \
  "runs/${RUN_ID}/gate1b-isolated-runtime" \
  "runs/${RUN_ID}/gate2-kev-direct" \
  "runs/${RUN_ID}/gate2a-reference-vs-fused-v2" \
  "runs/${RUN_ID}/gate3-system-one-qualification" \
  "runs/${RUN_ID}/gate4-hybrid-v2-qualification" \
  "runs/${RUN_ID}/gate5-robust-v3-qualification" \
  "runs/${RUN_ID}/gate6-v4-qualification-and-contract-v3" \
  "runs/${RUN_ID}/gate7-mappability-aware-qualification-r8"
do
  copy_rel "$rel"
done

echo "[3/7] Collecting Gate8 harness, executions, repair sources and validation results..."

copy_rel "runs/${RUN_ID}/gate8-full-path-shadow-ab"

for rel in \
  gate8-clean-rebuild \
  gate8-clean-rebuild-v10 \
  .gate8-clean-tools \
  .gate8-delivery \
  .gate8-local-evidence
do
  copy_if_exists "$rel"
done

copy_if_exists "results/gate8-v10-checks-20261002-060209-1203180"

for rel in \
  gate8-clean-rebuild-v7.tar.gz \
  gate8-clean-rebuild-v8.tar.gz \
  gate8-clean-rebuild-v9.tar.gz \
  gate8-clean-rebuild-v10.tar.gz \
  gate8-current-egress-harness.tar.gz
do
  copy_if_exists "$rel"
done

echo "[4/7] Collecting the latest production-source snapshot without runtime state..."

LATEST_STATE="$(
  find "$ROOT/.gate8-clean-state" \
    -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
    | sort \
    | tail -n 1
)"

if [[ -n "${LATEST_STATE:-}" ]]; then
  copy_if_exists ".gate8-clean-state/${LATEST_STATE}/snapshot/source"
  copy_if_exists ".gate8-clean-state/${LATEST_STATE}/snapshot/harness"

  printf '%s\n' "$LATEST_STATE" \
    > "$STAGE/meta/latest-gate8-clean-state.txt"
else
  echo "WARNING: no .gate8-clean-state snapshot found" >&2
fi

echo "[5/7] Collecting Kev implementation + model metadata (no weights)..."

copy_rel "vendor/kev/kev"

for rel in \
  vendor/kev/pyproject.toml \
  vendor/kev/uv.lock \
  vendor/kev/README.md \
  vendor/kev/LICENSE \
  vendor/kev/modal_app.py \
  vendor/kev/tests/test_api.py \
  vendor/kev/tests/test_model.py \
  vendor/kev/tests/test_unit.py \
  vendor/kev/tests/test_conventions.py
do
  copy_if_exists "$rel"
done

MODEL_DIR=".models/kev-0.8b-9a45d25eb2ab"

for rel in \
  "${MODEL_DIR}/adapter_config.json" \
  "${MODEL_DIR}/provenance.json" \
  "${MODEL_DIR}/README.md" \
  "${MODEL_DIR}/result.json" \
  "${MODEL_DIR}/tokenizer_config.json" \
  "${MODEL_DIR}/training_config.json" \
  "${MODEL_DIR}/training_metrics.json"
do
  copy_if_exists "$rel"
done

{
  echo "# Model artifacts intentionally NOT bundled"
  echo "# path | size_bytes | mtime"
  if [[ -d "$ROOT/$MODEL_DIR" ]]; then
    find "$ROOT/$MODEL_DIR" -type f \
      \( -name '*.safetensors' -o -name '*.pt' -o -name '*.pth' \
         -o -name '*.bin' -o -name '*.onnx' -o -name '*.engine' \
         -o -name 'tokenizer.json' \) \
      -printf '%P | %s | %TY-%Tm-%TdT%TH:%TM:%TS\n' \
      | sort
  fi
} > "$STAGE/meta/model-artifacts-not-bundled.txt"

echo "[6/7] Removing caches, secrets, runtime state and raw service logs from staging..."

# Generic generated caches.
find "$STAGE" -type d \
  \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' \
     -o -name '.ruff_cache' -o -name '.tox' \) \
  -prune -exec rm -rf {} + 2>/dev/null || true

find "$STAGE" -type f \
  \( -name '*.pyc' -o -name '*.pyo' \) \
  -delete

# Never ship model weights / compiled models.
find "$STAGE" -type f \
  \( -name '*.safetensors' -o -name '*.pt' -o -name '*.pth' \
     -o -name '*.onnx' -o -name '*.engine' \) \
  -delete

# Never ship private/auth material.
find "$STAGE" -type d \
  \( -name '.private' -o -name '.state' \) \
  -prune -exec rm -rf {} + 2>/dev/null || true

find "$STAGE" -type f \
  \( -name '.env' -o -name '.env.*' \
     -o -name 'accounts.json' \
     -o -name 'cookies.json' \
     -o -name 'cookie.json' \
     -o -name 'session-cookie.json' \
     -o -name 'session_cookies.json' \
     -o -name 'credentials.json' \
     -o -name 'secrets.json' \
     -o -name 'id_rsa' -o -name 'id_ed25519' \
     -o -name '*.pem' -o -name '*.key' \) \
  -delete

# Runtime/process environment dumps are not needed for source/evidence review.
find "$STAGE" -type f \
  \( -name 'backend-base-env.json' \
     -o -name 'process-env.json' \
     -o -name 'process-environment.json' \
     -o -name 'environment.json' \
     -o -name 'env.txt' \) \
  -delete

# Raw backend/service logs are deliberately excluded.
find "$STAGE" -type d \
  \( -name 'service-logs' -o -name 'raw-logs' \) \
  -prune -exec rm -rf {} + 2>/dev/null || true

find "$STAGE" -type f \
  \( -name 'service.log' -o -name 'backend.log' \
     -o -name 'kev-service.log' -o -name 'deployment-events.jsonl' \) \
  -delete

# Local harness config may contain deployment-specific values.
find "$STAGE/workspace/.gate8-clean-tools" \
  -type f -name 'config.json' -delete 2>/dev/null || true

find "$STAGE/workspace/.gate8-clean-state" \
  -type f -name 'config.json' -delete 2>/dev/null || true

cat > "$STAGE/meta/SELECTION.txt" <<EOF
BLINK Intent Parse Router Gate1-Gate8 analysis bundle

Source workspace:
  $ROOT

Included:
  - top-level Gate1-Gate7 scripts, including retry/revision scripts
  - src/, scripts/, tests/, data/, integration/
  - effective historical evidence:
      gate0
      gate1a-runtime-discovery
      gate1b-isolated-runtime
      gate2-kev-direct
      gate2a-reference-vs-fused-v2
      gate3-system-one-qualification
      gate4-hybrid-v2-qualification
      gate5-robust-v3-qualification
      gate6-v4-qualification-and-contract-v3
      gate7-mappability-aware-qualification-r8
  - Gate8 full-path harness and execution evidence
  - Gate8 v7-v10 repair/rebuild packages when present
  - Gate8 clean-tool history and local evidence
  - current Gate8 v10 validation results when present
  - latest Gate8 production-source + harness snapshot
  - Kev implementation and selected tests
  - Kev model metadata only

Explicitly excluded:
  - .runtime
  - .cache
  - general .state
  - full .gate8-clean-state runtime cells/raw logs
  - .private
  - account/session/cookie/credential files
  - .env and .env.*
  - full process-environment dumps
  - raw backend/service logs
  - model weights and compiled model files
  - tokenizer.json payload
EOF

{
  echo "Generated: $(date --iso-8601=seconds 2>/dev/null || date)"
  echo "Host: $(hostname)"
  echo "Kernel: $(uname -srmo)"
  echo "Workspace: $ROOT"
  echo "Selected run: $RUN_ID"
  echo "Latest Gate8 clean state: ${LATEST_STATE:-none}"
  echo
  echo "Staged file count:"
  find "$STAGE/workspace" -type f | wc -l
  echo
  echo "Staged size:"
  du -sh "$STAGE/workspace"
} > "$STAGE/meta/BUNDLE_INFO.txt"

echo "[7/7] Creating analysis bundle..."

tar -czf "$BUNDLE" -C "$STAGE" workspace meta

echo
echo "============================================================"
echo "ANALYSIS BUNDLE COMPLETE"
echo "============================================================"
echo "Bundle:"
echo "  $BUNDLE"
echo
echo "Size:"
du -h "$BUNDLE"
echo
echo "Upload this single file to ChatGPT."
echo
echo "IMPORTANT:"
echo "  This script did NOT delete or modify the benchmark workspace."
echo "  Do NOT clean the workspace yet."

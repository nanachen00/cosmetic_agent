#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# All local synthetic/loopback scratch stays inside this existing harness.
[[ ! -L "$HERE/.maintenance" ]] || { echo '[REFUSED] Linked maintenance directory'; exit 1; }
mkdir -p -- "$HERE/.maintenance"
chmod 700 "$HERE/.maintenance"
SELFTEST_TMP="$(mktemp -d "$HERE/.maintenance/selftest-XXXXXXXX")"
trap 'rm -rf -- "$SELFTEST_TMP"' EXIT
export TMPDIR="$SELFTEST_TMP" TMP="$SELFTEST_TMP" TEMP="$SELFTEST_TMP"
export PYTHONDONTWRITEBYTECODE=1
for f in "$HERE"/*.sh; do bash -n "$f"; done
for test in test_bundle.py test_auth_workflow.py test_concurrent_deploy.py test_concurrent_requests.py test_native_schema.py test_provider_comparison.py test_endpoint_correlation.py test_legacy_contract.py test_legacy_schema_fields.py test_generation_budget.py; do
  "${PYTHON_BIN:-python3}" -I -B "$HERE/tests/$test"
done

#!/usr/bin/env bash
set -u

PROJECT_ROOT="/var/lib/jenkins/tomod/cosmetic-agent"
PREFERRED_PYTHON="/opt/conda/envs/blink-backend/bin/python"
if [[ -z "${PYTHON_BIN:-}" && -x "$PREFERRED_PYTHON" ]]; then
  PYTHON_BIN="$PREFERRED_PYTHON"
else
  PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python || true)}"
fi

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: 找不到可執行的 Python；可用 PYTHON_BIN 指定路徑"
  exit 2
fi

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJECT_ROOT/.env"
  set +a
fi

if [[ ! -d "$PROJECT_ROOT" ]]; then
  echo "ERROR: 找不到 $PROJECT_ROOT"
  exit 2
fi

echo "[1/2] 執行離線原始碼契約測試（不連資料庫/模型）"
cd "$PROJECT_ROOT" || exit 2
PYTHONPYCACHEPREFIX=/tmp/blink_regression_pycache \
  "$PYTHON_BIN" -m unittest -v tests.regression.test_source_contracts
offline_status=$?

if [[ $offline_status -ne 0 ]]; then
  echo "離線測試失敗；停止，不執行線上測試。"
  exit $offline_status
fi

echo "[2/2] 執行線上基礎 smoke test"
"$PYTHON_BIN" tests/regression/live_regression.py \
  --base-url http://127.0.0.1:8010
live_status=$?

if [[ $live_status -ne 0 ]]; then
  echo "線上 smoke test 失敗。請查看上方 FAIL 項目與 /tmp/api_8011.log。"
  exit $live_status
fi

echo
echo "若要另外測試真正的 Agent 分流，執行："
echo "$PYTHON_BIN $PROJECT_ROOT/tests/regression/live_regression.py --chat"
exit 0

#!/usr/bin/env bash
# Post-dispatch hotfix 2026-10-29 (review L1–L4 + O1) for $ROOT/cosmetic-agent.
#
#   ./hotfix.sh check                 用 sha256 判斷每個目標檔是 BASE（10/21 restore v5）、APPLIED 或 DRIFT；
#                                     有 DRIFT 就把伺服器上的現行檔案打包到 drift/，交給 Claude 讀取最新版
#   ./hotfix.sh selftest              在暫存複本上套用並跑測試；不碰 $ROOT
#   ./hotfix.sh apply                 check → selftest → 備份 → 只替換 BASE 的檔案（已是修正版的略過）→ py_compile；不重啟服務
#   ./hotfix.sh restart-backend --yes 驗證 pid 與命令列後只重啟 backend :8010（其他服務維持原狀）
#   ./hotfix.sh verify                檔案狀態、backend 啟動時間晚於 apply、/livez、/readyz
#   ./hotfix.sh smoke --yes           打一則「profile 問句＋緊急症狀」訪客訊息，預期 stage=emergency（會建立 1 個訪客對話）
#   ./hotfix.sh rollback              用 bundle 內的 base/（10/21 版，sha256 驗證）還原，移除本修正新增的檔案；之後要再 restart-backend
#
# Overrides: ROOT（預設 /var/lib/jenkins/tomod）、PYTHON（預設 $BACKEND_ENV/bin/python）、BACKUPS（備份目錄，預設 ./backups）。
set -Eeuo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="${ROOT:-/var/lib/jenkins/tomod}"
# shellcheck source=/dev/null
source "$ROOT/ops/runtime/runtime.conf"
PYTHON="${PYTHON:-$BACKEND_ENV/bin/python}"
PAYLOAD="$HERE/payload"
BASE_DIR="$HERE/base"
BACKUPS="${BACKUPS:-$HERE/backups}"
DRIFT_DIR="${DRIFT_DIR:-$HERE/drift}"
APPLIED_MARK="$BACKUPS/LAST_APPLY"
TESTS=(test/test_postdispatch_fix.py test/test_response_composer.py test/test_root_routing.py tests/test_intent_fast_path.py)

say() { echo "[hotfix] $*"; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
targets() { (cd "$PAYLOAD" && find . -type f ! -name '*.pyc' | sed 's|^\./||' | sort); }
base_sha() { awk -v f="$1" '$2 == f { print $1 }' "$HERE/BASE_SHA256SUMS"; }

state_of() {  # rel -> BASE | APPLIED | DRIFT | MISSING
  local rel="$1" live="$ROOT/$1" base
  base="$(base_sha "$rel")"
  if [[ ! -f "$live" ]]; then [[ -z "$base" ]] && echo BASE || echo MISSING; return; fi
  local now; now="$(sha "$live")"
  if [[ "$now" == "$(sha "$PAYLOAD/$rel")" ]]; then echo APPLIED
  elif [[ -n "$base" && "$now" == "$base" ]]; then echo BASE
  else echo DRIFT; fi
}

cmd_check() {  # prints one line per file; sets SUMMARY to all-base | all-applied | mixed | drift
  local rel st n_base=0 n_applied=0 n_bad=0
  for rel in $(targets); do
    st="$(state_of "$rel")"; printf '  %-8s %s\n' "$st" "$rel"
    case "$st" in BASE) n_base=$((n_base + 1));; APPLIED) n_applied=$((n_applied + 1));; *) n_bad=$((n_bad + 1));; esac
  done
  if ((n_bad)); then SUMMARY=drift; elif ((n_applied == 0)); then SUMMARY=all-base
  elif ((n_base == 0)); then SUMMARY=all-applied; else SUMMARY=mixed; fi
  say "state: $SUMMARY"
  [[ "$SUMMARY" != drift ]] || package_drift
}

package_drift() {  # current server copies of every target + expected/actual hashes, for Claude to read
  local stamp rel live; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local dir="$DRIFT_DIR/drift-$stamp"; mkdir -p "$dir"
  {
    echo "hotfix-postdispatch drift report $stamp (UTC), ROOT=$ROOT"
    for rel in $(targets); do
      live="$ROOT/$rel"
      echo "== $rel"
      echo "   state:        $(state_of "$rel")"
      echo "   base_sha256:  $(base_sha "$rel")"
      echo "   fix_sha256:   $(sha "$PAYLOAD/$rel")"
      if [[ -f "$live" ]]; then
        echo "   live_sha256:  $(sha "$live")"
        echo "   live_stat:    $(stat -c 'size=%s mtime=%y owner=%U:%G mode=%a' "$live")"
        mkdir -p "$dir/$(dirname "$rel")"; cp -p "$live" "$dir/$rel"
      else
        echo "   live_sha256:  (missing)"
      fi
    done
  } >"$dir/DRIFT_REPORT.txt"
  tar -czf "$dir.tar.gz" -C "$DRIFT_DIR" "drift-$stamp"
  say "伺服器上的檔案和修正假設的版本不同。已把現行檔案與 hash 打包：$dir.tar.gz"
  say "請把這個 tar.gz 交給 Claude 讀取最新版，重做修正後再套用"
}

verify_bundle() {
  [[ -f "$HERE/SHA256SUMS" ]] || { say "SHA256SUMS 不存在（開發複本），略過 bundle 完整性檢查"; return; }
  (cd "$HERE" && sha256sum --quiet -c SHA256SUMS) || { say "bundle 檔案與 SHA256SUMS 不符，停止"; exit 3; }
  say "bundle SHA256SUMS OK"
}

cmd_selftest() {
  "$PYTHON" -c 'import pytest' 2>/dev/null || { say "$PYTHON 沒有 pytest（requirements-full.txt 有列），停止"; exit 3; }
  local src_mb; src_mb="$(du -sm "$ROOT/cosmetic-agent" | cut -f1)"
  if ((src_mb > ${SELFTEST_MAX_MB:-4096})); then
    say "$ROOT/cosmetic-agent 有 ${src_mb} MB，超過 SELFTEST_MAX_MB=${SELFTEST_MAX_MB:-4096}；確認 TMPDIR 空間足夠後再調高"; exit 3
  fi
  local tmp rc=0 extra=(); tmp="$(mktemp -d "${TMPDIR:-/tmp}/hotfix-postdispatch.XXXXXX")"
  [[ -n "${SYSTEMROOT:-}" ]] && extra+=(SYSTEMROOT="$SYSTEMROOT")   # Windows local replica only
  # Code only: no .env or other secrets (tests must never see production values), no logs or caches.
  tar -C "$ROOT" --exclude='.env' --exclude='.env.*' --exclude='accounts.json' --exclude='*.pem' --exclude='*.key' \
      --exclude='logs' --exclude='*.log' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='node_modules' \
      --exclude='.git' -cf - cosmetic-agent | tar -C "$tmp" -xf -
  [[ ! -e "$tmp/cosmetic-agent/.env" ]] || { rm -rf "$tmp"; say ".env 被複製進暫存區，停止"; exit 3; }
  (cd "$PAYLOAD" && tar --exclude='__pycache__' -cf - .) | tar -C "$tmp" -xf -
  # The team code hard-codes sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent') and load_dotenv('<that>/.env'),
  # so on the VM a plain copy silently imports production modules (and could read the production .env).
  # Point those literals at the copy; the isolation plugin below fails the run if anything still comes from $ROOT.
  local prod_lit=/var/lib/jenkins/tomod/cosmetic-agent n copy_py root_py
  copy_py="$(cygpath -m "$tmp/cosmetic-agent" 2>/dev/null || echo "$tmp/cosmetic-agent")"   # cygpath: Windows local replica only
  root_py="$(cygpath -m "$ROOT/cosmetic-agent" 2>/dev/null || echo "$ROOT/cosmetic-agent")"
  grep -rlF --include='*.py' -e "$prod_lit" -e "$root_py" "$tmp/cosmetic-agent" >"$tmp/rewritten.txt" || true
  n="$(wc -l <"$tmp/rewritten.txt" | tr -d ' ')"
  if ((n)); then xargs -a "$tmp/rewritten.txt" sed -i -e "s#$prod_lit#$copy_py#g" -e "s#$root_py#$copy_py#g"; fi
  mkdir -p "$tmp/plugin"
  cat >"$tmp/plugin/hotfix_isolation.py" <<'PY'
"""Fail the selftest when any imported module or sys.path entry lives in the production tree."""
import os, sys
import pytest

TREES = [os.path.normcase(os.path.realpath(p)) for p in os.environ["HOTFIX_PROD_TREES"].splitlines() if p.strip()]
ENV_AT_START = set(os.environ)        # plugins load before collection, i.e. before any module calls load_dotenv()

def _in_prod(path):
    path = os.path.normcase(os.path.realpath(path))
    return any(path == t or path.startswith(t + os.sep) for t in TREES)


def pytest_sessionfinish(session, exitstatus):
    leaked = sorted(n for n, m in list(sys.modules.items()) if getattr(m, "__file__", None) and _in_prod(m.__file__))
    paths = [p for p in sys.path if p and _in_prod(p)]
    added = sorted(set(os.environ) - ENV_AT_START)   # names only, never values
    print(f"\n[hotfix] env vars added during the run (names only): {added or 'none'}")
    if leaked or paths:
        print(f"[hotfix] isolation FAIL: {len(leaked)} modules from production {TREES}: {leaked[:8]} sys.path={paths}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    else:
        print(f"[hotfix] isolation OK: {len(sys.modules)} modules, none from {TREES}")
PY
  local trees; trees="$(printf '%s\n%s\n' "$root_py" "$prod_lit")"
  say "暫存複本：$tmp/cosmetic-agent（$(du -sm "$tmp" | cut -f1) MB）；$n 個 .py 的正式路徑已改指向暫存複本；執行 ${#TESTS[@]} 個測試檔"
  (
    cd "$tmp/cosmetic-agent"
    env -i PATH="$PATH" HOME="$tmp" LANG=C.UTF-8 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ${extra[@]+"${extra[@]}"} \
        PYTHONPATH="$(cygpath -m "$tmp/plugin" 2>/dev/null || echo "$tmp/plugin")" HOTFIX_PROD_TREES="$trees" \
        OPENAI_API_KEY=hotfix-selftest-not-a-key RESPONSE_COMPOSER_ENABLED=1 \
        "$PYTHON" -m pytest -q -p no:cacheprovider -p hotfix_isolation "${TESTS[@]}"
  ) || rc=$?
  rm -rf "$tmp"
  ((rc == 0)) || { say "selftest FAIL（pytest rc=$rc），未修改 $ROOT"; exit 3; }
  say "selftest PASS"
}

replace_file() {  # src dst  — keep dst's mode/owner when it exists, swap atomically
  local src="$1" dst="$2" tmp="$2.hotfix-tmp"
  if [[ -f "$dst" ]]; then cp -p "$dst" "$tmp"; cat "$src" >"$tmp"; else mkdir -p "$(dirname "$dst")"; cp "$src" "$tmp"; fi
  mv -f "$tmp" "$dst"
}

cmd_apply() {
  verify_bundle
  cmd_check
  case "$SUMMARY" in
    all-applied) say "已經套用過，不需要再套用"; return;;
    drift) say "沒有修改任何檔案"; exit 3;;
    mixed) say "部分檔案已經是修正版（例如手動更新過 L1），只套用其餘檔案";;
  esac
  if [[ "${SKIP_SELFTEST:-0}" == 1 ]]; then say "SKIP_SELFTEST=1，略過 selftest"; else cmd_selftest; fi
  local stamp dir rel; stamp="$(date -u +%Y%m%dT%H%M%SZ)"; dir="$BACKUPS/$stamp"
  mkdir -p "$dir"; : >"$dir/NEW_FILES"
  for rel in $(targets); do
    [[ "$(state_of "$rel")" == BASE ]] || continue
    if [[ -f "$ROOT/$rel" ]]; then mkdir -p "$dir/$(dirname "$rel")"; cp -p "$ROOT/$rel" "$dir/$rel"; else echo "$rel" >>"$dir/NEW_FILES"; fi
  done
  say "備份：$dir"
  for rel in $(targets); do
    if [[ "$(state_of "$rel")" == APPLIED ]]; then printf '  %-8s %s\n' "skip" "$rel"; continue; fi
    replace_file "$PAYLOAD/$rel" "$ROOT/$rel"; printf '  %-8s %s\n' "replaced" "$rel"
  done
  (cd "$ROOT/cosmetic-agent" && "$PYTHON" -m py_compile agents/root/orchestrator.py tools/a2a/skin_product_bridge.py tools/agent/response_composer.py)
  cmd_check
  [[ "$SUMMARY" == all-applied ]] || { say "套用後狀態不是 all-applied，請執行 ./hotfix.sh rollback"; exit 4; }
  echo "$stamp $(date -u +%s)" >"$APPLIED_MARK"
  say "已套用（執行中的 backend 仍是舊程式）。下一步：./hotfix.sh restart-backend --yes，然後 ./hotfix.sh verify"
}

cmd_rollback() {
  local rel base
  cmd_check   # refuse before touching anything if a file is neither ours nor the base
  [[ "$SUMMARY" != drift ]] || { say "沒有還原任何檔案"; exit 3; }
  for rel in $(targets); do   # every base copy we would write must still hash to the 10/21 version
    [[ "$(state_of "$rel")" == APPLIED ]] || continue
    base="$(base_sha "$rel")"; [[ -n "$base" ]] || continue
    [[ -f "$BASE_DIR/$rel" && "$(sha "$BASE_DIR/$rel")" == "$base" ]] \
      || { say "$BASE_DIR/$rel 不存在或 hash 不符，沒有還原任何檔案"; exit 3; }
  done
  say "用 $BASE_DIR 的 10/21 版本還原"
  for rel in $(targets); do
    if [[ "$(state_of "$rel")" == BASE ]]; then printf '  %-8s %s\n' "skip" "$rel"; continue; fi
    if [[ -z "$(base_sha "$rel")" ]]; then rm -f "$ROOT/$rel"; printf '  %-8s %s\n' "removed" "$rel"
    else replace_file "$BASE_DIR/$rel" "$ROOT/$rel"; printf '  %-8s %s\n' "restored" "$rel"; fi
  done
  rm -f "$APPLIED_MARK"
  cmd_check
  [[ "$SUMMARY" == all-base ]] || { say "還原後狀態不是 all-base"; exit 4; }
  say "已還原。下一步：./hotfix.sh restart-backend --yes"
}

backend_pid() {  # prints the verified backend pid, or nothing
  local pid cmd; pid="$(cat "$PIDDIR/backend.pid" 2>/dev/null || true)"
  [[ -n "$pid" && -r "/proc/$pid/cmdline" ]] || return 0
  cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
  [[ "$cmd" == *"uvicorn api.api_server:app"* && "$cmd" == *"--port 8010"* ]] && echo "$pid"
  return 0
}

cmd_restart_backend() {
  [[ "${1:-}" == --yes ]] || { say "會重啟正式 backend :8010（約 10–60 秒無法服務）。確認後加 --yes"; exit 2; }
  local pid; pid="$(backend_pid)"
  if [[ -z "$pid" ]]; then
    say "$PIDDIR/backend.pid 不存在，或該 pid 的命令列不是 'uvicorn api.api_server:app --port 8010'；不會停止任何程序"
    say "請用平常的方式重啟 backend，再執行 ./hotfix.sh verify"; exit 3
  fi
  say "停止 backend PID=$pid：$(tr '\0' ' ' <"/proc/$pid/cmdline")"
  kill "$pid"
  for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  if kill -0 "$pid" 2>/dev/null; then say "30 秒後仍未結束，送 SIGKILL"; kill -9 "$pid"; fi
  rm -f "$PIDDIR/backend.pid"
  say "用 start-production.sh 補起 backend（其他已在執行的服務會顯示 [UP] 並略過）"
  "$ROOT/ops/runtime/start-production.sh"
}

cmd_verify() {
  cmd_check
  local pid; pid="$(backend_pid)"
  if [[ -n "$pid" && -f "$APPLIED_MARK" ]]; then
    local started applied
    started=$(( $(date +%s) - $(ps -o etimes= -p "$pid" | tr -d ' ') ))
    applied="$(cut -d' ' -f2 "$APPLIED_MARK")"
    if ((started >= applied)); then say "backend PID=$pid 在 apply 之後啟動 ✓"; else say "backend PID=$pid 在 apply 之前啟動：仍是舊程式，請 restart-backend"; fi
  fi
  curl -fsS --max-time 5 http://127.0.0.1:8010/livez >/dev/null && say "/livez OK" || say "/livez 失敗"
  curl -sS --max-time 10 http://127.0.0.1:8010/readyz | "$PYTHON" -m json.tool || true
}

cmd_smoke() {
  [[ "${1:-}" == --yes ]] || { say "會在正式 backend 建立 1 個訪客對話（不呼叫 Terra）。確認後加 --yes"; exit 2; }
  ORIGIN="${SMOKE_ORIGIN:-https://www.blinkai.tw}" "$PYTHON" - <<'PY'
import http.cookiejar, json, os, urllib.request, uuid
api, h = "http://127.0.0.1:8010", {"Content-Type": "application/json", "Origin": os.environ["ORIGIN"]}
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
op.open(urllib.request.Request(api + "/api/auth/guest-session", data=b"{}", headers=h), timeout=15).read()
body = {"message": "我是什麼膚質？我剛擦完精華嘴唇腫起來而且呼吸困難", "conversation_id": "hotfix-smoke-" + uuid.uuid4().hex[:8]}
req = urllib.request.Request(api + "/api/chat/message/stream", data=json.dumps(body, ensure_ascii=False).encode(),
                             headers={**h, "Idempotency-Key": str(uuid.uuid4())})
event, done = None, None
with op.open(req, timeout=60) as r:
    for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("event:"): event = line[6:].strip()
        elif line.startswith("data:") and event in ("done", "error"): done = {"event": event, **json.loads(line[5:])}
stage = (done or {}).get("stage")
print(f"[hotfix] smoke stage={stage} reply={str((done or {}).get('reply'))[:40]!r}")
raise SystemExit(0 if stage == "emergency" else 1)
PY
}

case "${1:-}" in
  check) cmd_check;;
  selftest) cmd_selftest;;
  apply) cmd_apply;;
  rollback) cmd_rollback;;
  restart-backend) shift; cmd_restart_backend "$@";;
  verify) cmd_verify;;
  smoke) shift; cmd_smoke "$@";;
  *) sed -n "2,13p" "$0"; exit 2;;
esac

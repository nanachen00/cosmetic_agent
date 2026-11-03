#!/bin/bash

set -e

APP_DIR="/var/lib/jenkins/tomod/cosmetic-agent"
RESTART_SCRIPT="/var/lib/jenkins/tomod/restart-backend-prod.sh"

cd "$APP_DIR"

echo "========================================"
echo " BLINK AI Backend Production Deploy"
echo "========================================"
echo

# =========================================================
# Safety Check
# =========================================================

echo "檢查 Backend working tree..."

BACKEND_DIRTY=$(
    git status --porcelain --         agents         api         core         tools         test         tests     | grep -v '\.bak'     || true
)

if [ -n "$BACKEND_DIRTY" ]; then
    echo "❌ Backend 有未提交修改"
    echo
    echo "$BACKEND_DIRTY"
    echo
    echo "請先 commit / stash / 處理 backend 修改，再部署。"
    exit 1
fi

echo "✅ Backend working tree clean"

# =========================================================
# Git Information
# =========================================================

BRANCH=$(git branch --show-current)

if [ -z "$BRANCH" ]; then
    echo "❌ 無法取得目前 Git branch"
    exit 1
fi

echo
echo "目前 branch: $BRANCH"

echo
echo "目前 local commit:"
git log -1 --oneline

# =========================================================
# Fetch
# =========================================================

echo
echo "從 GitHub 取得最新版本..."

git fetch origin

if ! git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
    echo "❌ 找不到 origin/$BRANCH"
    exit 1
fi

echo
echo "Remote commit:"
git log -1 --oneline "origin/$BRANCH"

LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
    echo
    echo "ℹ️ 已經是最新版本"
else
    echo
    echo "即將部署以下更新："
    git log --oneline --decorate \
        "$LOCAL_COMMIT..$REMOTE_COMMIT"

    echo
    echo "更新 production working tree..."

    git pull --ff-only origin "$BRANCH"

    echo "✅ Git 更新完成"
fi

# =========================================================
# Confirm Deployed Commit
# =========================================================

echo
echo "準備部署 commit："
git log -1 --oneline

# =========================================================
# Restart Backend
# =========================================================

echo
echo "開始重啟 production backend..."

"$RESTART_SCRIPT"

# =========================================================
# Public Health Check
# =========================================================

echo
echo "測試 public API health..."

PUBLIC_HEALTH=$(
    curl -fsS \
        "https://api.blinkai.tw/api/v1/health"
)

echo "$PUBLIC_HEALTH"

echo "$PUBLIC_HEALTH" \
    | grep -q '"status":"ok"' \
|| {
    echo "❌ Public health check 失敗"
    exit 1
}

echo "✅ Public API health OK"

# =========================================================
# Public CORS Check
# =========================================================

echo
echo "測試 public CORS..."

CORS_RESULT=$(
    curl -si -X OPTIONS \
        "https://api.blinkai.tw/api/chat/message" \
        -H "Origin: https://www.blinkai.tw" \
        -H "Access-Control-Request-Method: POST" \
        -H "Access-Control-Request-Headers: content-type"
)

echo "$CORS_RESULT" \
    | grep -qi \
      "access-control-allow-origin: https://www.blinkai.tw" \
|| {
    echo "❌ Public CORS check 失敗"
    echo "$CORS_RESULT"
    exit 1
}

echo "✅ Public CORS OK"

# =========================================================
# Final Summary
# =========================================================

echo
echo "========================================"
echo " ✅ Production Deploy Complete"
echo "========================================"

echo "Branch: $BRANCH"
echo -n "Commit: "
git rev-parse --short HEAD

echo
echo "Backend:"
echo "  Main API :8010"
echo "  Auth API :8011"

echo
echo "Source:"
echo "  $APP_DIR"

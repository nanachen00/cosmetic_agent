#!/usr/bin/env bash
# shadow-sys1 離線自測（mock only）：shadowpatch 保險、Gate B 一致性、overlay 整合、S3 影子比對、T8 切換/回滾演練。
# 用到的埠：127.0.0.1:8212（mock backend）、8234/8235（mock Sys1）；8238 必須空著（當作「Sys1 掛掉」）。不碰 production、GPU、真模型。
set -euo pipefail
cd "$(dirname "$0")"
for p in 8212 8234 8235 8238; do (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && { echo "port $p busy — aborting"; exit 1; } || true; done
python3 -m py_compile shadowpatch.py s3_shadow.py sys1ctl.py rollback_check.py shadow_stack.py payload/sys1_dispatch.py tests/*.py
bash -n find_prod_roots.sh
python3 -W ignore tests/test_sys1_dispatch.py 2>&1 | tail -3
python3 -W ignore tests/test_s3_t8.py 2>&1 | tail -3
python3 -W ignore tests/test_shadow_stack.py 2>&1 | tail -3
python3 - <<'PY'
import json; c = json.load(open("control/sys1_dispatch.json")); assert c["arm"] == "off" and c["error_fallback"] == "safety_or_clarify", c
print("control/sys1_dispatch.json: arm=off ✓")
PY
echo "shadow-sys1 selftest OK（mock only）"

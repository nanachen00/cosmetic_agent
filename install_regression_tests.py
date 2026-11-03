"""將 BLINK AI 回歸測試安裝到遠端專案；不修改正式業務程式。"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/var/lib/jenkins/tomod")
SOURCE = ROOT / "regression_tests"
TARGET = ROOT / "cosmetic-agent" / "tests" / "regression"
TESTS_INIT = ROOT / "cosmetic-agent" / "tests" / "__init__.py"
RUNNER_SOURCE = ROOT / "run_regression_tests.sh"


def main() -> None:
    required = (
        SOURCE / "__init__.py",
        SOURCE / "test_source_contracts.py",
        SOURCE / "live_regression.py",
        RUNNER_SOURCE,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("安裝包缺少檔案：" + ", ".join(missing))

    if TARGET.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = TARGET.with_name(f"regression.bak_{stamp}")
        shutil.copytree(TARGET, backup)
        print(f"已備份舊測試：{backup}")

    TARGET.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "test_source_contracts.py", "live_regression.py"):
        shutil.copy2(SOURCE / name, TARGET / name)

    if not TESTS_INIT.exists():
        TESTS_INIT.write_text(
            '\"\"\"BLINK AI test package.\"\"\"\n',
            encoding="utf-8",
        )

    RUNNER_SOURCE.chmod(0o755)
    print(f"已安裝：{TARGET}")
    print(f"測試指令：{RUNNER_SOURCE}")


if __name__ == "__main__":
    main()

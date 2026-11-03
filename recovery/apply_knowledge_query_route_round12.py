from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    old = '''    if any(marker in compact for marker in ("乾肌", "繃繃", "洗後緊繃", "洗完很緊")):
        return "乾燥脫皮"
'''
    new = '''    if any(marker in compact for marker in ("乾肌", "繃繃", "洗後緊繃", "洗完很緊", "起皮")):
        return "乾燥脫皮"
'''
    if new in source:
        print("knowledge query route round 12 already applied")
        return
    if source.count(old) != 1:
        raise RuntimeError(f"expected one dry-skin marker block, found {source.count(old)}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    print("knowledge query route round 12 applied")


if __name__ == "__main__":
    main()

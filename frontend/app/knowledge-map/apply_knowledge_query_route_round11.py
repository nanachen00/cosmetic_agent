from __future__ import annotations

import sys
from pathlib import Path


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return source.replace(old, new, 1)


def main() -> None:
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    marker = "knowledge query route round 11"
    if marker in source:
        print("knowledge query route round 11 already applied")
        return

    source = replace_once(
        source,
        '''        for marker in ("吃", "口服", "劑量", "幾顆", "幾粒", "加倍", "懷孕", "備孕", "哺乳")
''',
        '''        for marker in ("吃", "劑量", "幾顆", "幾粒", "加倍", "懷孕", "備孕", "哺乳")
''',
        "oral retinoid action markers",
    )

    source = replace_once(
        source,
        '''def _canonical_single_query(value: str) -> str:
    compact = _compact_query(value)
    if "bha" in compact or "salicylicacid" in compact:
        return "BHA"
    if any(marker in compact for marker in ("乾肌", "繃繃", "洗後緊繃", "洗完很緊")):
        return "乾燥脫皮"
    return value
''',
        '''def _canonical_single_query(value: str) -> str:
    compact = _compact_query(value)
    if "bha" in compact or "salicylicacid" in compact:
        return "BHA"
    if any(marker in compact for marker in ("乾肌", "繃繃", "洗後緊繃", "洗完很緊")):
        return "乾燥脫皮"
    if "洗完" in compact and any(marker in compact for marker in ("緊", "繃")):
        return "乾燥脫皮"
    if "紅" in compact and "熱" in compact:
        return "臉部泛紅"
    return value


# knowledge query route round 11
''',
        "colloquial canonical queries",
    )

    path.write_text(source, encoding="utf-8")
    print("knowledge query route round 11 applied")


if __name__ == "__main__":
    main()

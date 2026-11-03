from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    old = '''def _urgent_medical_query(value: str) -> bool:
    compact = _compact_query(value)
    return any(marker in compact for marker in _URGENT_MARKERS)
'''
    new = '''def _urgent_medical_query(value: str) -> bool:
    compact = _compact_query(value)
    if any(marker in compact for marker in _URGENT_MARKERS):
        return True
    # Oral retinoid dosage, pregnancy, and self-adjustment questions need a
    # clinician/pharmacist rather than an actionable beauty graph response.
    retinoid_reference = "a酸" in compact or any(
        marker in compact for marker in _ORAL_RETINOID_MARKERS
    )
    oral_medication_request = any(
        marker in compact
        for marker in ("吃", "口服", "劑量", "幾顆", "幾粒", "加倍", "懷孕", "備孕", "哺乳")
    )
    return retinoid_reference and oral_medication_request
'''
    if new in source:
        print("knowledge query route round 9 already applied")
        return
    if source.count(old) != 1:
        raise RuntimeError(f"expected one urgent-query helper, found {source.count(old)}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    print("knowledge query route round 9 applied")


if __name__ == "__main__":
    main()

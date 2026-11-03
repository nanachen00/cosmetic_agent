from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    old = '''                    "code": "knowledge_query_out_of_scope",
                    "query": payload.query,
                    "suggestions": [],
                    "intent": "medical_attention",
'''
    new = '''                    "code": "knowledge_query_out_of_scope",
                    "suggestions": [],
                    "intent": "medical_attention",
'''
    if new in source and old not in source:
        print("knowledge query route round 10 already applied")
        return
    if source.count(old) != 1:
        raise RuntimeError(f"expected one urgent response payload, found {source.count(old)}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    print("knowledge query route round 10 applied")


if __name__ == "__main__":
    main()

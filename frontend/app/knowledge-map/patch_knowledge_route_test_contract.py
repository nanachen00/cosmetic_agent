from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_knowledge_route_test_contract.py TARGET")
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    old = "    async def explain(self, node_id):\n"
    new = "    async def explain(self, node_id, *, viewer_context=None, focus=\"overview\"):\n"
    if new in source:
        print("route test contract already current")
        return
    if source.count(old) != 1:
        raise RuntimeError(f"expected one FakeService.explain signature, found {source.count(old)}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    print("route test contract updated")


if __name__ == "__main__":
    main()

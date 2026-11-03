from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


GRAPH_PATH = Path(
    "/var/lib/jenkins/tomod/cosmetic-agent/data/knowledge_graph/v1/knowledge_map.json"
)
BACKUP_DIR = Path("/var/lib/jenkins/tomod/recovery-backups")

ALIAS_UPDATES = {
    "乾燥脫皮": [
        "乾肌", "緊繃", "繃繃", "繃繃的", "起屑", "洗後緊繃", "洗完很緊", "洗後很緊",
    ],
    "臉部泛紅": [
        "泛紅", "刺痛泛紅", "敏感泛紅", "敏感紅", "容易泛紅",
    ],
    "口服 A 酸 (Isotretinoin)": [
        "異維A酸", "異維 A 酸", "異維甲酸", "Isotretinoin", "isotretinoin", "口服異維A酸", "口服異維 A 酸",
    ],
}


def merge_unique(existing: object, additions: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    values = existing if isinstance(existing, list) else []
    for value in [*values, *additions]:
        text = str(value).strip()
        key = text.casefold().replace(" ", "")
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def main() -> None:
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        raise RuntimeError("unexpected knowledge map schema")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"knowledge-map-before-alias-round6-{stamp}.json"
    shutil.copy2(GRAPH_PATH, backup)

    changed: list[str] = []
    missing = set(ALIAS_UPDATES)
    for node in nodes:
        name = str(node.get("name") or node.get("display_name") or "").strip()
        if name not in ALIAS_UPDATES:
            continue
        missing.discard(name)
        merged = merge_unique(node.get("aliases"), ALIAS_UPDATES[name])
        if merged != node.get("aliases"):
            node["aliases"] = merged
            changed.append(name)
        properties = node.get("properties")
        if isinstance(properties, dict):
            properties["aliases"] = merge_unique(properties.get("aliases"), ALIAS_UPDATES[name])

    if missing:
        raise RuntimeError("missing canonical nodes: " + ", ".join(sorted(missing)))

    GRAPH_PATH.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"changed": sorted(set(changed)), "backup": str(backup)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

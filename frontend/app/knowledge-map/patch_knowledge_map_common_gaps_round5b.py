from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


GRAPH_PATH = Path(
    "/var/lib/jenkins/tomod/cosmetic-agent/data/knowledge_graph/v1/knowledge_map.json"
)
BACKUP_DIR = Path("/var/lib/jenkins/tomod/recovery-backups")


def source_fields(*, source_id: str, title: str, url: str) -> dict[str, object]:
    return {
        "source_ids": [source_id],
        "evidence_titles": [title],
        "evidence_urls": [url],
        "reviewed_at": "2026-11-02",
        "knowledge_patch_id": "kg-common-gaps-v1",
    }


AAD_RETINOL = source_fields(
    source_id="aad-retinoid-retinol",
    title="American Academy of Dermatology — Retinoid or retinol?",
    url="https://www.aad.org/public/everyday-care/skin-care-secrets/anti-aging/retinoid-retinol/",
)
AAD_DRY_SKIN = source_fields(
    source_id="aad-dry-skin-care",
    title="American Academy of Dermatology — Dermatologists' top tips for relieving dry skin",
    url="https://www.aad.org/public/everyday-care/skin-care-basics/dry/dermatologists-tips-relieve-dry-skin",
)


def node(
    node_id: str,
    name: str,
    node_type: str,
    aliases: list[str],
    description: str,
    *,
    name_en: str,
    evidence: dict[str, object],
) -> dict[str, object]:
    return {
        "short_label": name,
        "aliases": aliases,
        **evidence,
        "description": description,
        "normalized_name": name,
        "kg_id": node_id,
        "type": node_type,
        "display_name": name,
        "entity_type": node_type,
        "name": name,
        "id": node_id,
        "review_status": "verified",
        "publication_status": "published",
        "name_en": name_en,
    }


def edge(
    edge_id: str,
    source: str,
    target: str,
    relation: str,
    label: str,
    *,
    evidence_reference: str,
    source_locator: str,
    evidence: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": edge_id,
        "source": source,
        "target": target,
        "relation": relation,
        "relation_label": label,
        "label_zh": label,
        "direction": "source_to_target",
        "evidence_status": "verified",
        "evidence_reference": evidence_reference,
        "source_locator": source_locator,
        **evidence,
    }
    payload["properties"] = {
        "kg_edge_id": edge_id,
        **evidence,
        "source_locator": source_locator,
        "source": source,
        "label": label,
        "target": target,
        "relation": relation,
        "evidence_status": "verified",
        "evidence_reference": evidence_reference,
        "id": edge_id,
    }
    return payload


NODES = [
    node(
        "ingredient_retinol",
        "視黃醇",
        "Ingredient",
        [
            "A 醇",
            "A醇",
            "Retinol",
            "retinol",
            "維生素 A 醇",
            "維生素A醇",
            "維他命A醇",
        ],
        "視黃醇是維生素 A 衍生的外用保養成分，常見於改善膚色不均與膚質的產品；初次使用可能出現乾燥或刺激。",
        name_en="Retinol",
        evidence=AAD_RETINOL,
    ),
    node(
        "function_support_surface_skin_renewal",
        "幫助表層肌膚更新",
        "Function",
        ["肌膚更新", "表層更新", "角質更新"],
        "視黃醇類成分可加快表層肌膚細胞更新；實際感受會因配方、用法與膚況而異。",
        name_en="Supports surface skin cell turnover",
        evidence=AAD_RETINOL,
    ),
    node(
        "warning_retinol_dryness_irritation",
        "可能乾燥或刺激",
        "SafetyWarning",
        ["A醇刺激", "視黃醇乾燥", "視黃醇脫皮", "retinol irritation"],
        "視黃醇類產品可能造成乾燥、泛紅或刺激；乾燥或敏感膚況使用前需特別留意。",
        name_en="Possible dryness or irritation from retinol",
        evidence=AAD_RETINOL,
    ),
    node(
        "concern_dry_flaky_skin",
        "乾燥脫皮",
        "SkinConcern",
        [
            "乾燥肌",
            "皮膚乾燥",
            "臉很乾",
            "臉好乾",
            "乾到脫皮",
            "缺水脫皮",
            "起皮",
            "脫屑",
            "皮膚緊繃",
            "洗完臉很緊繃",
        ],
        "皮膚感到緊繃、粗糙或出現脫屑的常見膚況；可先從溫和清潔與保濕照護開始。",
        name_en="Dry and flaky skin",
        evidence=AAD_DRY_SKIN,
    ),
    node(
        "care_gentle_fragrance_free_cleansing",
        "溫和無香精清潔",
        "CareNeed",
        ["溫和洗臉", "溫和潔面", "無香精清潔", "乾燥肌清潔"],
        "乾燥或敏弱時可選擇溫和、無香精的清潔產品，並避免過度清潔與搓揉。",
        name_en="Gentle fragrance-free cleansing",
        evidence=AAD_DRY_SKIN,
    ),
]


EDGES = [
    edge(
        "retinol-has-function-surface-renewal",
        "ingredient_retinol",
        "function_support_surface_skin_renewal",
        "HAS_FUNCTION",
        "主要作用",
        evidence_reference="AAD retinoid and retinol guidance",
        source_locator="AAD: retinoids spur faster turnover of surface skin cells",
        evidence=AAD_RETINOL,
    ),
    edge(
        "retinol-may-cause-dryness-irritation",
        "ingredient_retinol",
        "warning_retinol_dryness_irritation",
        "MAY_CAUSE",
        "注意事項",
        evidence_reference="AAD retinoid and retinol guidance",
        source_locator="AAD: dryness, redness, or inflammation can make retinoids unsuitable or irritating",
        evidence=AAD_RETINOL,
    ),
    edge(
        "retinol-support-with-hydration",
        "ingredient_retinol",
        "need_hydration",
        "SUPPORT_WITH",
        "可搭配",
        evidence_reference="AAD retinoid and retinol guidance",
        source_locator="AAD: starting slowly and using moisturizer can help mitigate irritation",
        evidence=AAD_RETINOL,
    ),
    edge(
        "dry-flaky-skin-needs-hydration",
        "concern_dry_flaky_skin",
        "need_hydration",
        "RELEVANT_TO_CARE",
        "照護方向",
        evidence_reference="AAD dry skin care guidance",
        source_locator="AAD: apply a cream or ointment when skin feels dry",
        evidence=AAD_DRY_SKIN,
    ),
    edge(
        "dry-flaky-skin-needs-gentle-cleansing",
        "concern_dry_flaky_skin",
        "care_gentle_fragrance_free_cleansing",
        "RELEVANT_TO_CARE",
        "照護方向",
        evidence_reference="AAD dry skin care guidance",
        source_locator="AAD: wash dry skin with a gentle, fragrance-free cleanser",
        evidence=AAD_DRY_SKIN,
    ),
    edge(
        "dry-flaky-skin-caution-retinol",
        "concern_dry_flaky_skin",
        "ingredient_retinol",
        "REQUIRES_CAUTION",
        "使用前留意",
        evidence_reference="AAD retinoid and retinol guidance",
        source_locator="AAD: people with dryness may not be good candidates for retinoids",
        evidence=AAD_RETINOL,
    ),
]


def upsert(items: list[dict[str, object]], additions: list[dict[str, object]]) -> int:
    by_id = {str(item.get("id") or item.get("kg_id") or ""): index for index, item in enumerate(items)}
    changed = 0
    for addition in additions:
        item_id = str(addition["id"])
        if item_id in by_id:
            if items[by_id[item_id]] != addition:
                items[by_id[item_id]] = addition
                changed += 1
        else:
            items.append(addition)
            by_id[item_id] = len(items) - 1
            changed += 1
    return changed


def main() -> None:
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    if not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise RuntimeError("unexpected knowledge map schema")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"knowledge-map-before-common-gaps-{stamp}.json"
    shutil.copy2(GRAPH_PATH, backup)

    changed_nodes = upsert(graph["nodes"], NODES)
    changed_edges = upsert(graph["edges"], EDGES)

    node_ids = {str(item.get("id") or item.get("kg_id") or "") for item in graph["nodes"]}
    missing = sorted(
        {
            str(edge_item[key])
            for edge_item in graph["edges"]
            for key in ("source", "target")
            if isinstance(edge_item.get(key), str) and edge_item[key] not in node_ids
        }
    )
    if missing:
        raise RuntimeError(f"graph contains dangling references: {missing}")

    GRAPH_PATH.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "backup": str(backup),
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]),
                "changed_nodes": changed_nodes,
                "changed_edges": changed_edges,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

import copy
import unittest

from app.repositories.curated_overlay_query_repository import (
    CuratedOverlayQueryRepository,
    OverlayFirstCuratedRepository,
)
from scripts.migrate_knowledge_seed_v1 import PlanError


CORE_VIEWS = [
    ("酒糟肌", "concern_rosacea", "SkinConcern", ["edge-rosacea-symptom"], []),
    ("泛紅", "symptom_redness", "Symptom", ["edge-redness-warning"], []),
    ("保濕", "need_hydration", "CareNeed", ["edge-glycerin-hydration"], []),
    ("玻尿酸", "ingredient_hyaluronic_acid", "Ingredient", ["edge-ha-function"], []),
    (
        "A酸", "alias_a_acid", "AliasTerm",
        ["edge-a-tretinoin", "edge-a-isotretinoin"],
        ["edge-isotretinoin-warning"],
    ),
    ("皮爽", "alias_pishuang", "AliasTerm", ["edge-pishuang-product"], []),
]

EDGE_ENDPOINTS = {
    "edge-rosacea-symptom": ("concern_rosacea", "symptom_redness"),
    "edge-redness-warning": ("symptom_redness", "warning_not_diagnosis"),
    "edge-glycerin-hydration": ("ingredient_glycerin", "need_hydration"),
    "edge-ha-function": ("ingredient_hyaluronic_acid", "function_hydration"),
    "edge-a-tretinoin": ("alias_a_acid", "drug_tretinoin"),
    "edge-a-isotretinoin": ("alias_a_acid", "drug_isotretinoin"),
    "edge-isotretinoin-warning": ("drug_isotretinoin", "warning_pregnancy"),
    "edge-pishuang-product": ("alias_pishuang", "drug_product_pishuang"),
}


def _node(node_id, node_type, name):
    return {
        "id": node_id, "type": node_type, "name": name,
        "source_ids": ["source-reviewed"], "identity_status": "verified-node",
    }


def _edge(edge_id, source, target, relation):
    return {
        "id": edge_id, "source": source, "target": target, "relation": relation,
        "source_ids": ["source-reviewed"], "evidence_status": "reviewed",
        "evidence_reference": f"reviewed claim {edge_id}",
        "source_locator": f"section/{edge_id}", "reviewed_at": "2026-10-10",
        "label": relation.lower().replace("_", " "),
    }


def make_dataset():
    nodes = [
        _node("concern_rosacea", "SkinConcern", "酒糟性皮膚炎"),
        _node("symptom_redness", "Symptom", "泛紅"),
        _node("warning_not_diagnosis", "SafetyWarning", "不可取代診斷"),
        _node("need_hydration", "CareNeed", "保濕"),
        _node("ingredient_glycerin", "Ingredient", "甘油"),
        _node("ingredient_hyaluronic_acid", "Ingredient", "玻尿酸"),
        _node("function_hydration", "Function", "保水"),
        _node("alias_a_acid", "AliasTerm", "A酸"),
        _node("drug_tretinoin", "DrugActiveIngredient", "Tretinoin"),
        _node("drug_isotretinoin", "DrugActiveIngredient", "Isotretinoin"),
        _node("warning_pregnancy", "SafetyWarning", "孕期警語"),
        _node("alias_pishuang", "AliasTerm", "皮爽"),
        _node("drug_product_pishuang", "DrugProduct", "皮爽藥品"),
    ]
    edges = [
        _edge("edge-rosacea-symptom", "concern_rosacea", "symptom_redness", "HAS_SYMPTOM"),
        _edge("edge-redness-warning", "symptom_redness", "warning_not_diagnosis", "REQUIRES_CAUTION"),
        _edge("edge-glycerin-hydration", "ingredient_glycerin", "need_hydration", "SUPPORTS_NEED"),
        _edge("edge-ha-function", "ingredient_hyaluronic_acid", "function_hydration", "HAS_FUNCTION"),
        _edge("edge-a-tretinoin", "alias_a_acid", "drug_tretinoin", "MAY_REFER_TO"),
        _edge("edge-a-isotretinoin", "alias_a_acid", "drug_isotretinoin", "MAY_REFER_TO"),
        _edge("edge-isotretinoin-warning", "drug_isotretinoin", "warning_pregnancy", "REQUIRES_CAUTION"),
        _edge("edge-pishuang-product", "alias_pishuang", "drug_product_pishuang", "MAY_REFER_TO"),
    ]
    return {
        "metadata": {"dataset_id": "query-test", "version": "1"},
        "sources": [{
            "id": "source-reviewed", "title": "Reviewed medical source",
            "url": "https://example.invalid/reviewed", "review_status": "verified-source",
        }],
        "nodes": nodes, "edges": edges,
    }


def make_overlay():
    visible_edges = [
        edge_id for _, _, _, initial, drilldown in CORE_VIEWS
        for edge_id in [*initial, *drilldown]
    ]
    views = []
    for query_key, center_id, center_type, initial, drilldown in CORE_VIEWS:
        all_nodes, initial_nodes = {center_id}, {center_id}
        for edge_id in [*initial, *drilldown]:
            all_nodes.update(EDGE_ENDPOINTS[edge_id])
        for edge_id in initial:
            initial_nodes.update(EDGE_ENDPOINTS[edge_id])
        views.append({
            "query_key": query_key, "center_id": center_id, "center_type": center_type,
            "edge_ids": initial, "drilldown_edge_ids": drilldown,
            "visible_edge_count": len(initial) + len(drilldown),
            "visible_node_count": len(all_nodes),
            "initial_visible_edge_count": len(initial),
            "initial_visible_node_count": len(initial_nodes),
            "initial_edge_limit_desktop": len(initial),
            "initial_edge_limit_mobile": min(2, len(initial)),
        })
    return {
        "overlay_id": "query-overlay-test", "version": "query-overlay-test",
        "user_visible_center_ids": [center_id for _, center_id, _, _, _ in CORE_VIEWS],
        "user_visible_edge_ids": visible_edges,
        "allowed_relations": [
            "HAS_FUNCTION", "HAS_SYMPTOM", "MAY_REFER_TO",
            "REQUIRES_CAUTION", "SUPPORTS_NEED",
        ],
        "approved_evidence_statuses": ["reviewed"], "query_views": views,
    }


class CuratedOverlayQueryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.dataset, self.overlay = make_dataset(), make_overlay()

    def test_all_six_core_terms_resolve_only_through_declared_exact_views(self):
        repository = CuratedOverlayQueryRepository(self.dataset, self.overlay)
        for query_key, center_id, center_type, _, _ in CORE_VIEWS:
            graph = repository.explore(query_key)
            self.assertIsNotNone(graph)
            self.assertEqual(graph["center"]["id"], center_id)
            self.assertEqual(graph["center"]["type"], center_type)
            self.assertEqual(graph["resolution"]["strategy"], "curated-overlay-exact")
            self.assertEqual(graph["resolution"]["canonical_node_id"], center_id)
            self.assertEqual(
                set(graph), {"center", "nodes", "edges", "resolution", "evidence", "view"}
            )
        self.assertIsNone(repository.explore("刺痛"))
        self.assertIsNone(repository.explore("保濕劑"))
        self.assertEqual(repository.explore("  Ａ 酸 ")["center"]["id"], "alias_a_acid")

    def test_query_view_edge_order_and_drilldown_are_preserved(self):
        graph = CuratedOverlayQueryRepository(self.dataset, self.overlay).explore("A酸")
        self.assertEqual([edge["id"] for edge in graph["edges"]], [
            "edge-a-tretinoin", "edge-a-isotretinoin", "edge-isotretinoin-warning",
        ])
        self.assertEqual(
            graph["view"]["initial_edge_ids"], ["edge-a-tretinoin", "edge-a-isotretinoin"]
        )
        self.assertEqual(
            graph["view"]["drilldown_edge_ids"], ["edge-isotretinoin-warning"]
        )
        self.assertEqual(graph["edges"][-1]["direction_from_center"], "connected")

    def test_evidence_contract_is_flat_and_nested_for_api_compatibility(self):
        graph = CuratedOverlayQueryRepository(self.dataset, self.overlay).explore("玻尿酸")
        edge = graph["edges"][0]
        self.assertEqual(edge["source_id"], "ingredient_hyaluronic_acid")
        self.assertEqual(edge["target_id"], "function_hydration")
        self.assertEqual(edge["evidence_status"], "reviewed")
        self.assertEqual(edge["source_locator"], "section/edge-ha-function")
        self.assertEqual(edge["reviewed_at"], "2026-10-10")
        self.assertEqual(edge["evidence"]["source_ids"], ["source-reviewed"])
        self.assertEqual(graph["evidence"]["gate"], "migrate_knowledge_seed_v1.build_plan")
        self.assertTrue(graph["evidence"]["all_edges_have_source_locator"])
        self.assertEqual(graph["evidence"]["sources"][0]["title"], "Reviewed medical source")

    def test_build_plan_gate_rejects_needs_review_view_edge(self):
        unsafe = copy.deepcopy(self.dataset)
        unsafe["edges"][0]["evidence_status"] = "needs-review"
        with self.assertRaisesRegex(PlanError, "did not pass the public plan gate"):
            CuratedOverlayQueryRepository(unsafe, self.overlay)

    def test_callers_cannot_mutate_repository_state(self):
        repository = CuratedOverlayQueryRepository(self.dataset, self.overlay)
        graph = repository.explore("皮爽")
        graph["center"]["name"] = "mutated"
        graph["edges"][0]["properties"]["label"] = "mutated"
        again = repository.explore("皮爽")
        self.assertEqual(again["center"]["name"], "皮爽")
        self.assertNotEqual(again["edges"][0]["properties"]["label"], "mutated")

    def test_overlay_adapter_does_not_touch_database_for_exact_core_term(self):
        class Fallback:
            calls = 0

            def curated_explore(self, query, *, limit):
                self.calls += 1
                return {"center": {"id": "database"}}

        repository = CuratedOverlayQueryRepository(self.dataset, self.overlay)
        fallback = Fallback()
        adapter = OverlayFirstCuratedRepository(repository, fallback)

        result = adapter.curated_explore("酒糟肌", limit=60)

        self.assertEqual(result["center"]["id"], "concern_rosacea")
        self.assertEqual(fallback.calls, 0)

    def test_non_center_node_neighborhood_uses_only_reviewed_overlay_edges(self):
        repository = CuratedOverlayQueryRepository(self.dataset, self.overlay)

        graph = repository.neighborhood("warning_not_diagnosis")

        self.assertIsNotNone(graph)
        self.assertEqual(graph["center"]["id"], "warning_not_diagnosis")
        self.assertEqual(graph["resolution"]["strategy"], "curated-overlay-node-id")
        self.assertEqual(
            [edge["id"] for edge in graph["edges"]],
            ["edge-redness-warning"],
        )
        self.assertTrue(
            all(edge["evidence_status"] == "reviewed" for edge in graph["edges"])
        )
        self.assertTrue(
            all(edge["source_locator"] for edge in graph["edges"])
        )

    def test_overlay_adapter_does_not_fall_back_for_visible_non_center_node(self):
        class Fallback:
            calls = 0

            def curated_neighborhood(self, node_id, *, limit):
                self.calls += 1
                return {"center": {"id": "database"}}

        repository = CuratedOverlayQueryRepository(self.dataset, self.overlay)
        fallback = Fallback()
        adapter = OverlayFirstCuratedRepository(repository, fallback)

        result = adapter.curated_neighborhood("warning_not_diagnosis", limit=12)

        self.assertEqual(result["center"]["id"], "warning_not_diagnosis")
        self.assertEqual(fallback.calls, 0)

    def test_overlay_adapter_falls_back_only_for_unknown_exact_term(self):
        class Fallback:
            def __init__(self):
                self.calls = []

            def curated_search(self, query, *, limit):
                self.calls.append((query, limit))
                return [{"id": "legacy-result"}]

            def close(self):
                return "closed"

        repository = CuratedOverlayQueryRepository(self.dataset, self.overlay)
        fallback = Fallback()
        adapter = OverlayFirstCuratedRepository(repository, fallback)

        self.assertEqual(
            adapter.curated_search("未知名詞", limit=7),
            [{"id": "legacy-result"}],
        )
        self.assertEqual(fallback.calls, [("未知名詞", 7)])
        self.assertEqual(adapter.close(), "closed")


if __name__ == "__main__":
    unittest.main()

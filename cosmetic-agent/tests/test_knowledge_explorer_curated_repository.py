from app.repositories.knowledge_explorer_repository import KnowledgeExplorerRepository
from app.services.knowledge_explorer_service import KnowledgeExplorerService


class _Rows(list):
    def single(self):
        return self[0] if self else None


class _Session:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        if "RETURN p AS properties" in query:
            return _Rows([{
                "properties": {
                    "id": "alias_query_a_acid", "kg_id": "alias_query_a_acid",
                    "name": "A酸", "type": "AliasTerm", "aliases": ["A 酸"],
                }
            }])
        if "RETURN properties(n) AS properties" in query:
            return _Rows([{
                "properties": {
                    "id": "alias_query_a_acid", "kg_id": "alias_query_a_acid",
                    "name": "A酸", "type": "AliasTerm", "aliases": ["A 酸"],
                }
            }])
        return _Rows([{
            "neighbor_properties": {
                "id": "drug_active_tretinoin", "kg_id": "drug_active_tretinoin",
                "name": "Tretinoin", "type": "DrugActiveIngredient",
            },
            "source_id": "alias_query_a_acid",
            "target_id": "drug_active_tretinoin",
            "relation": "MAY_REFER_TO",
            "relation_properties": {
                "kg_edge_id": "curated_a_acid_tretinoin",
                "label": "可能指的是",
                "evidence_status": "verified",
                "evidence_reference": "reviewed claim",
                "source_ids": ["src-medical"],
                "source_locator": "section A",
                "reviewed_at": "2026-10-10",
            },
        }])


class _Graph:
    def __init__(self):
        self.fake_session = _Session()

    def session(self):
        return self.fake_session

    def close(self):
        return None


def test_exact_term_reads_only_public_blinkkg_namespace_and_preserves_contract():
    graph = _Graph()
    repository = KnowledgeExplorerRepository(graph_repository=graph)
    result = repository.curated_explore("A酸", limit=8)

    assert result["center"]["id"] == "alias_query_a_acid"
    assert result["center"]["type"] == "AliasTerm"
    assert result["resolution"]["strategy"] == "curated-exact"
    assert result["edges"] == [{
        "id": "curated_a_acid_tretinoin",
        "source": "alias_query_a_acid",
        "target": "drug_active_tretinoin",
        "source_id": "alias_query_a_acid",
        "target_id": "drug_active_tretinoin",
        "relation": "MAY_REFER_TO",
        "relation_label": "可能指的是",
        "label_zh": "可能指的是",
        "direction": "source_to_target",
        "evidence_status": "verified",
        "evidence_reference": "reviewed claim",
        "source_ids": ["src-medical"],
        "source_locator": "section A",
        "reviewed_at": "2026-10-10",
        "properties": {
            "kg_edge_id": "curated_a_acid_tretinoin",
            "label": "可能指的是",
            "evidence_status": "verified",
            "evidence_reference": "reviewed claim",
            "source_ids": ["src-medical"],
            "source_locator": "section A",
            "reviewed_at": "2026-10-10",
        },
    }]
    assert all("MATCH (n:BlinkKGv1)" in query for query, _ in graph.fake_session.queries)
    search_query = next(
        query for query, _ in graph.fake_session.queries if "any(value IN names" in query
    )
    assert "WITH n, properties(n) AS p\n        WITH n, p," in search_query
    assert "NOT m:EvidenceSource" in graph.fake_session.queries[-1][0]


class _CuratedRepository:
    def curated_explore(self, query, *, limit):
        return {"center": {"id": "curated", "type": "Ingredient", "name": query},
                "nodes": [], "edges": []}

    def curated_neighborhood(self, node_id, *, limit):
        return {"center": {"id": node_id, "type": "Ingredient", "name": "curated"},
                "nodes": [], "edges": []}


class _SeedMustNotRun:
    def explore(self, *args, **kwargs):
        raise AssertionError("seed fallback must not run after a curated match")

    def neighborhood(self, *args, **kwargs):
        raise AssertionError("seed fallback must not run after a curated match")


def test_service_prefers_curated_namespace_without_touching_chat_path():
    service = KnowledgeExplorerService(
        repository=_CuratedRepository(), seed_repository=_SeedMustNotRun()
    )
    assert service.explore("玻尿酸")["center"]["id"] == "curated"
    assert service.neighborhood("ingredient_hyaluronic_acid")["center"]["id"] == (
        "ingredient_hyaluronic_acid"
    )

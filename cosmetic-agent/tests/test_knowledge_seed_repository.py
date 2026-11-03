import pytest

from app.repositories.knowledge_seed_repository import KnowledgeSeedRepository


@pytest.fixture(scope="module")
def repository():
    repository = KnowledgeSeedRepository()
    if not repository.status().get("available"):
        pytest.skip(
            "optional local knowledge seed overlay is not installed; "
            "the live cloud graph is validated by integration tests"
        )
    return repository


def test_seed_has_connected_v1_core(repository):
    status = repository.status()
    assert status["node_types"]["Ingredient"] == 100
    assert status["node_types"]["SkinConcern"] == 47
    assert status["node_types"]["CareNeed"] == 12
    assert status["node_count"] >= 400
    assert status["edge_count"] >= 1_000


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("酒糟肌", "concern_rosacea"),
        ("玫瑰痤瘡", "concern_rosacea"),
        ("我有酒糟肌", "concern_rosacea"),
        ("痘痘", "concern_acne"),
        ("敏感肌", "concern_sensitive_skin"),
        ("rosacea", "concern_rosacea"),
    ],
)
def test_aliases_resolve_to_canonical_concern(repository, query, expected_id):
    matches = repository.search(query, limit=3)
    assert matches
    assert matches[0]["id"] == expected_id


def test_rosacea_explore_returns_real_two_hop_graph(repository):
    graph = repository.explore("酒糟肌", depth=2, limit=120)
    assert graph is not None
    assert graph["center"]["id"] == "concern_rosacea"
    assert graph["resolution"]["matched_alias"] == "酒糟肌"
    node_ids = {node["id"] for node in graph["nodes"]}
    assert "ingredient_azelaic_acid" in node_ids
    assert "function_soothing" in node_ids
    assert "concern_sensitive_skin" in node_ids
    assert any(
        edge["source"] == "ingredient_azelaic_acid"
        and edge["target"] == "concern_rosacea"
        and edge["relation"] == "SUITABLE_FOR_CONCERN"
        for edge in graph["edges"]
    )


def test_every_returned_edge_is_traceable(repository):
    graph = repository.explore("酒糟肌", depth=2, limit=120)
    assert graph is not None
    assert graph["edges"]
    for edge in graph["edges"]:
        assert edge.get("evidence_status")
        assert edge.get("source_ids")


def test_drug_slang_resolves_without_conflating_retinoids(repository):
    assert repository.search("A酸", limit=1)[0]["id"] == "drug_active_tretinoin"
    assert repository.search("A醇", limit=1)[0]["id"] == "ingredient_retinol"


def test_ingredient_projection_is_grouped_and_mobile_bounded(repository):
    graph = repository.neighborhood("ingredient_azelaic_acid", depth=2, limit=120)
    assert graph is not None
    groups = {group["id"]: group for group in graph["groups"]}
    assert "aliases" in groups
    assert "products" in groups
    assert "functions" in groups
    assert all(len(group["preview_node_ids"]) <= 5 for group in graph["groups"])
    node_types = {node["type"] for node in graph["nodes"]}
    assert "DrugProduct" in node_types
    assert "Manufacturer" in node_types


def test_hydration_need_resolves_to_bidirectional_ingredient_graph(repository):
    graph = repository.explore("保濕", depth=1, limit=120)
    assert graph is not None
    assert graph["center"]["id"] == "need_hydration"
    node_ids = {node["id"] for node in graph["nodes"]}
    assert "ingredient_glycerin" in node_ids
    assert "ingredient_hyaluronic_acid" in node_ids
    assert any(edge["relation"] == "NAVIGATES_TO_INGREDIENT" for edge in graph["edges"])
    reverse = repository.neighborhood("ingredient_glycerin", depth=1, limit=120)
    assert reverse is not None
    assert "need_hydration" in {node["id"] for node in reverse["nodes"]}


def test_pishuang_is_ambiguous_product_query_not_betamethasone_alias(repository):
    graph = repository.explore("皮爽", depth=1, limit=60)
    assert graph is not None
    assert graph["center"]["id"] == "alias_query_pishuang"
    products = {node["id"]: node for node in graph["nodes"] if node["type"] == "DrugProduct"}
    assert len(products) == 4
    assert any("怡皮爽" in node["name"] for node in products.values())
    assert any("皮爽能" in node["name"] for node in products.values())
    assert all(edge["relation"] == "MAY_REFER_TO" for edge in graph["edges"])


def test_legacy_hard_conflicts_are_not_exposed_as_facts(repository):
    assert not any(edge["relation"] == "CONFLICTS_WITH" for edge in repository.edges)
    assert not any(edge["source"] == edge["target"] for edge in repository.edges)

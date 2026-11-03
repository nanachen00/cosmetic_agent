import importlib.util
import json
import sys
from pathlib import Path

import pytest


REMOTE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/migrate_knowledge_seed_v1.py"
SCRIPT = REMOTE_SCRIPT if REMOTE_SCRIPT.exists() else Path(__file__).with_name("migrate_knowledge_seed_v1.py")
SPEC = importlib.util.spec_from_file_location("migrate_knowledge_seed_v1", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def sample_dataset():
    return {
        "metadata": {"dataset_id": "test", "version": "1"},
        "sources": [{"id": "source-1", "title": "Reviewed source"}],
        "nodes": [
            {"id": "ingredient-1", "type": "Ingredient", "name": "成分", "source_ids": ["source-1"]},
            {"id": "concern-1", "type": "SkinConcern", "name": "膚況", "source_ids": ["source-1"]},
        ],
        "edges": [{
            "id": "edge-1", "source": "ingredient-1", "target": "concern-1",
            "relation": "RELEVANT_TO_CARE", "source_ids": ["source-1"],
            "evidence_status": "source-linked",
            "source_locator": "section 1", "reviewed_at": "2026-10-10",
        }],
    }


def sample_overlay():
    return {
        "version": "test",
        "user_visible_center_ids": ["ingredient-1"],
        "user_visible_edge_ids": ["edge-1"],
        "allowed_relations": ["RELEVANT_TO_CARE"],
        "approved_evidence_statuses": ["source-linked"],
    }


def sample_overlay_with_claim(*, status="source-linked", source_ids=None, target="concern-1"):
    overlay = sample_overlay()
    overlay["additional_nodes"] = [{
        "id": "ingredient-2", "type": "Ingredient", "name": "新增成分",
        "source_ids": ["source-1"], "review_status": "reviewed",
    }]
    overlay["additional_edges"] = [{
        "id": "edge-overlay-1", "source": "ingredient-2", "target": target,
        "relation": "RELEVANT_TO_CARE",
        "source_ids": ["source-1"] if source_ids is None else source_ids,
        "evidence_status": status,
        "source_locator": "section 2", "reviewed_at": "2026-10-10",
    }]
    overlay["user_visible_edge_ids"].append("edge-overlay-1")
    return overlay


def test_build_plan_is_deterministic_and_namespaced():
    plan = MODULE.build_plan(sample_dataset(), overlay=sample_overlay())
    assert plan.migration_id == "test:1:test"
    assert len(plan.nodes_by_label["Ingredient"]) == 1
    assert len(plan.nodes_by_label["SkinConcern"]) == 1
    assert len(plan.nodes_by_label["EvidenceSource"]) == 1
    assert plan.edges_by_type["RELEVANT_TO_CARE"][0]["id"] == "edge-1"
    assert len(plan.edges_by_type["SUPPORTED_BY"]) == 2


def test_missing_evidence_is_rejected():
    dataset = sample_dataset()
    dataset["edges"][0]["source_ids"] = []
    with pytest.raises(MODULE.PlanError, match="source/evidence/review"):
        MODULE.build_plan(dataset, overlay=sample_overlay())


def test_unknown_source_is_rejected():
    dataset = sample_dataset()
    dataset["nodes"][0]["source_ids"] = ["missing"]
    with pytest.raises(MODULE.PlanError, match="unknown sources"):
        MODULE.build_plan(dataset, overlay=sample_overlay())


def test_production_target_is_rejected():
    with pytest.raises(MODULE.PlanError, match="production"):
        MODULE.assert_write_target(
            uri="neo4j+s://prod.example", database="neo4j",
            acknowledgement="prod.example/neo4j", production_uri="neo4j+s://prod.example",
            production_database="neo4j",
        )


def test_acknowledgement_must_match_target():
    with pytest.raises(MODULE.PlanError, match="acknowledgement"):
        MODULE.assert_write_target(
            uri="neo4j://test.example", database="blink_test",
            acknowledgement="wrong.example/blink_test", production_uri="neo4j://prod.example",
            production_database="neo4j",
        )


def test_missing_production_identity_is_rejected():
    with pytest.raises(MODULE.PlanError, match="production Neo4j identity"):
        MODULE.assert_write_target(
            uri="neo4j://test.example", database="blink_test",
            acknowledgement="test.example/blink_test", production_uri=None,
            production_database=None,
        )


def test_dry_run_does_not_require_neo4j_credentials(tmp_path, capsys):
    seed = tmp_path / "seed.json"
    overlay = tmp_path / "overlay.json"
    seed.write_text(json.dumps(sample_dataset()), encoding="utf-8")
    overlay.write_text(json.dumps(sample_overlay()), encoding="utf-8")
    assert MODULE.main(["--seed", str(seed), "--overlay", str(overlay)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry-run"
    assert output["planned_nodes"] == 3
    assert output["rollback_manifest"]["migration_id"] == "test:1:test"


def test_needs_review_is_not_planned_as_public_relation():
    dataset = sample_dataset()
    dataset["edges"][0]["evidence_status"] = "needs-review"
    plan = MODULE.build_plan(dataset, overlay=sample_overlay())
    assert "RELEVANT_TO_CARE" not in plan.edges_by_type
    assert plan.summary["skipped_evidence_statuses"] == {"needs-review": 1}


def test_center_does_not_implicitly_publish_adjacent_approved_edges():
    dataset = sample_dataset()
    dataset["edges"].append({
        "id": "edge-not-curated", "source": "ingredient-1", "target": "concern-1",
        "relation": "RELEVANT_TO_CARE", "source_ids": ["source-1"],
        "evidence_status": "source-linked",
    })
    plan = MODULE.build_plan(dataset, overlay=sample_overlay())
    assert [row["id"] for row in plan.edges_by_type["RELEVANT_TO_CARE"]] == ["edge-1"]
    assert plan.summary["skipped_reasons"]["not-user-visible"] == 1


def test_concern_category_is_not_mislabeled_as_ingredient_category():
    dataset = sample_dataset()
    dataset["nodes"].append({
        "id": "concern-category-1", "type": "ConcernCategory", "name": "膚況分類",
        "source_ids": ["source-1"],
    })
    dataset["edges"].append({
        "id": "edge-category", "source": "concern-1", "target": "concern-category-1",
        "relation": "BELONGS_TO_CATEGORY", "source_ids": ["source-1"],
        "evidence_status": "source-linked",
        "source_locator": "section 3", "reviewed_at": "2026-10-10",
    })
    overlay = sample_overlay()
    overlay["user_visible_edge_ids"].append("edge-category")
    overlay["allowed_relations"].append("BELONGS_TO_CATEGORY")
    plan = MODULE.build_plan(dataset, overlay=overlay)
    assert [row["id"] for row in plan.nodes_by_label["ConcernCategory"]] == ["concern-category-1"]
    assert "IngredientCategory" not in plan.nodes_by_label
    assert "IngredientCategory" in plan.summary["missing_public_labels"]


def test_overlay_additional_node_and_edge_can_be_planned():
    plan = MODULE.build_plan(sample_dataset(), overlay=sample_overlay_with_claim())
    assert [row["id"] for row in plan.nodes_by_label["Ingredient"]] == ["ingredient-1", "ingredient-2"]
    assert [row["id"] for row in plan.edges_by_type["RELEVANT_TO_CARE"]] == ["edge-1", "edge-overlay-1"]
    assert plan.summary["overlay_additional_nodes"] == 1
    assert plan.summary["overlay_additional_edges"] == 1


def test_additional_source_is_merged_before_claim_validation():
    overlay = sample_overlay_with_claim(source_ids=["overlay-source"])
    overlay["additional_nodes"][0]["source_ids"] = ["overlay-source"]
    overlay["additional_sources"] = [{
        "id": "overlay-source", "title": "Overlay reviewed source",
        "url": "https://example.invalid/reviewed", "review_status": "verified-source",
    }]
    plan = MODULE.build_plan(sample_dataset(), overlay=overlay)
    assert plan.summary["overlay_additional_sources"] == 1
    assert any(row["id"] == "evidence_source:overlay-source" for row in plan.nodes_by_label["EvidenceSource"])


@pytest.mark.parametrize("overlay,match", [
    (sample_overlay_with_claim(target="missing-node"), "missing endpoint"),
    (sample_overlay_with_claim(source_ids=["missing-source"]), "unknown sources"),
])
def test_overlay_claim_rejects_unknown_endpoint_or_source(overlay, match):
    with pytest.raises(MODULE.PlanError, match=match):
        MODULE.build_plan(sample_dataset(), overlay=overlay)


def test_overlay_records_cannot_overwrite_seed_ids():
    overlay = sample_overlay_with_claim()
    overlay["additional_nodes"][0]["id"] = "ingredient-1"
    with pytest.raises(MODULE.PlanError, match="duplicate node"):
        MODULE.build_plan(sample_dataset(), overlay=overlay)


def test_overlay_needs_review_claim_is_not_public():
    plan = MODULE.build_plan(sample_dataset(), overlay=sample_overlay_with_claim(status="needs-review"))
    assert [row["id"] for row in plan.edges_by_type["RELEVANT_TO_CARE"]] == ["edge-1"]
    assert plan.summary["skipped_evidence_statuses"] == {"needs-review": 1}


def test_safe_existing_node_override_changes_only_public_projection():
    dataset = sample_dataset()
    dataset["nodes"][1]["aliases"] = ["old", "remove-me"]
    overlay = sample_overlay()
    overlay["existing_node_overrides"] = [{
        "id": "concern-1", "expected_seed_type": "SkinConcern",
        "public_type": "Symptom", "public_name_zh_tw": "公開症狀",
        "aliases_add": ["new"], "aliases_remove": ["remove-me"],
    }]
    plan = MODULE.build_plan(dataset, overlay=overlay)
    row = plan.nodes_by_label["Symptom"][0]
    assert row["props"]["display_name"] == "公開症狀"
    assert row["props"]["entity_type"] == "Symptom"
    assert row["props"]["aliases"] == ["old", "new"]
    assert dataset["nodes"][1]["type"] == "SkinConcern"


def test_existing_node_override_requires_exact_seed_type():
    overlay = sample_overlay()
    overlay["existing_node_overrides"] = [{
        "id": "concern-1", "expected_seed_type": "Ingredient",
        "public_type": "Symptom", "aliases_add": [], "aliases_remove": [],
    }]
    with pytest.raises(MODULE.PlanError, match="expected seed type"):
        MODULE.build_plan(sample_dataset(), overlay=overlay)


def test_edge_preserves_claim_locator_and_review_date():
    dataset = sample_dataset()
    dataset["edges"][0]["source_locator"] = "section 2"
    dataset["edges"][0]["reviewed_at"] = "2026-10-10"
    plan = MODULE.build_plan(dataset, overlay=sample_overlay())
    props = plan.edges_by_type["RELEVANT_TO_CARE"][0]["props"]
    assert props["source_locator"] == "section 2"
    assert props["reviewed_at"] == "2026-10-10"


def test_overlay_identity_changes_default_migration_id():
    one = sample_overlay()
    two = sample_overlay()
    two["version"] = "test-2"
    assert MODULE.build_plan(sample_dataset(), overlay=one).migration_id != MODULE.build_plan(
        sample_dataset(), overlay=two
    ).migration_id


def test_existing_edge_override_requires_exact_identity_and_supplies_claim_fields():
    dataset = sample_dataset()
    dataset["edges"][0].pop("source_locator")
    dataset["edges"][0].pop("reviewed_at")
    overlay = sample_overlay()
    overlay["existing_edge_overrides"] = [{
        "id": "edge-1", "expected_relation": "RELEVANT_TO_CARE",
        "expected_source": "ingredient-1", "expected_target": "concern-1",
        "expected_evidence_status": "source-linked",
        "expected_source_ids": ["source-1"], "source_ids": ["source-1"],
        "source_locator": "reviewed section", "reviewed_at": "2026-10-10",
    }]
    plan = MODULE.build_plan(dataset, overlay=overlay)
    assert plan.edges_by_type["RELEVANT_TO_CARE"][0]["props"]["source_locator"] == "reviewed section"
    overlay["existing_edge_overrides"][0]["expected_target"] = "wrong"
    with pytest.raises(MODULE.PlanError, match="expected identity"):
        MODULE.build_plan(dataset, overlay=overlay)


def test_existing_edge_override_cannot_replace_source_identity():
    overlay = sample_overlay()
    overlay["existing_edge_overrides"] = [{
        "id": "edge-1", "expected_relation": "RELEVANT_TO_CARE",
        "expected_source": "ingredient-1", "expected_target": "concern-1",
        "source_ids": ["another-source"],
    }]
    with pytest.raises(MODULE.PlanError, match="cannot replace source ids"):
        MODULE.build_plan(sample_dataset(), overlay=overlay)


def test_public_edge_requires_claim_locator_and_review_date():
    dataset = sample_dataset()
    dataset["edges"][0].pop("source_locator")
    with pytest.raises(MODULE.PlanError, match="missing source_locator"):
        MODULE.build_plan(dataset, overlay=sample_overlay())


def test_missing_user_visible_edge_is_rejected():
    overlay = sample_overlay()
    overlay["user_visible_edge_ids"].append("missing-edge")
    with pytest.raises(MODULE.PlanError, match="missing user-visible edges"):
        MODULE.build_plan(sample_dataset(), overlay=overlay)


def test_query_view_ids_and_counts_are_validated():
    overlay = sample_overlay()
    overlay["query_views"] = [{
        "query_key": "測試", "center_id": "ingredient-1",
        "visible_node_count": 2, "visible_edge_count": 1, "edge_ids": ["edge-1"],
    }]
    MODULE.build_plan(sample_dataset(), overlay=overlay)
    overlay["query_views"][0]["visible_edge_count"] = 2
    with pytest.raises(MODULE.PlanError, match="visible_edge_count is inconsistent"):
        MODULE.build_plan(sample_dataset(), overlay=overlay)


def test_dry_run_reports_overlay_counts(tmp_path, capsys):
    seed = tmp_path / "seed.json"
    overlay = tmp_path / "overlay.json"
    seed.write_text(json.dumps(sample_dataset()), encoding="utf-8")
    overlay.write_text(json.dumps(sample_overlay_with_claim()), encoding="utf-8")
    assert MODULE.main(["--seed", str(seed), "--overlay", str(overlay)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["overlay_additional_nodes"] == 1
    assert output["overlay_additional_edges"] == 1
    assert output["planned_nodes"] == 4


class _Result:
    def __init__(self, record=None):
        self.record = record

    def consume(self):
        return None

    def single(self):
        return self.record


class _FakeTx:
    def __init__(self, state):
        self.state = state

    def run(self, query, **params):
        rows = params.get("rows") or []
        migration_id = params.get("migration_id") or params.get("id")
        if "foreign_count" in query:
            count = sum(value != migration_id for value in self.state["nodes"].values())
            return _Result({"foreign_count": count})
        if "MERGE (n:BlinkKGv1" in query:
            for row in rows:
                self.state["nodes"][row["id"]] = migration_id
        elif "MERGE (s)-[r:" in query:
            for row in rows:
                self.state["edges"][row["id"]] = migration_id
        elif "DELETE r" in query:
            self.state["edges"] = {key: value for key, value in self.state["edges"].items() if value != migration_id}
        elif "DETACH DELETE n" in query:
            self.state["nodes"] = {key: value for key, value in self.state["nodes"].items() if value != migration_id}
        return _Result()


class _FakeSession:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute_write(self, callback):
        return callback(_FakeTx(self.state))

    def execute_read(self, callback):
        return callback(_FakeTx(self.state))


class _FakeDriver:
    def __init__(self):
        self.state = {"nodes": {"unrelated": "other"}, "edges": {"unrelated-edge": "other"}}

    def session(self, **_kwargs):
        return _FakeSession(self.state)


def test_second_apply_is_idempotent_and_rollback_is_scoped():
    plan = MODULE.build_plan(sample_dataset(), overlay=sample_overlay())
    driver = _FakeDriver()
    MODULE.apply_plan(driver, database="blink_test", plan=plan)
    first_counts = (len(driver.state["nodes"]), len(driver.state["edges"]))
    assert first_counts == (
        1 + sum(map(len, plan.nodes_by_label.values())),
        1 + sum(map(len, plan.edges_by_type.values())),
    )
    MODULE.apply_plan(driver, database="blink_test", plan=plan)
    assert (len(driver.state["nodes"]), len(driver.state["edges"])) == first_counts
    MODULE.rollback(driver, database="blink_test", migration_id=plan.migration_id)
    assert driver.state == {"nodes": {"unrelated": "other"}, "edges": {"unrelated-edge": "other"}}


def test_namespace_guard_rejects_a_different_migration():
    driver = _FakeDriver()
    with pytest.raises(MODULE.PlanError, match="different migration"):
        MODULE.assert_namespace_compatible(driver, database="blink_test", migration_id="new")
    driver.state["nodes"] = {"same": "new"}
    MODULE.assert_namespace_compatible(driver, database="blink_test", migration_id="new")

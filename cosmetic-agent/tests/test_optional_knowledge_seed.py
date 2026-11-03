from pathlib import Path

from app.repositories.knowledge_seed_repository import KnowledgeSeedRepository


def test_missing_seed_uses_empty_cloud_live_overlay(tmp_path: Path) -> None:
    repository = KnowledgeSeedRepository(tmp_path / "missing-seed.json")

    assert repository.search("玻尿酸") == []
    assert repository.explore("玻尿酸") is None
    assert repository.status() == {
        "dataset_id": None,
        "dataset_version": None,
        "node_count": 0,
        "edge_count": 0,
        "node_types": {},
        "mode": "cloud-live-only",
        "available": False,
    }

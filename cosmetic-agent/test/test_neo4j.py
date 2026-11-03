from __future__ import annotations

import os

import pytest
from neo4j import GraphDatabase


def test_neo4j_cloud_connectivity_from_environment() -> None:
    uri = os.getenv("NEO4J_URI", "").strip()
    user = os.getenv("NEO4J_USER", "").strip()
    password = os.getenv("NEO4J_PASSWORD", "").strip()
    if not uri or not user or not password:
        pytest.skip("Neo4j integration configuration is unavailable")

    database = os.getenv("NEO4J_DATABASE", "").strip() or None
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            result = session.run("RETURN 1 AS ok").single()
    assert result["ok"] == 1

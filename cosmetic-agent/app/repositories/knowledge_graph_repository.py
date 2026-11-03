"""Shared Neo4j (Aura) driver boundary for knowledge-graph reads.

RECONSTRUCTED 2026-10-21 from agents/knowledge/knowledge_sub_agent.py and
app/repositories/knowledge_explorer_repository.py. Connection values come only
from NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterator


class KnowledgeGraphConfigurationError(RuntimeError):
    pass


@dataclass
class KnowledgeGraphRepository:
    uri: str
    user: str
    password: str = field(repr=False)
    database: str | None = None
    _driver: Any = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "KnowledgeGraphRepository":
        uri = os.getenv("NEO4J_URI", "").strip()
        user = os.getenv("NEO4J_USER", "").strip()
        password = os.getenv("NEO4J_PASSWORD", "").strip()
        missing = [
            name
            for name, value in (("NEO4J_URI", uri), ("NEO4J_USER", user), ("NEO4J_PASSWORD", password))
            if not value
        ]
        if missing:
            raise KnowledgeGraphConfigurationError("缺少 Neo4j 設定：" + ", ".join(missing))
        return cls(
            uri=uri,
            user=user,
            password=password,
            database=os.getenv("NEO4J_DATABASE", "").strip() or None,
        )

    @property
    def driver(self) -> Any:
        with self._lock:
            if self._driver is None:
                from neo4j import GraphDatabase

                self._driver = GraphDatabase.driver(
                    self.uri,
                    auth=(self.user, self.password),
                    max_connection_pool_size=int(os.getenv("NEO4J_MAX_POOL_SIZE", "50")),
                    connection_acquisition_timeout=float(
                        os.getenv("NEO4J_ACQUISITION_TIMEOUT_SECONDS", "10")
                    ),
                )
            return self._driver

    @contextmanager
    def session(self, **kwargs: Any) -> Iterator[Any]:
        if self.database and "database" not in kwargs:
            kwargs["database"] = self.database
        with self.driver.session(**kwargs) as session:
            yield session

    def verify_connectivity(self) -> None:
        self.driver.verify_connectivity()

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None

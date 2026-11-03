"""Derived semantic vectors for a bounded, read-only curated-cloud catalogue.

The injected provider is the authority for entities. This module never creates
entities, relationships, seed fallbacks, or database writes. The injected
embedder is the existing embedding client, not a local replacement model.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from numbers import Real
import threading
import time
from typing import Callable


class CloudSemanticUnavailable(RuntimeError):
    """Safe machine-readable failure; no upstream response or credential text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _normalized_vector(value, *, error_code):
    if isinstance(value, (str, bytes, dict)):
        raise CloudSemanticUnavailable(error_code)
    try:
        values = list(value)
    except (TypeError, ValueError):
        raise CloudSemanticUnavailable(error_code) from None
    if not values or len(values) > 65536:
        raise CloudSemanticUnavailable(error_code)
    if any(isinstance(item, bool) or not isinstance(item, Real) for item in values):
        raise CloudSemanticUnavailable(error_code)
    try:
        values = [float(item) for item in values]
    except (TypeError, ValueError, OverflowError):
        raise CloudSemanticUnavailable(error_code) from None
    if not all(math.isfinite(item) for item in values):
        raise CloudSemanticUnavailable(error_code)
    scale = max(abs(item) for item in values)
    if scale == 0:
        raise CloudSemanticUnavailable(error_code)
    # Scaling first keeps normalisation finite even for large finite inputs.
    values = [item / scale for item in values]
    norm = math.sqrt(math.fsum(item * item for item in values))
    return tuple(item / norm for item in values)


def _document(node):
    if not isinstance(node, dict):
        return None
    node_id, name, kind = node.get("id"), node.get("name"), node.get("type")
    if (not isinstance(node_id, str) or not node_id.strip() or node_id != node_id.strip()
            or not isinstance(name, str) or not name.strip()
            or not isinstance(kind, str) or not kind.strip()
            or kind.strip().casefold() == "evidencesource"):
        return None
    properties = node.get("properties") or {}
    if not isinstance(properties, dict):
        return None
    aliases = properties.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, (list, tuple)):
        aliases = []
    aliases = sorted({" ".join(alias.split()) for alias in aliases
                      if isinstance(alias, str) and alias.strip()})
    description = node.get("description") or properties.get("description") or ""
    if not isinstance(description, str):
        description = ""
    document = "\n".join((
        "名稱：" + " ".join(name.split()),
        "類別：" + kind.strip(),
        "別名：" + "、".join(aliases),
        "說明：" + " ".join(description.split()),
    ))[:2000]
    return node_id, document


class CloudSemanticCatalog:
    def __init__(self, node_provider, embedder, ttl_seconds=60, max_nodes=1000,
                 lock_timeout=0.5, *, clock: Callable[[], float] = time.monotonic):
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, Real)
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0):
            raise ValueError("invalid_catalog_ttl")
        if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
            raise ValueError("invalid_catalog_max_nodes")
        if (isinstance(lock_timeout, bool) or not isinstance(lock_timeout, Real)
                or not math.isfinite(lock_timeout) or not 0 < lock_timeout <= 2):
            raise ValueError("invalid_catalog_lock_timeout")
        self._provider = node_provider
        self._embedder = embedder
        self._ttl = float(ttl_seconds)
        self._max_nodes = max_nodes
        self._lock_timeout = float(lock_timeout)
        self._clock = clock
        self._lock = threading.Lock()
        self._nodes = ()
        self._vectors = {}
        self._vector_cache = {}
        self._dimension = None
        self._model = None
        self._expires_at = 0.0

    def _embedding_model(self):
        try:
            model = self._embedder.model
        except Exception:
            raise CloudSemanticUnavailable("cloud_embedding_model_invalid") from None
        if not isinstance(model, str) or not model.strip():
            raise CloudSemanticUnavailable("cloud_embedding_model_invalid")
        return model.strip()

    def _refresh(self):
        model = self._embedding_model()
        if self._model == model and self._clock() < self._expires_at:
            return
        # An expired catalogue is never a fallback after a failed refresh.
        self._nodes, self._vectors = (), {}
        self._dimension, self._model, self._expires_at = None, None, 0.0
        try:
            supplied = self._provider()
            if isinstance(supplied, (dict, str, bytes)) or supplied is None:
                raise CloudSemanticUnavailable("cloud_catalog_invalid")
            nodes, documents = [], {}
            for count, node in enumerate(supplied, start=1):
                if count > self._max_nodes:
                    raise CloudSemanticUnavailable("cloud_catalog_too_large")
                identity = _document(node)
                if identity is None:
                    continue
                node_id, document = identity
                if node_id in documents:
                    raise CloudSemanticUnavailable("cloud_catalog_duplicate_id")
                documents[node_id] = document
                nodes.append(deepcopy(node))
        except CloudSemanticUnavailable:
            raise
        except Exception:
            raise CloudSemanticUnavailable("cloud_catalog_unavailable") from None

        vectors, retained, dimension = {}, {}, None
        for node in nodes:
            node_id = node["id"]
            document = documents[node_id]
            key = model, hashlib.sha256(document.encode("utf-8")).hexdigest()
            vector = self._vector_cache.get(key) or retained.get(key)
            if vector is None:
                try:
                    raw = self._embedder.embed_query(document)
                except Exception:
                    raise CloudSemanticUnavailable("cloud_embedding_unavailable") from None
                vector = _normalized_vector(raw, error_code="cloud_embedding_invalid")
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise CloudSemanticUnavailable("cloud_vector_dimension_mismatch")
            vectors[node_id], retained[key] = vector, vector
        if self._embedding_model() != model:
            raise CloudSemanticUnavailable("cloud_embedding_model_changed")
        # Publish a complete snapshot atomically; removals and edits prune cache.
        self._nodes, self._vectors = tuple(nodes), vectors
        self._vector_cache = retained
        self._dimension, self._model = dimension, model
        self._expires_at = self._clock() + self._ttl

    def prewarm(self):
        """Load only current-cloud entity embeddings before serving user traffic."""
        if not self._lock.acquire(timeout=self._lock_timeout):
            raise CloudSemanticUnavailable("cloud_semantic_busy")
        try:
            self._refresh()
            return {"nodes": len(self._nodes), "dimension": self._dimension}
        finally:
            self._lock.release()

    def search(self, query_vector, *, limit=12):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("invalid_catalog_search_limit")
        query = _normalized_vector(query_vector, error_code="cloud_query_vector_invalid")
        if not self._lock.acquire(timeout=self._lock_timeout):
            raise CloudSemanticUnavailable("cloud_semantic_busy")
        try:
            self._refresh()
            if not self._nodes:
                return []
            if len(query) != self._dimension:
                raise CloudSemanticUnavailable("cloud_vector_dimension_mismatch")
            ranked = []
            for node in self._nodes:
                vector = self._vectors[node["id"]]
                score = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(query, vector))))
                candidate = deepcopy(node)
                candidate["semantic_score"] = score
                ranked.append(candidate)
            ranked.sort(key=lambda item: (-item["semantic_score"], item["id"]))
            return ranked[:min(limit, self._max_nodes)]
        finally:
            self._lock.release()

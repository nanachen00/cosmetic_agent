from __future__ import annotations

import pytest

from app.clients.reranker_client import RerankerClient, RerankerServiceError


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        return _Response(self.payload)


def test_reranker_client_uses_expected_contract():
    session = _Session({"results": [{"index": 1, "relevance_score": 0.91}]})
    client = RerankerClient(
        base_url="http://127.0.0.1:8004/v1/",
        model="bge-reranker-v2-m3",
        timeout_seconds=2.5,
        session=session,
    )

    result = client.rerank("保濕", ["玻尿酸", "神經醯胺"], top_n=1)

    assert result == [{"index": 1, "score": 0.91}]
    assert session.calls == [
        (
            "http://127.0.0.1:8004/v1/rerank",
            {
                "model": "bge-reranker-v2-m3",
                "query": "保濕",
                "documents": ["玻尿酸", "神經醯胺"],
                "top_n": 1,
                "return_documents": False,
            },
            2.5,
        )
    ]


def test_reranker_rejects_invalid_result():
    session = _Session({"results": [{"index": 9, "score": 0.8}]})
    client = RerankerClient(
        base_url="http://127.0.0.1:8004/v1",
        model="bge-reranker-v2-m3",
        timeout_seconds=2.5,
        session=session,
    )

    with pytest.raises(RerankerServiceError, match="no valid result"):
        client.rerank("保濕", ["玻尿酸"], top_n=1)

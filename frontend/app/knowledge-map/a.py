from __future__ import annotations

import sys
from pathlib import Path


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return source.replace(old, new, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_knowledge_query_interpreter.py TARGET")

    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")

    source = replace_once(
        source,
        "import asyncio\nimport logging\nimport os\n",
        "import asyncio\nimport json\nimport logging\nimport os\n",
        "json import",
    )
    source = replace_once(
        source,
        "from app.clients.reranker_client import RerankerClient\n",
        "from app.clients.reranker_client import RerankerClient\n"
        "from app.clients.terra_completion_client import TerraCompletionClient\n",
        "Terra client import",
    )
    source = replace_once(
        source,
        "logger = logging.getLogger(__name__)\n\n\nclass KnowledgeQueryUnavailable",
        '''logger = logging.getLogger(__name__)\n\n_QUERY_INTERPRETER_SYSTEM_PROMPT = (\n    "你是 BLINK AI 的美妝知識搜尋理解器。你的工作不是診斷或回答問題，而是把使用者的自然語句、"\n    "俗稱、別名或錯別字整理成可能存在於美妝知識圖譜的標準實體名稱。"\n    "實體類型只可為肌膚狀況、成分、照護需求或藥品。"\n    "請輸出一個 JSON 物件，格式固定為 "\n    '{"terms":["最可能詞","次可能詞"],"intent":"concern|ingredient|care|drug|unknown"}。'\n    "terms 依可能性排序，最多四個，每個不超過四十字；只輸出 JSON，不要解釋。"\n    "不要捏造療效、診斷、用量或圖譜關係；不確定時可回傳空 terms。"\n)\n\n\nclass KnowledgeQueryUnavailable''',
        "system prompt",
    )
    source = replace_once(
        source,
        "class _Reranker(Protocol):\n",
        "class _Reranker(Protocol):\n",
        "reranker protocol anchor",
    )
    source = replace_once(
        source,
        "\n\ndef _admission_from_env(max_running: int, max_queued: int):",
        '''\n\nclass _QueryInterpreter(Protocol):\n    def complete(\n        self,\n        *,\n        model: str,\n        messages: list[dict[str, str]],\n        temperature: float,\n        max_tokens: int,\n    ) -> str: ...\n\n\ndef _admission_from_env(max_running: int, max_queued: int):''',
        "query interpreter protocol",
    )
    source = replace_once(
        source,
        "        embedding_client: _Embedder,\n        reranker_client: _Reranker | None,\n",
        "        embedding_client: _Embedder,\n        reranker_client: _Reranker | None,\n"
        "        query_interpreter: _QueryInterpreter | None = None,\n"
        "        query_interpreter_model: str | None = None,\n",
        "constructor parameters",
    )
    source = replace_once(
        source,
        "        self.reranker_client = reranker_client\n",
        '''        self.reranker_client = reranker_client\n        self.query_interpreter = query_interpreter\n        self.query_interpreter_model = (\n            query_interpreter_model\n            or os.getenv("KNOWLEDGE_QUERY_TERRA_MODEL", "").strip()\n            or TerraCompletionClient.DEFAULT_MODEL\n        )\n''',
        "constructor assignments",
    )
    source = replace_once(
        source,
        '''        max_running = int(os.getenv("KNOWLEDGE_QUERY_MAX_RUNNING", "2"))\n        max_queued = int(os.getenv("KNOWLEDGE_QUERY_MAX_QUEUED", "8"))\n        return cls(\n''',
        '''        max_running = int(os.getenv("KNOWLEDGE_QUERY_MAX_RUNNING", "2"))\n        max_queued = int(os.getenv("KNOWLEDGE_QUERY_MAX_QUEUED", "8"))\n        interpreter = None\n        if os.getenv("KNOWLEDGE_QUERY_LLM_ENABLED", "1").strip().lower() in {\n            "1", "true", "yes", "on",\n        }:\n            try:\n                interpreter = TerraCompletionClient.from_env()\n            except Exception:\n                logger.warning("knowledge query interpreter unavailable", exc_info=True)\n        return cls(\n''',
        "from_env interpreter",
    )
    source = replace_once(
        source,
        "            reranker_client=reranker,\n",
        '''            reranker_client=reranker,\n            query_interpreter=interpreter,\n            query_interpreter_model=(\n                os.getenv("KNOWLEDGE_QUERY_TERRA_MODEL", "").strip()\n                or os.getenv("KNOWLEDGE_TERRA_MODEL", "").strip()\n                or TerraCompletionClient.DEFAULT_MODEL\n            ),\n''',
        "from_env constructor arguments",
    )
    source = replace_once(
        source,
        '''            "reranker_model": (\n                self.reranker_client.model if self.reranker_client else None\n            ),\n''',
        '''            "reranker_model": (\n                self.reranker_client.model if self.reranker_client else None\n            ),\n            "query_understanding": {\n                "enabled": self.query_interpreter is not None,\n                "provider": "terra" if self.query_interpreter is not None else None,\n                "model": self.query_interpreter_model if self.query_interpreter is not None else None,\n                "verified_against_graph": True,\n            },\n''',
        "status metadata",
    )
    source = replace_once(
        source,
        "\n    @staticmethod\n    def _filter_graph(graph: dict[str, Any]) -> dict[str, Any]:",
        '''\n    def _interpret_query(self, query: str) -> list[str]:\n        """Translate free-form wording into graph candidates without trusting LLM facts."""\n        if self.query_interpreter is None:\n            return []\n        try:\n            response = self.query_interpreter.complete(\n                model=self.query_interpreter_model,\n                messages=[\n                    {"role": "system", "content": _QUERY_INTERPRETER_SYSTEM_PROMPT},\n                    {\n                        "role": "user",\n                        "content": (\n                            "使用者輸入：" + query + "\\n"\n                            "請整理最可能的圖譜實體候選詞。"\n                        ),\n                    },\n                ],\n                temperature=0.0,\n                max_tokens=180,\n            )\n        except Exception:\n            logger.warning("knowledge query interpretation failed", exc_info=True)\n            return []\n        text = str(response or "").strip()\n        start = text.find("{")\n        end = text.rfind("}")\n        if start < 0 or end <= start:\n            return []\n        try:\n            payload = json.loads(text[start : end + 1])\n        except (TypeError, ValueError, json.JSONDecodeError):\n            return []\n        terms = payload.get("terms") if isinstance(payload, dict) else None\n        if not isinstance(terms, list):\n            return []\n        result: list[str] = []\n        seen: set[str] = set()\n        for value in terms:\n            if not isinstance(value, str):\n                continue\n            term = " ".join(value.split())[:40]\n            key = normalize_lookup(term)\n            if not key or key in seen:\n                continue\n            seen.add(key)\n            result.append(term)\n            if len(result) >= 4:\n                break\n        return result\n\n    @staticmethod\n    def _filter_graph(graph: dict[str, Any]) -> dict[str, Any]:''',
        "query interpreter method",
    )
    source = replace_once(
        source,
        '''        candidates = self._exact_candidates(clean_query)\n        strategy = "exact-alias"\n        if not candidates:\n            candidates = self._semantic_candidates(clean_query)\n            strategy = "bge-m3-plus-reranker"\n        if not candidates:\n            return None\n        center_id = str(candidates[0]["id"])\n''',
        '''        candidates = self._exact_candidates(clean_query)\n        strategy = "exact-alias"\n        interpreted_terms: list[str] = []\n        if not candidates:\n            interpreted_terms = self._interpret_query(clean_query)\n            seen_candidate_ids: set[str] = set()\n            interpreted_candidates: list[dict[str, Any]] = []\n            for term in interpreted_terms:\n                for node in self._exact_candidates(term):\n                    node_id = str(node.get("id") or "")\n                    if not node_id or node_id in seen_candidate_ids:\n                        continue\n                    seen_candidate_ids.add(node_id)\n                    interpreted_candidates.append(node)\n            if interpreted_candidates:\n                candidates = interpreted_candidates\n                strategy = "terra-query-understanding"\n        if not candidates:\n            semantic_query = interpreted_terms[0] if interpreted_terms else clean_query\n            candidates = self._semantic_candidates(semantic_query)\n            strategy = (\n                "terra-query-understanding-plus-bge-m3"\n                if interpreted_terms else "bge-m3-plus-reranker"\n            )\n        if not candidates:\n            return None\n        center_id = str(candidates[0]["id"])\n        canonical_name = str(candidates[0].get("name") or "").strip()\n''',
        "resolution flow",
    )
    source = replace_once(
        source,
        '''            "matched_alias": candidates[0].get("matched_alias"),\n            "strategy": strategy,\n            "ambiguous": len(candidates) > 1,\n''',
        '''            "matched_alias": candidates[0].get("matched_alias"),\n            "strategy": strategy,\n            "interpreted_terms": interpreted_terms,\n            "interpreted_query": canonical_name or None,\n            "input_was_rewritten": normalize_lookup(canonical_name) != normalize_lookup(clean_query),\n            "ambiguous": len(candidates) > 1,\n''',
        "resolution metadata",
    )

    required = (
        "_QUERY_INTERPRETER_SYSTEM_PROMPT",
        "query_interpreter: _QueryInterpreter | None = None",
        "def _interpret_query",
        '"strategy": strategy,\n            "interpreted_terms": interpreted_terms',
    )
    for marker in required:
        if marker not in source:
            raise RuntimeError(f"missing required marker after migration: {marker}")

    path.write_text(source, encoding="utf-8")
    print("knowledge query interpreter migration complete")


if __name__ == "__main__":
    main()

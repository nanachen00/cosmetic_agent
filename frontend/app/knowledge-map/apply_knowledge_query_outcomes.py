from __future__ import annotations

import sys
from pathlib import Path


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return source.replace(old, new, 1)


def patch_pipeline(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    source = replace_once(
        source,
        "class KnowledgeQueryUnavailable",
        '''class KnowledgeQueryNotResolved(Exception):
    """A safe, structured outcome for a query that has no verified graph node."""

    def __init__(
        self,
        *,
        code: str,
        query: str,
        terms: list[str] | None = None,
        intent: str = "unknown",
        mode: str = "single",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.query = query
        self.terms = list(terms or [])[:4]
        self.intent = intent
        self.mode = mode


class KnowledgeQueryUnavailable''',
        "not-resolved exception",
    )

    source = replace_once(
        source,
        '''    '{"terms":["最可能詞","次可能詞"],"intent":"concern|ingredient|care|drug|unknown"}。'\n''',
        '''    '{"terms":["最可能詞","次可能詞"],"intent":"concern|ingredient|care|drug|unknown",'\n    '"mode":"single|compare"}。'\n''',
        "interpreter response schema",
    )

    old_method_start = source.index("    def _interpret_query(self, query: str) -> list[str]:")
    old_method_end = source.index("\n    @staticmethod\n    def _filter_graph", old_method_start)
    new_method = '''    def _interpret_query(self, query: str) -> dict[str, Any]:
        """Extract graph candidates and UX intent without trusting LLM facts."""
        empty = {
            "terms": [],
            "intent": "unknown",
            "mode": "single",
            "available": False,
        }
        if self.query_interpreter is None:
            return empty
        try:
            response = self.query_interpreter.complete(
                model=self.query_interpreter_model,
                messages=[
                    {"role": "system", "content": _QUERY_INTERPRETER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "使用者輸入：" + query + "\\n"
                            "請整理最可能的圖譜實體候選詞；比較兩個主題時保留兩個候選。"
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=200,
            )
        except Exception:
            logger.warning("knowledge query interpretation failed", exc_info=True)
            return empty
        text = str(response or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {**empty, "available": True}
        try:
            payload = json.loads(text[start : end + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {**empty, "available": True}
        if not isinstance(payload, dict):
            return {**empty, "available": True}

        raw_terms = payload.get("terms")
        result: list[str] = []
        seen: set[str] = set()
        if isinstance(raw_terms, list):
            for value in raw_terms:
                if not isinstance(value, str):
                    continue
                term = " ".join(value.split())[:40]
                key = normalize_lookup(term)
                if not key or key in seen:
                    continue
                seen.add(key)
                result.append(term)
                if len(result) >= 4:
                    break

        intent = str(payload.get("intent") or "unknown").strip().lower()
        if intent not in {"concern", "ingredient", "care", "drug", "unknown"}:
            intent = "unknown"
        mode = str(payload.get("mode") or "single").strip().lower()
        if mode not in {"single", "compare"}:
            mode = "compare" if len(result) > 1 and any(
                marker in query for marker in ("比較", "差別", "一樣", "不同", "還是", "跟", "和")
            ) else "single"
        return {
            "terms": result,
            "intent": intent,
            "mode": mode,
            "available": True,
        }
'''
    source = source[:old_method_start] + new_method + source[old_method_end:]

    old_flow = '''        candidates = self._exact_candidates(clean_query)
        strategy = "exact-alias"
        interpreted_terms: list[str] = []
        if not candidates:
            interpreted_terms = self._interpret_query(clean_query)
            seen_candidate_ids: set[str] = set()
            interpreted_candidates: list[dict[str, Any]] = []
            for term in interpreted_terms:
                for node in self._exact_candidates(term):
                    node_id = str(node.get("id") or "")
                    if not node_id or node_id in seen_candidate_ids:
                        continue
                    seen_candidate_ids.add(node_id)
                    interpreted_candidates.append(node)
            if interpreted_candidates:
                candidates = interpreted_candidates
                strategy = "terra-query-understanding"
        if not candidates:
            semantic_query = interpreted_terms[0] if interpreted_terms else clean_query
            candidates = self._semantic_candidates(semantic_query)
            strategy = (
                "terra-query-understanding-plus-bge-m3"
                if interpreted_terms else "bge-m3-plus-reranker"
            )
        if not candidates:
            return None
        center_id = str(candidates[0]["id"])
        canonical_name = str(candidates[0].get("name") or "").strip()
'''
    new_flow = '''        candidates = self._exact_candidates(clean_query)
        strategy = "exact-alias"
        interpretation = {
            "terms": [],
            "intent": "unknown",
            "mode": "single",
            "available": False,
        }
        interpreted_terms: list[str] = []
        unresolved_terms: list[str] = []
        if not candidates:
            interpretation = self._interpret_query(clean_query)
            interpreted_terms = list(interpretation.get("terms") or [])[:4]
            seen_candidate_ids: set[str] = set()
            interpreted_candidates: list[dict[str, Any]] = []
            for term in interpreted_terms:
                term_candidates = self._exact_candidates(term)
                if not term_candidates:
                    unresolved_terms.append(term)
                for node in term_candidates:
                    node_id = str(node.get("id") or "")
                    if not node_id or node_id in seen_candidate_ids:
                        continue
                    seen_candidate_ids.add(node_id)
                    interpreted_candidates.append(node)
            if interpreted_candidates:
                candidates = interpreted_candidates
                strategy = "terra-query-understanding"
        if not candidates:
            semantic_query = interpreted_terms[0] if interpreted_terms else clean_query
            candidates = self._semantic_candidates(semantic_query)
            strategy = (
                "terra-query-understanding-plus-bge-m3"
                if interpreted_terms else "bge-m3-plus-reranker"
            )
        if not candidates:
            intent = str(interpretation.get("intent") or "unknown")
            interpreter_available = bool(interpretation.get("available"))
            if interpreter_available and intent in {"concern", "ingredient", "care", "drug"} and interpreted_terms:
                code = "knowledge_term_not_indexed"
            elif interpreter_available and (intent == "unknown" or not interpreted_terms):
                code = "knowledge_query_out_of_scope"
            else:
                code = "knowledge_term_not_resolved"
            raise KnowledgeQueryNotResolved(
                code=code,
                query=clean_query,
                terms=interpreted_terms,
                intent=intent,
                mode=str(interpretation.get("mode") or "single"),
            )
        center_id = str(candidates[0]["id"])
        canonical_name = str(candidates[0].get("name") or "").strip()
        candidate_entities = [
            {
                "id": str(node.get("id") or ""),
                "name": str(node.get("name") or ""),
                "type": str(node.get("type") or ""),
            }
            for node in candidates[:4]
            if node.get("id") and node.get("name")
        ]
'''
    source = replace_once(source, old_flow, new_flow, "resolution outcome flow")

    source = replace_once(
        source,
        '''            "interpreted_query": canonical_name or None,
            "input_was_rewritten": normalize_lookup(canonical_name) != normalize_lookup(clean_query),
            "ambiguous": len(candidates) > 1,
''',
        '''            "interpreted_query": canonical_name or None,
            "input_was_rewritten": normalize_lookup(canonical_name) != normalize_lookup(clean_query),
            "query_intent": str(interpretation.get("intent") or "unknown"),
            "query_mode": str(interpretation.get("mode") or "single"),
            "candidate_entities": candidate_entities,
            "unresolved_terms": unresolved_terms,
            "ambiguous": len(candidates) > 1 or bool(unresolved_terms),
''',
        "resolution outcome metadata",
    )

    for marker in (
        "class KnowledgeQueryNotResolved",
        '"knowledge_term_not_indexed"',
        '"candidate_entities": candidate_entities',
    ):
        if marker not in source:
            raise RuntimeError(f"pipeline patch missing marker: {marker}")
    path.write_text(source, encoding="utf-8")


def patch_route(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    source = replace_once(
        source,
        '''    KnowledgeQueryPipeline,
    KnowledgeQueryUnavailable,
    query_pipeline_enabled,
''',
        '''    KnowledgeQueryNotResolved,
    KnowledgeQueryPipeline,
    KnowledgeQueryUnavailable,
    query_pipeline_enabled,
''',
        "route import",
    )
    source = replace_once(
        source,
        '''        except KnowledgeQueryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
''',
        '''        except KnowledgeQueryNotResolved as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": exc.code,
                    "query": exc.query,
                    "suggestions": exc.terms,
                    "intent": exc.intent,
                    "mode": exc.mode,
                },
            ) from exc
        except KnowledgeQueryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
''',
        "route structured miss",
    )
    path.write_text(source, encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: apply_knowledge_query_outcomes.py PIPELINE ROUTE")
    pipeline = Path(sys.argv[1])
    route = Path(sys.argv[2])
    patch_pipeline(pipeline)
    patch_route(route)
    print("knowledge query outcomes migration complete")


if __name__ == "__main__":
    main()

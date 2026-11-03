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
        '''        empty = {
            "terms": [],
            "intent": "unknown",
            "mode": "single",
            "available": False,
        }
''',
        '''        empty = {
            "terms": [],
            "intent": "unknown",
            "mode": "single",
            "available": False,
            "parsed": False,
        }
''',
        "interpreter parsed state",
    )
    source = replace_once(
        source,
        '''        mode = str(payload.get("mode") or "single").strip().lower()
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
''',
        '''        has_compare_marker = len(result) > 1 and any(
            marker in query for marker in ("比較", "差別", "一樣", "不同", "還是", "跟", "和")
        )
        mode = str(payload.get("mode") or "single").strip().lower()
        if mode not in {"single", "compare"}:
            mode = "single"
        if has_compare_marker:
            mode = "compare"
        return {
            "terms": result,
            "intent": intent,
            "mode": mode,
            "available": True,
            "parsed": True,
        }
''',
        "interpreter comparison fallback",
    )

    flow_start = source.index('        candidates = self._exact_candidates(clean_query)')
    flow_end = source.index('        center_id = str(candidates[0]["id"])', flow_start)
    replacement = '''        candidates = self._exact_candidates(clean_query)
        strategy = "exact-alias"
        interpretation = {
            "terms": [],
            "intent": "unknown",
            "mode": "single",
            "available": False,
            "parsed": False,
        }
        interpreted_terms: list[str] = []
        unresolved_terms: list[str] = []
        if not candidates:
            interpretation = self._interpret_query(clean_query)
            interpreted_terms = list(interpretation.get("terms") or [])[:4]
            intent = str(interpretation.get("intent") or "unknown")
            mode = str(interpretation.get("mode") or "single")

            # A successfully parsed out-of-scope decision is authoritative.
            # Do not let vector similarity force unrelated input into the graph.
            if bool(interpretation.get("parsed")) and intent == "unknown":
                raise KnowledgeQueryNotResolved(
                    code="knowledge_query_out_of_scope",
                    query=clean_query,
                    terms=[],
                    intent=intent,
                    mode=mode,
                )

            seen_candidate_ids: set[str] = set()
            interpreted_candidates: list[dict[str, Any]] = []
            terms_to_resolve = interpreted_terms if mode == "compare" else interpreted_terms[:1]
            for term in terms_to_resolve:
                term_candidates = self._exact_candidates(term)
                term_strategy = "terra-query-understanding"
                if not term_candidates:
                    term_candidates = self._semantic_candidates(term)
                    term_strategy = "terra-query-understanding-plus-bge-m3"
                if not term_candidates:
                    unresolved_terms.append(term)
                    continue
                strategy = term_strategy
                for node in term_candidates[:1] if mode == "compare" else term_candidates:
                    node_id = str(node.get("id") or "")
                    if not node_id or node_id in seen_candidate_ids:
                        continue
                    seen_candidate_ids.add(node_id)
                    interpreted_candidates.append(node)
            if interpreted_candidates:
                candidates = interpreted_candidates

        if not candidates and not bool(interpretation.get("parsed")):
            # LLM offline or malformed output: retain the pre-existing verified
            # semantic fallback instead of treating infrastructure failure as OOS.
            candidates = self._semantic_candidates(clean_query)
            strategy = "bge-m3-plus-reranker"

        if not candidates:
            intent = str(interpretation.get("intent") or "unknown")
            interpreter_parsed = bool(interpretation.get("parsed"))
            if interpreter_parsed and intent in {"concern", "ingredient", "care", "drug"} and interpreted_terms:
                code = "knowledge_term_not_indexed"
            elif interpreter_parsed and (intent == "unknown" or not interpreted_terms):
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
'''
    source = source[:flow_start] + replacement + source[flow_end:]
    path.write_text(source, encoding="utf-8")


def patch_route(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    source = replace_once(
        source,
        "status_code=status.HTTP_404_NOT_FOUND,\n                detail={\n                    \"code\": exc.code,",
        "status_code=(\n                    status.HTTP_422_UNPROCESSABLE_ENTITY\n                    if exc.code == \"knowledge_query_out_of_scope\"\n                    else status.HTTP_404_NOT_FOUND\n                ),\n                detail={\n                    \"code\": exc.code,",
        "out-of-scope HTTP status",
    )
    path.write_text(source, encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: apply_knowledge_query_outcomes_round3.py PIPELINE ROUTE")
    patch_pipeline(Path(sys.argv[1]))
    patch_route(Path(sys.argv[2]))
    print("knowledge query outcomes round 3 complete")


if __name__ == "__main__":
    main()

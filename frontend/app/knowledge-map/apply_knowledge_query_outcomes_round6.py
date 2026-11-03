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
        '''    '"mode":"single|compare"}。'\n''',
        '''    '"mode":"single|compare","primary_term":"使用者主要想查的實體"}。'\n''',
        "interpreter primary term schema",
    )
    source = replace_once(
        source,
        '''    "terms 依可能性排序，最多四個，每個不超過四十字；只輸出 JSON，不要解釋。"\n''',
        '''    "terms 依可能性排序，最多四個，每個不超過四十字；primary_term 必須取自 terms。"\n    "使用者明確提到的成分或藥品通常是主要查詢對象；膚質與症狀可保留為其他 terms。"\n    "異維A酸、異維甲酸、isotretinoin 或口服A酸請正規化為口服 A 酸 (Isotretinoin)。"\n    "只輸出 JSON，不要解釋。"\n''',
        "interpreter ranking guidance",
    )
    source = replace_once(
        source,
        '''        return {\n            "terms": result,\n            "intent": intent,\n            "mode": mode,\n            "available": True,\n            "parsed": True,\n        }\n''',
        '''        primary_term = ""\n        raw_primary = payload.get("primary_term")\n        if isinstance(raw_primary, str):\n            candidate = " ".join(raw_primary.split())[:40]\n            candidate_key = normalize_lookup(candidate)\n            if candidate_key and any(normalize_lookup(term) == candidate_key for term in result):\n                primary_term = candidate\n        return {\n            "terms": result,\n            "intent": intent,\n            "mode": mode,\n            "primary_term": primary_term,\n            "available": True,\n            "parsed": True,\n        }\n''',
        "interpreter primary term parsing",
    )

    flow_start = source.index('        candidates = self._exact_candidates(clean_query)')
    flow_end = source.index('        center_id = str(candidates[0]["id"])', flow_start)
    replacement = '''        candidates = self._exact_candidates(clean_query)
        strategy = "exact-alias"
        interpretation = {
            "terms": [],
            "intent": "unknown",
            "mode": "single",
            "primary_term": "",
            "available": False,
            "parsed": False,
        }
        interpreted_terms: list[str] = []
        unresolved_terms: list[str] = []

        # These are canonical taxonomy aliases, not medical advice.  Resolve
        # them before the broader topical-A-acid concept so the safety layer can
        # handle oral isotretinoin correctly.
        normalized_input = normalize_lookup(clean_query)
        oral_isotretinoin_aliases = (
            "異維a酸",
            "異維甲酸",
            "isotretinoin",
            "口服異維a酸",
        )
        if any(alias in normalized_input for alias in oral_isotretinoin_aliases):
            candidates = self._exact_candidates("異維A酸")
            if not candidates:
                candidates = self._exact_candidates("口服 A 酸 (Isotretinoin)")
            if candidates:
                strategy = "explicit-canonical-alias"

        if not candidates:
            interpretation = self._interpret_query(clean_query)
            interpreted_terms = list(interpretation.get("terms") or [])[:4]
            intent = str(interpretation.get("intent") or "unknown")
            mode = str(interpretation.get("mode") or "single")
            primary_term = str(interpretation.get("primary_term") or "")

            # Only an unknown decision with no candidate terms is authoritative.
            if bool(interpretation.get("parsed")) and intent == "unknown" and not interpreted_terms:
                raise KnowledgeQueryNotResolved(
                    code="knowledge_query_out_of_scope",
                    query=clean_query,
                    terms=[],
                    intent=intent,
                    mode=mode,
                )

            seen_candidate_ids: set[str] = set()
            exact_hits: list[tuple[dict[str, Any], int, str]] = []
            semantic_hits: list[tuple[dict[str, Any], int, str]] = []

            # Resolve every interpreted term. A modifier in terms[0] must not
            # hide an explicit entity in terms[1]. Exact hits are collected
            # across all terms before any semantic fallback is considered.
            for term_index, term in enumerate(interpreted_terms):
                term_candidates = self._exact_candidates(term)
                if not term_candidates:
                    unresolved_terms.append(term)
                    continue
                for node in term_candidates:
                    node_id = str(node.get("id") or "")
                    if not node_id or node_id in seen_candidate_ids:
                        continue
                    seen_candidate_ids.add(node_id)
                    exact_hits.append((node, term_index, term))

            if mode == "compare":
                ordered: list[dict[str, Any]] = []
                ordered_ids: set[str] = set()
                for term_index, term in enumerate(interpreted_terms):
                    term_hits = [item for item in exact_hits if item[1] == term_index]
                    if not term_hits:
                        fallback = self._semantic_candidates(term)
                        if fallback:
                            term_hits = [(fallback[0], term_index, term)]
                            if term in unresolved_terms:
                                unresolved_terms.remove(term)
                    if not term_hits:
                        continue
                    node = term_hits[0][0]
                    node_id = str(node.get("id") or "")
                    if node_id and node_id not in ordered_ids:
                        ordered_ids.add(node_id)
                        ordered.append(node)
                candidates = ordered
                strategy = "terra-query-understanding" if exact_hits else "terra-query-understanding-plus-bge-m3"
            else:
                if not exact_hits:
                    for term_index, term in enumerate(interpreted_terms):
                        fallback = self._semantic_candidates(term)
                        if not fallback:
                            continue
                        node = fallback[0]
                        node_id = str(node.get("id") or "")
                        if not node_id or node_id in seen_candidate_ids:
                            continue
                        seen_candidate_ids.add(node_id)
                        semantic_hits.append((node, term_index, term))
                    if semantic_hits:
                        unresolved_terms = []

                ranked_hits = exact_hits or semantic_hits
                if ranked_hits:
                    intent_types = {
                        "concern": {"SkinConcern", "Concern"},
                        "ingredient": {"Ingredient"},
                        "care": {"CareNeed", "Care"},
                        "drug": {"Drug", "Medication"},
                    }
                    expected_types = intent_types.get(intent, set())
                    primary_key = normalize_lookup(primary_term)

                    def rank_hit(item: tuple[dict[str, Any], int, str]) -> tuple[int, int, int, int, int]:
                        node, term_index, term = item
                        properties = dict(node.get("properties") or {})
                        labels = [
                            str(node.get("matched_alias") or ""),
                            str(node.get("name") or ""),
                            *[str(value) for value in properties.get("aliases") or []],
                        ]
                        explicit_length = max(
                            (
                                len(key)
                                for key in (normalize_lookup(label) for label in labels)
                                if key and key in normalized_input
                            ),
                            default=0,
                        )
                        term_key = normalize_lookup(term)
                        return (
                            1 if explicit_length else 0,
                            1 if primary_key and term_key == primary_key else 0,
                            1 if str(node.get("type") or "") in expected_types else 0,
                            explicit_length,
                            -term_index,
                        )

                    ranked_hits.sort(key=rank_hit, reverse=True)
                    candidates = [item[0] for item in ranked_hits]
                    strategy = (
                        "terra-query-understanding"
                        if exact_hits
                        else "terra-query-understanding-plus-bge-m3"
                    )

        if not candidates and not bool(interpretation.get("parsed")):
            candidates = self._semantic_candidates(clean_query)
            strategy = "bge-m3-plus-reranker"

        if not candidates:
            intent = str(interpretation.get("intent") or "unknown")
            interpreter_parsed = bool(interpretation.get("parsed"))
            if interpreter_parsed and interpreted_terms:
                code = "knowledge_term_not_indexed"
            elif interpreter_parsed:
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


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_knowledge_query_outcomes_round6.py PIPELINE")
    patch_pipeline(Path(sys.argv[1]))
    print("knowledge query outcomes round 6 complete")


if __name__ == "__main__":
    main()

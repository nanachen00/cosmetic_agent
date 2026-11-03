from __future__ import annotations

import sys
from pathlib import Path


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return source.replace(old, new, 1)


def patch_route(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    marker = "knowledge query route round 8"
    if marker in source:
        print("knowledge query route round 8 already applied")
        return

    source = replace_once(
        source,
        """from app.services.knowledge_query_pipeline import (
    KnowledgeQueryPipeline,
    KnowledgeQueryUnavailable,
    query_pipeline_enabled,
)
""",
        """from app.services.knowledge_query_pipeline import (
    KnowledgeQueryNotResolved,
    KnowledgeQueryPipeline,
    KnowledgeQueryUnavailable,
    query_pipeline_enabled,
)
""",
        "structured query outcome import",
    )

    helper_anchor = """class KnowledgeQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=160)
    depth: int = Field(default=1, ge=1, le=2)
    limit: int = Field(default=60, ge=1, le=120)
    explain: bool = False
    viewer_context: ViewerContext | None = None


"""
    helper_block = helper_anchor + '''# knowledge query route round 8
_COMPARE_MARKERS = ("比較", "差別", "不同", "哪裡不同", "一樣嗎", "和", "跟", "與", "vs", "VS")
_URGENT_MARKERS = (
    "呼吸困難", "喘不過氣", "呼吸不到", "臉腫", "喉嚨腫", "流膿", "發燒",
    "皮膚癌", "黑色素瘤", "是不是癌", "直接診斷",
)
_ORAL_RETINOID_MARKERS = ("口服", "異維a酸", "異維甲酸", "isotretinoin")


def _compact_query(value: str) -> str:
    return "".join(value.casefold().split())


def _urgent_medical_query(value: str) -> bool:
    compact = _compact_query(value)
    return any(marker in compact for marker in _URGENT_MARKERS)


def _canonical_single_query(value: str) -> str:
    compact = _compact_query(value)
    if "bha" in compact or "salicylicacid" in compact:
        return "BHA"
    if any(marker in compact for marker in ("乾肌", "繃繃", "洗後緊繃", "洗完很緊")):
        return "乾燥脫皮"
    return value


def _comparison_terms(value: str) -> list[str]:
    compact = _compact_query(value)
    if not any(marker.casefold() in compact for marker in _COMPARE_MARKERS):
        return []
    terms: list[str] = []
    if any(marker in compact for marker in ("水楊酸", "bha", "salicylicacid")):
        terms.append("BHA")
    oral_retinoid = any(marker in compact for marker in _ORAL_RETINOID_MARKERS)
    if "a酸" in compact and not oral_retinoid:
        terms.append("A酸")
    if any(marker in compact for marker in ("a醇", "retinol", "視黃醇")):
        terms.append("retinol")
    return list(dict.fromkeys(terms)) if len(terms) >= 2 else []


def _merge_comparison_graphs(graphs: list[dict], original_query: str) -> dict:
    merged = dict(graphs[0])
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_nodes: set[str] = set()
    seen_edges: set[str] = set()
    candidates: list[dict[str, str]] = []

    for graph in graphs:
        center = graph.get("center") if isinstance(graph.get("center"), dict) else {}
        center_id = str(center.get("id") or "")
        center_name = str(center.get("name") or "")
        center_type = str(center.get("type") or "")
        if center_id and center_name:
            candidates.append({"id": center_id, "name": center_name, "type": center_type})
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "")
            if node_id and node_id not in seen_nodes:
                seen_nodes.add(node_id)
                nodes.append(node)
        for edge in graph.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            edge_key = str(edge.get("id") or "") or "|".join(
                str(edge.get(key) or "") for key in ("source", "target", "relationship")
            )
            if edge_key and edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append(edge)

    resolution = dict(merged.get("resolution") or {})
    resolution.update(
        {
            "query_mode": "compare",
            "candidate_entities": candidates,
            "unresolved_terms": [],
            "input_was_rewritten": True,
            "original_query": original_query,
        }
    )
    merged.update({"nodes": nodes, "edges": edges, "resolution": resolution})
    return merged


'''
    source = replace_once(source, helper_anchor, helper_block, "query route helpers")

    old_endpoint = '''        try:
            pipeline = query_pipeline_provider()
            graph = await pipeline.resolve_async(
                payload.query,
                depth=payload.depth,
                limit=payload.limit,
            )
        except KnowledgeQueryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail={
                "code": "knowledge_query_backend_unavailable", "retryable": True,
            }) from exc
'''
    new_endpoint = '''        if _urgent_medical_query(payload.query):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "knowledge_query_out_of_scope",
                    "query": payload.query,
                    "suggestions": [],
                    "intent": "medical_attention",
                    "mode": "single",
                    "message": "這個狀況不適合只靠美妝知識圖譜判斷，請盡快尋求醫療協助。",
                },
            )
        try:
            pipeline = query_pipeline_provider()
            comparison_terms = _comparison_terms(payload.query)
            if comparison_terms:
                comparison_graphs = [
                    await pipeline.resolve_async(
                        term,
                        depth=payload.depth,
                        limit=payload.limit,
                    )
                    for term in comparison_terms
                ]
                graph = _merge_comparison_graphs(comparison_graphs, payload.query)
            else:
                canonical_query = _canonical_single_query(payload.query)
                graph = await pipeline.resolve_async(
                    canonical_query,
                    depth=payload.depth,
                    limit=payload.limit,
                )
                if canonical_query != payload.query and isinstance(graph, dict):
                    resolution = dict(graph.get("resolution") or {})
                    resolution.update(
                        {
                            "input_was_rewritten": True,
                            "original_query": payload.query,
                        }
                    )
                    graph["resolution"] = resolution
        except KnowledgeQueryNotResolved as exc:
            not_resolved_status = (
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if exc.code == "knowledge_query_out_of_scope"
                else status.HTTP_404_NOT_FOUND
            )
            raise HTTPException(
                status_code=not_resolved_status,
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
            raise HTTPException(status_code=503, detail={
                "code": "knowledge_query_backend_unavailable", "retryable": True,
            }) from exc
'''
    source = replace_once(source, old_endpoint, new_endpoint, "query route flow")

    for required in (
        "KnowledgeQueryNotResolved",
        "def _comparison_terms",
        "def _urgent_medical_query",
        "_merge_comparison_graphs",
    ):
        if required not in source:
            raise RuntimeError(f"round 8 route patch missing marker: {required}")

    path.write_text(source, encoding="utf-8")
    print("knowledge query route round 8 applied")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_knowledge_query_route_round8.py ROUTE")
    patch_route(Path(sys.argv[1]))


if __name__ == "__main__":
    main()

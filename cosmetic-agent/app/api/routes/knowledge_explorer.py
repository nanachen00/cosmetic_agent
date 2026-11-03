"""Feature-flagged APIs for browsing approved knowledge and simplifying it."""

from __future__ import annotations

from threading import Lock
from typing import Callable, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.repositories.knowledge_explorer_repository import KnowledgeExplorerRepository
from app.services.knowledge_explorer_service import KnowledgeExplorerService, env_enabled
from app.services.knowledge_async_work import KnowledgeReadBusy
from app.services.knowledge_query_pipeline import (
    KnowledgeQueryNotResolved,
    KnowledgeQueryPipeline,
    KnowledgeQueryUnavailable,
    query_pipeline_enabled,
)


_SERVICE: KnowledgeExplorerService | None = None
_SERVICE_LOCK = Lock()
_QUERY_PIPELINE: KnowledgeQueryPipeline | None = None
_QUERY_PIPELINE_LOCK = Lock()
_INGREDIENT_REPOSITORY: KnowledgeExplorerRepository | None = None
_INGREDIENT_REPOSITORY_LOCK = Lock()


def _default_service_provider() -> KnowledgeExplorerService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = KnowledgeExplorerService()
    return _SERVICE


def _default_query_pipeline_provider() -> KnowledgeQueryPipeline:
    global _QUERY_PIPELINE
    service = _default_service_provider()
    if _QUERY_PIPELINE is None:
        with _QUERY_PIPELINE_LOCK:
            if _QUERY_PIPELINE is None:
                _QUERY_PIPELINE = KnowledgeQueryPipeline.from_env(service)
    return _QUERY_PIPELINE


def _default_ingredient_repository_provider() -> KnowledgeExplorerRepository:
    global _INGREDIENT_REPOSITORY
    if _INGREDIENT_REPOSITORY is None:
        with _INGREDIENT_REPOSITORY_LOCK:
            if _INGREDIENT_REPOSITORY is None:
                _INGREDIENT_REPOSITORY = KnowledgeExplorerRepository()
    return _INGREDIENT_REPOSITORY


def _require_feature() -> None:
    if not env_enabled("KNOWLEDGE_GRAPH_ENABLED"):
        # Keep disabled experimental routes undiscoverable to clients.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


class ViewerContext(BaseModel):
    skin_type: str | None = Field(default=None, max_length=32)
    concerns: list[str] = Field(default_factory=list, max_length=5)
    allergens: list[str] = Field(default_factory=list, max_length=5)
    recent_issues: list[str] = Field(default_factory=list, max_length=5)
    worsening_issues: list[str] = Field(default_factory=list, max_length=5)
    recent_products: list[str] = Field(default_factory=list, max_length=5)
    recent_record_count: int = Field(default=0, ge=0, le=30)


class ExplainRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=220)
    focus: Literal["overview", "cause", "care", "safety"] = "overview"
    viewer_context: ViewerContext | None = None


class IngredientSuggestionsRequest(BaseModel):
    messages: list[str] = Field(min_length=1, max_length=40)


_INGREDIENT_QUESTION_MARKERS = (
    "?", "？", "請問", "成分", "功效", "作用", "適合", "可以", "能不能",
    "怎麼", "什麼", "搭配", "含有", "有沒有", "刺激", "過敏", "風險", "禁忌",
)


class KnowledgeQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=160)
    depth: int = Field(default=1, ge=1, le=2)
    limit: int = Field(default=60, ge=1, le=120)
    explain: bool = False
    viewer_context: ViewerContext | None = None


# knowledge query route round 8
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
    if any(marker in compact for marker in _URGENT_MARKERS):
        return True
    # Oral retinoid dosage, pregnancy, and self-adjustment questions need a
    # clinician/pharmacist rather than an actionable beauty graph response.
    retinoid_reference = "a酸" in compact or any(
        marker in compact for marker in _ORAL_RETINOID_MARKERS
    )
    oral_medication_request = any(
        marker in compact
        for marker in ("吃", "劑量", "幾顆", "幾粒", "加倍", "懷孕", "備孕", "哺乳")
    )
    return retinoid_reference and oral_medication_request


def _canonical_single_query(value: str) -> str:
    compact = _compact_query(value)
    if "bha" in compact or "salicylicacid" in compact:
        return "BHA"
    if any(marker in compact for marker in ("乾肌", "繃繃", "洗後緊繃", "洗完很緊", "起皮")):
        return "乾燥脫皮"
    if "洗完" in compact and any(marker in compact for marker in ("緊", "繃")):
        return "乾燥脫皮"
    if "紅" in compact and "熱" in compact:
        return "臉部泛紅"
    return value


# knowledge query route round 11


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


def create_knowledge_explorer_router(
    service_provider: Callable[[], KnowledgeExplorerService] = _default_service_provider,
    query_pipeline_provider: Callable[[], KnowledgeQueryPipeline] = (
        _default_query_pipeline_provider
    ),
    ingredient_repository_provider: Callable[[], KnowledgeExplorerRepository] = (
        _default_ingredient_repository_provider
    ),
) -> APIRouter:
    router = APIRouter(prefix="/api/knowledge", tags=["knowledge-explorer"])

    @router.get("/status")
    async def knowledge_status():
        _require_feature()
        result = await run_in_threadpool(lambda: service_provider().status())
        if query_pipeline_enabled():
            try:
                result["retrieval"] = await run_in_threadpool(lambda: query_pipeline_provider().status())
            except Exception:
                result["retrieval"] = {"ready": False, "code": "knowledge_query_backend_unavailable"}
        return result

    @router.get("/search")
    async def search_knowledge(
        q: str = Query(min_length=1, max_length=80),
        limit: int = Query(default=12, ge=1, le=30),
    ):
        _require_feature()
        service = service_provider()
        nodes = await run_in_threadpool(service.search, q, limit=limit)
        return {"ok": True, "query": q.strip(), "nodes": nodes}

    @router.get("/ingredients/graph")
    async def ingredient_graph(
        q: str = Query(min_length=1, max_length=80),
        limit: int = Query(default=18, ge=1, le=30),
    ):
        _require_feature()
        try:
            graph = await run_in_threadpool(
                ingredient_repository_provider().ingredient_graph, q, limit=limit
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="成分圖譜暫時無法查詢",
            ) from exc
        if graph is None:
            raise HTTPException(status_code=404, detail="查無這項成分")
        return {"ok": True, **graph}

    @router.post("/ingredient-suggestions")
    async def ingredient_suggestions(payload: IngredientSuggestionsRequest):
        _require_feature()
        questions = [
            text.strip()[:160]
            for message in payload.messages
            if (text := message.strip())
            and any(marker in text for marker in _INGREDIENT_QUESTION_MARKERS)
        ]
        if not questions:
            return {"ok": True, "ingredients": []}
        try:
            names = await run_in_threadpool(
                ingredient_repository_provider().ingredient_mentions, questions, limit=2
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="知識圖譜暫時無法查詢成分",
            ) from exc
        return {"ok": True, "ingredients": names}

    @router.get("/explore")
    async def explore_knowledge(
        q: str = Query(min_length=1, max_length=160),
        depth: int = Query(default=1, ge=1, le=2),
        limit: int = Query(default=60, ge=1, le=120),
    ):
        _require_feature()
        service = service_provider()
        graph = await run_in_threadpool(
            service.explore, q, depth=depth, limit=limit
        )
        if graph is None:
            raise HTTPException(status_code=404, detail="Knowledge term not resolved")
        return {"ok": True, **graph}

    @router.get("/nodes/{node_id:path}")
    async def get_knowledge_node(
        node_id: str,
        limit: int = Query(default=18, ge=1, le=30),
    ):
        _require_feature()
        service = service_provider()
        graph = await run_in_threadpool(service.neighborhood, node_id, limit=limit)
        if graph is None:
            raise HTTPException(status_code=404, detail="Knowledge node not found")
        return {"ok": True, **graph}

    @router.post("/explain")
    async def explain_knowledge(payload: ExplainRequest):
        _require_feature()
        try:
            viewer_context = (
                payload.viewer_context.model_dump(exclude_none=True)
                if payload.viewer_context is not None
                else None
            )
            result = await service_provider().explain(
                payload.node_id,
                viewer_context=viewer_context,
                focus=payload.focus,
            )
        except KnowledgeReadBusy as exc:
            raise HTTPException(status_code=503, detail={
                "code": "knowledge_explanation_busy", "retryable": True,
            }) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail={
                "code": "knowledge_explanation_backend_unavailable", "retryable": True,
            }) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Knowledge node not found")
        return result

    @router.post("/query")
    async def query_knowledge(payload: KnowledgeQueryRequest):
        _require_feature()
        if not query_pipeline_enabled():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if _urgent_medical_query(payload.query):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "knowledge_query_out_of_scope",
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
        if graph is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Knowledge term not resolved",
            )
        viewer_context = (
            payload.viewer_context.model_dump(exclude_none=True)
            if payload.viewer_context is not None
            else None
        )
        service = service_provider()
        personalize = getattr(service, "personalize_graph", None)
        personalized_graph = (
            personalize(graph, viewer_context)
            if callable(personalize)
            else graph
        )
        response = {"ok": True, **personalized_graph}
        if payload.explain:
            try:
                response["explanation"] = await pipeline.explain(personalized_graph)
            except (KnowledgeQueryUnavailable, KnowledgeReadBusy) as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": str(exc), "retryable": True},
                ) from exc
        return response

    return router

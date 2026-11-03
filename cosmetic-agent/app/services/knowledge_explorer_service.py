"""Knowledge explorer orchestration with Terra-first, reversible explanations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
import re
import time
from typing import Any, Callable

import httpx

from app.clients.terra_completion_client import TerraCompletionClient
from app.services.knowledge_async_work import KnowledgeReadGate
from app.repositories.curated_overlay_query_repository import (
    CuratedOverlayQueryRepository,
    OverlayFirstCuratedRepository,
)
from app.repositories.knowledge_explorer_repository import KnowledgeExplorerRepository
from app.repositories.knowledge_seed_repository import KnowledgeSeedRepository
from app.repositories.reviewed_cloud_repository import (
    ReviewedCloudRepository, NoRuntimeSeedRepository,
)


_VALID_EXPLAINER_PROVIDERS = {"terra", "taide", "off"}
_EXPLANATION_FOCUSES = {
    "overview": "先回答這個節點最值得理解的重點，再串起最關鍵的關係。",
    "cause": "優先解釋形成因素、誘因與容易混淆的關係。",
    "care": "優先解釋圖譜已提供的照護方向與可觀察的下一步。",
    "safety": "優先解釋注意事項、證據限制與何時需要專業協助。",
}
_EXPLANATION_SECTION_LABELS = {
    "overview": ("簡單說", "重要關聯", "可以留意"),
    "cause": ("可能原因", "怎麼連起來", "可以留意"),
    "care": ("照護方向", "怎麼做", "可以留意"),
    "safety": ("注意事項", "避免什麼", "需要協助時"),
}
logger = logging.getLogger(__name__)
_FALSE_VALUES = {"0", "false", "no", "off"}


@lru_cache(maxsize=1)
def _load_readonly_curated_overlay() -> CuratedOverlayQueryRepository | None:
    enabled = os.getenv("KNOWLEDGE_CURATED_OVERLAY_READONLY", "1")
    if enabled.strip().lower() in _FALSE_VALUES:
        return None

    project_root = Path(__file__).resolve().parents[2]
    seed_path = Path(
        os.getenv(
            "KNOWLEDGE_SEED_PATH",
            str(project_root / "data/knowledge_graph/v1/knowledge_seed.json"),
        )
    )
    overlay_path = Path(
        os.getenv(
            "KNOWLEDGE_CURATED_OVERLAY_PATH",
            str(project_root / "data/knowledge_graph/v1/public_overlay.curated.json"),
        )
    )
    required = os.getenv("KNOWLEDGE_CURATED_OVERLAY_REQUIRED", "0")
    required_enabled = required.strip().lower() not in _FALSE_VALUES

    if not seed_path.is_file() or not overlay_path.is_file():
        message = (
            "read-only curated overlay unavailable: "
            f"seed={seed_path.is_file()} overlay={overlay_path.is_file()}"
        )
        if required_enabled:
            raise FileNotFoundError(message)
        logger.warning(message)
        return None

    try:
        return CuratedOverlayQueryRepository.from_paths(seed_path, overlay_path)
    except Exception:
        logger.exception("failed to load read-only curated knowledge overlay")
        if required_enabled:
            raise
        return None


_EXPLAINER_SYSTEM_PROMPT = (
    "你是 BLINK AI 的臺灣藥妝知識轉譯器。supplied_graph_evidence 是唯一可用的知識來源；"
    "viewer_context 只能改變解說的排序、用詞與提醒，不能被當成診斷或新增圖譜事實。"
    "viewer_context 中的 recent_issues、worsening_issues 與 recent_products 來自使用者近期膚況紀錄；"
    "若與圖譜證據有直接關聯，要具體說明這個關係為什麼值得該使用者優先看，不能只說『已個人化』。"
    "請依中心節點的類型、explanation_focus 與 viewer_context，用自然、好掃讀的繁體中文整理二到三點。"
    "每點必須使用 section_labels 指定的順序與『標題｜內容』格式，內容一到兩個短句、盡量不超過六十個中文字。"
    "先直接回答本次最重要的問題，再串起最相關的圖譜關係；不要讓每點都用中心節點名稱開頭，"
    "不要使用『已審核關係』等資料庫術語，也不要對不同節點重複同一組空泛句子。"
    "若有膚質、在意膚況或需避開項目，只能說明『為什麼這段資訊值得優先看』，"
    "不得聲稱它一定適合或一定造成問題。只能使用圖譜已提供的外觀、形成因素、照護方向、注意事項或專業協助門檻；"
    "不得新增節點、藥名、療效、診斷、用量、治療建議或因果關係，不得把導覽關係說成治療效果。"
    "遇到 needs-review、ontology-review 或 derived-review 時必須明說尚待人工複核；資料不足就直接說明缺口。"
    "不要輸出節點 ID、關係 ID、JSON、Markdown 標題或額外前言，引用會由系統附加。"
)


def env_enabled(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def explanation_provider() -> str:
    provider = os.getenv("KNOWLEDGE_EXPLAINER_PROVIDER", "terra").strip().lower()
    return provider if provider in _VALID_EXPLAINER_PROVIDERS else "invalid"


def explanation_model(provider: str) -> str | None:
    if provider == "terra":
        return (
            os.getenv(
                "KNOWLEDGE_TERRA_MODEL",
                os.getenv("TERRA_MODEL", TerraCompletionClient.DEFAULT_MODEL),
            ).strip()
            or TerraCompletionClient.DEFAULT_MODEL
        )
    if provider == "taide":
        return os.getenv("KNOWLEDGE_TAIDE_MODEL", "taide-12b").strip() or "taide-12b"
    return None


class KnowledgeExplorerService:
    def __init__(
        self,
        repository: KnowledgeExplorerRepository | None = None,
        *,
        seed_repository: KnowledgeSeedRepository | None = None,
        client_factory: Callable[..., Any] = httpx.AsyncClient,
        terra_client_factory: Callable[[], Any] = TerraCompletionClient.from_env,
    ) -> None:
        live_repository = repository or KnowledgeExplorerRepository()
        source_mode = os.getenv("KNOWLEDGE_EXPLORER_SOURCE", "neo4j-reviewed").strip()
        if source_mode not in {"neo4j-reviewed", "legacy", "json"}:
            raise ValueError("Invalid KNOWLEDGE_EXPLORER_SOURCE")
        self.source_mode = source_mode
        self.cloud_only = isinstance(repository, ReviewedCloudRepository) or (
            repository is None and source_mode == "neo4j-reviewed"
        )
        if source_mode == "json":
            snapshot_path = Path(
                os.getenv(
                    "KNOWLEDGE_MAP_JSON_PATH",
                    str(
                        Path(__file__).resolve().parents[2]
                        / "data/knowledge_graph/v1/knowledge_map.json"
                    ),
                )
            )
            snapshot = seed_repository or KnowledgeSeedRepository(snapshot_path)
            snapshot_status = snapshot.status()
            snapshot_available = bool(
                snapshot_status.get("available", snapshot_status.get("ok", False))
            )
            snapshot_node_count = snapshot_status.get("node_count")
            if snapshot_node_count is None:
                # Compatibility for read-only repository adapters that expose
                # only an ``ok`` health flag. Actual JSON snapshots still
                # report and validate their concrete node count.
                snapshot_node_count = 1 if snapshot_available else 0
            if not snapshot_available or not snapshot_node_count:
                raise RuntimeError("Knowledge map JSON is missing or empty")
            self.repository = snapshot
            self.seed_repository = snapshot
        elif self.cloud_only:
            self.repository = (
                repository if isinstance(repository, ReviewedCloudRepository)
                else ReviewedCloudRepository(live_repository)
            )
            self.seed_repository = NoRuntimeSeedRepository()
        else:
            readonly_overlay = (
                _load_readonly_curated_overlay() if repository is None else None
            )
            self.repository = (
                OverlayFirstCuratedRepository(readonly_overlay, live_repository)
                if readonly_overlay is not None else live_repository
            )
            self.seed_repository = seed_repository or KnowledgeSeedRepository()
        self._taide_client_factory = client_factory
        self._terra_client_factory = terra_client_factory
        concurrency = os.getenv(
            "KNOWLEDGE_EXPLAINER_MAX_CONCURRENCY",
            os.getenv("KNOWLEDGE_TAIDE_MAX_CONCURRENCY", "2"),
        )
        self._graph_gate = KnowledgeReadGate(max(1, int(concurrency)))
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        provider = explanation_provider()
        return {
            "ok": True,
            "feature_enabled": env_enabled("KNOWLEDGE_GRAPH_ENABLED"),
            "provider": provider,
            "model": explanation_model(provider),
            "taide_enabled": env_enabled("KNOWLEDGE_TAIDE_ENABLED"),
            "taide_experimental": True,
            "seed": self.seed_repository.status(),
            "data_source": (
                "json-file"
                if self.source_mode == "json"
                else ("neo4j-reviewed" if self.cloud_only else "legacy-or-injected")
            ),
            "seed_required": not self.cloud_only,
            "llm_generated_relations": False,
        }

    def search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 30))
        curated_search = getattr(self.repository, "curated_search", None)
        if callable(curated_search):
            try:
                curated_nodes = curated_search(query, limit=safe_limit)
            except Exception:
                logger.warning("curated knowledge search unavailable", exc_info=True)
                if self.cloud_only or env_enabled("KNOWLEDGE_CURATED_REQUIRED"):
                    raise
                curated_nodes = []
        else:
            # Legacy and injected repositories predate the curated overlay API.
            # Treat the optional overlay as absent instead of failing the request.
            curated_nodes = []
        seed_nodes = self.seed_repository.search(query, limit=safe_limit)
        try:
            live_nodes = self.repository.search(query, limit=safe_limit)
        except Exception:
            live_nodes = []
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for node in [*curated_nodes, *seed_nodes, *live_nodes]:
            properties = dict(node.get("properties") or {})
            identity = str(
                properties.get("normalized_name")
                or properties.get("mongo_id")
                or node.get("name")
                or node.get("id")
            ).casefold()
            key = (str(node.get("type") or "Knowledge"), identity)
            if key not in seen:
                merged.append(node)
                seen.add(key)
            if len(merged) >= safe_limit:
                break
        return merged

    def neighborhood(
        self,
        node_id: str,
        *,
        depth: int = 2,
        limit: int = 60,
    ) -> dict[str, Any] | None:
        curated_neighborhood = getattr(
            self.repository, "curated_neighborhood", None
        )
        if callable(curated_neighborhood):
            try:
                curated_graph = curated_neighborhood(
                    node_id, limit=min(limit, 60)
                )
            except Exception:
                logger.warning(
                    "curated knowledge neighborhood unavailable", exc_info=True
                )
                if self.cloud_only or env_enabled("KNOWLEDGE_CURATED_REQUIRED"):
                    raise
                curated_graph = None
        else:
            curated_graph = None
        if curated_graph is not None:
            return curated_graph
        seed_graph = self.seed_repository.neighborhood(
            node_id, depth=depth, limit=limit
        )
        if seed_graph is not None:
            return seed_graph
        return self.repository.neighborhood(node_id, limit=min(limit, 30))

    def explore(
        self,
        query: str,
        *,
        depth: int = 2,
        limit: int = 60,
    ) -> dict[str, Any] | None:
        curated_explore = getattr(self.repository, "curated_explore", None)
        if callable(curated_explore):
            try:
                curated_graph = curated_explore(query, limit=min(limit, 60))
            except Exception:
                logger.warning("curated knowledge explore unavailable", exc_info=True)
                if self.cloud_only or env_enabled("KNOWLEDGE_CURATED_REQUIRED"):
                    raise
                curated_graph = None
        else:
            curated_graph = None
        if curated_graph is not None:
            return curated_graph
        seed_graph = self.seed_repository.explore(query, depth=depth, limit=limit)
        if seed_graph is not None:
            return seed_graph
        matches = self.repository.search(query, limit=1)
        if not matches:
            return None
        graph = self.repository.neighborhood(matches[0]["id"], limit=min(limit, 30))
        if graph is not None:
            graph["resolution"] = {
                "query": str(query).strip(),
                "canonical_node_id": matches[0]["id"],
                "matched_alias": None,
                "strategy": "live-graph-text",
            }
        return graph

    @staticmethod
    def _citations(graph: dict[str, Any]) -> dict[str, Any]:
        edges = list(graph.get("edges") or [])[:12]
        node_ids = {str(graph["center"]["id"])}
        for edge in edges:
            node_ids.add(str(edge.get("source") or ""))
            node_ids.add(str(edge.get("target") or ""))
        return {
            "node_ids": sorted(node_id for node_id in node_ids if node_id),
            "edge_ids": [str(edge.get("id")) for edge in edges if edge.get("id")],
            "evidence_statuses": sorted(
                {
                    str(edge.get("evidence_status"))
                    for edge in edges
                    if edge.get("evidence_status")
                }
            ),
        }

    @staticmethod
    def _viewer_context(value: dict[str, Any] | None) -> dict[str, Any]:
        raw = value if isinstance(value, dict) else {}

        def clean_text(item: Any, *, max_length: int = 32) -> str:
            return re.sub(r"[\r\n\t]+", " ", str(item or "")).strip()[:max_length]

        def clean_list(item: Any) -> list[str]:
            if not isinstance(item, list):
                return []
            cleaned: list[str] = []
            for entry in item[:5]:
                text = clean_text(entry)
                if text and text not in cleaned:
                    cleaned.append(text)
            return cleaned

        context = {
            "skin_type": clean_text(raw.get("skin_type")),
            "concerns": clean_list(raw.get("concerns")),
            "allergens": clean_list(raw.get("allergens")),
            "recent_issues": clean_list(raw.get("recent_issues")),
            "worsening_issues": clean_list(raw.get("worsening_issues")),
            "recent_products": clean_list(raw.get("recent_products")),
        }
        try:
            record_count = max(0, min(30, int(raw.get("recent_record_count") or 0)))
        except (TypeError, ValueError):
            record_count = 0
        if record_count:
            context["recent_record_count"] = record_count
        return {key: item for key, item in context.items() if item}

    @staticmethod
    def _context_match(haystack: str, term: str) -> bool:
        left = re.sub(r"[\s\-_()/（）]+", "", haystack).casefold()
        right = re.sub(r"[\s\-_()/（）]+", "", term).casefold()
        return bool(right) and (right in left or (len(left) >= 2 and left in right))

    @classmethod
    def personalize_graph(
        cls,
        graph: dict[str, Any],
        viewer_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Rank graph entities for the viewer without inventing medical facts."""
        safe_context = cls._viewer_context(viewer_context)
        copied = dict(graph)
        raw_nodes = [dict(node) for node in graph.get("nodes", [])]
        if not safe_context or not raw_nodes:
            copied["nodes"] = raw_nodes
            copied["personalization"] = {
                "active": False,
                "used": [],
                "priority_node_ids": [],
                "caution_node_ids": [],
            }
            return copied

        skin_cues = {
            "油性": ("出油", "粉刺", "痘痘", "毛孔", "水楊酸", "清潔"),
            "乾性": ("乾燥", "脫屑", "緊繃", "保濕", "屏障", "神經醯胺"),
            "敏感性": ("敏感", "泛紅", "刺痛", "搔癢", "屏障", "溫和"),
            "混合性": ("出油", "乾燥", "毛孔", "保濕", "清潔"),
        }
        skin_type = str(safe_context.get("skin_type") or "")
        context_groups = (
            ("allergens", "caution", "與你設定的避開項目有關"),
            ("worsening_issues", "caution", "與你近期加重的膚況有關"),
            ("recent_issues", "priority", "與你近期記錄的膚況有關"),
            ("concerns", "priority", "與你在意的膚況有關"),
            ("recent_products", "priority", "與你近期記錄的產品有關"),
        )
        priority_ids: list[str] = []
        caution_ids: list[str] = []
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for index, node in enumerate(raw_nodes):
            searchable = " ".join((
                str(node.get("name") or ""),
                str(node.get("description") or ""),
                json.dumps(node.get("properties") or {}, ensure_ascii=False, default=str),
            ))
            level = ""
            reasons: list[str] = []
            matched: list[str] = []
            for key, candidate_level, reason in context_groups:
                for term in safe_context.get(key) or []:
                    if cls._context_match(searchable, str(term)):
                        matched.append(str(term))
                        if reason not in reasons:
                            reasons.append(reason)
                        if candidate_level == "caution" or not level:
                            level = candidate_level
            for cue in skin_cues.get(skin_type, ()):
                if cls._context_match(searchable, cue):
                    matched.append(cue)
                    if not level:
                        level = "priority"
                    reason = "與你的" + skin_type + "膚質較相關"
                    if reason not in reasons:
                        reasons.append(reason)
                    break
            if level:
                node["personalization"] = {
                    "level": level,
                    "reason": "、".join(reasons[:2]),
                    "matched_context": list(dict.fromkeys(matched))[:4],
                }
                node_id = str(node.get("id") or "")
                if level == "caution":
                    caution_ids.append(node_id)
                else:
                    priority_ids.append(node_id)
            rank = 0 if level == "caution" else 1 if level == "priority" else 2
            ranked.append((rank, index, node))

        copied["nodes"] = [node for _, _, node in sorted(ranked, key=lambda item: (item[0], item[1]))]
        center_id = str((graph.get("center") or {}).get("id") or "")
        center = next((node for node in copied["nodes"] if str(node.get("id") or "") == center_id), None)
        if center is not None:
            copied["center"] = dict(center)
        copied["personalization"] = {
            "active": bool(priority_ids or caution_ids),
            "used": sorted(safe_context),
            "priority_node_ids": priority_ids,
            "caution_node_ids": caution_ids,
        }
        return copied

    @staticmethod
    def _personalization_note(viewer_context: dict[str, Any]) -> str:
        labels: list[str] = []
        if viewer_context.get("skin_type"):
            labels.append(str(viewer_context["skin_type"]) + "膚質")
        concerns = viewer_context.get("concerns") or []
        if concerns:
            labels.append("在意「" + "、".join(concerns[:2]) + "」")
        allergens = viewer_context.get("allergens") or []
        if allergens:
            labels.append("需留意「" + "、".join(allergens[:2]) + "」")
        recent_issues = viewer_context.get("recent_issues") or []
        if recent_issues:
            labels.append("近期記錄「" + "、".join(recent_issues[:2]) + "」")
        worsening_issues = viewer_context.get("worsening_issues") or []
        if worsening_issues:
            labels.append("近期加重「" + "、".join(worsening_issues[:2]) + "」")
        if not labels:
            return ""
        return "已依你的" + "、".join(labels) + "調整閱讀順序；這不是個人診斷。"

    @classmethod
    def _fallback(
        cls,
        graph: dict[str, Any],
        *,
        reason: str,
        viewer_context: dict[str, Any] | None = None,
        focus: str = "overview",
    ) -> dict[str, Any]:
        center = graph["center"]
        safe_context = cls._viewer_context(viewer_context)
        description = str(center.get("description") or "").strip()
        contents: list[str] = []
        if description:
            contents.append(description)
        if graph.get("edges"):
            relations = "、".join(
                sorted({str(edge.get("relation_label") or edge.get("relation") or "相關") for edge in graph["edges"]})[:3]
            )
            contents.append("可以從「" + relations + "」等方向繼續理解。")
        if not contents:
            contents.append(center["name"] + "目前只有基本資料，還沒有足夠內容可以整理。")
        explanation = cls._format_sections(contents, focus)
        explanation = cls._ensure_required_notice(explanation, graph)
        explanation = cls._relabel_sections(explanation, focus)
        return {
            "ok": True,
            "node_id": center["id"],
            "explanation": explanation,
            "sections": cls._sections_from_text(explanation),
            "citations": cls._citations(graph),
            "provider": "graph-fallback",
            "model": None,
            "fallback": True,
            "reason": reason,
            "focus": focus if focus in _EXPLANATION_FOCUSES else "overview",
            "personalized": bool(safe_context),
            "personalization_used": sorted(safe_context),
            "latency_ms": 0,
        }

    @staticmethod
    def _evidence(graph: dict[str, Any]) -> dict[str, Any]:
        center = graph["center"]
        node_names = {node["id"]: node["name"] for node in graph.get("nodes", [])}
        relations = []
        for edge in graph.get("edges", [])[:12]:
            relations.append({
                "edge_id": edge.get("id"),
                "source_id": edge.get("source"),
                "source_name": node_names.get(edge["source"], edge["source"]),
                "relation": edge["relation"],
                "relation_label": edge.get("relation_label"),
                "target_id": edge.get("target"),
                "target_name": node_names.get(edge["target"], edge["target"]),
                "evidence_status": edge.get("evidence_status"),
                "evidence_reference": edge.get("evidence_reference"),
            })
        return {
            "term": center["name"],
            "node_id": center["id"],
            "type": center["type"],
            "description": center.get("description", ""),
            "properties": center.get("properties", {}),
            "relations": relations,
        }

    @staticmethod
    def _short_section(value: str, limit: int = 64) -> str:
        text = re.sub(r"\s+", " ", value).strip(" ，、；:：|｜-*#")
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip("，、； ") + "…"

    @classmethod
    def _format_sections(cls, contents: list[str], focus: str) -> str:
        safe_focus = focus if focus in _EXPLANATION_SECTION_LABELS else "overview"
        labels = _EXPLANATION_SECTION_LABELS[safe_focus]
        cleaned: list[str] = []
        for item in contents:
            shortened = cls._short_section(item)
            if shortened:
                cleaned.append(shortened)
        return "\n".join(
            label + "｜" + content
            for label, content in zip(labels, cleaned[: len(labels)])
        )[:520]

    @classmethod
    def _clean_model_text(cls, value: str, focus: str = "overview") -> str:
        raw_lines = [
            re.sub(r"^[\s\-•*\d.、）)]+", "", re.sub(r"[ \t]+", " ", item)).strip()
            for item in str(value or "").replace("根據提供的資料，", "").splitlines()
            if item.strip()
        ]
        contents: list[str] = []
        for line in raw_lines:
            if "｜" in line:
                line = line.split("｜", 1)[1]
            elif re.match(r"^[^。！？]{1,8}[：:]", line):
                line = re.split(r"[：:]", line, maxsplit=1)[1]
            if line.strip():
                contents.append(line.strip())
        if len(contents) < 2:
            text = " ".join(contents or raw_lines)
            contents = [item.strip() for item in re.split(r"(?<=[。！？])", text) if item.strip()]
        return cls._format_sections(contents, focus)

    @staticmethod
    def _sections_from_text(text: str) -> list[dict[str, str]]:
        sections: list[dict[str, str]] = []
        for line in str(text or "").splitlines():
            if "｜" not in line:
                continue
            title, content = line.split("｜", 1)
            if title.strip() and content.strip():
                sections.append({"title": title.strip(), "content": content.strip()})
        return sections[:4]

    @classmethod
    def _relabel_sections(cls, text: str, focus: str) -> str:
        contents: list[str] = []
        notices: list[str] = []
        for section in cls._sections_from_text(text):
            if section["title"] == "資料提醒":
                notices.append("資料提醒｜" + section["content"])
            else:
                contents.append(section["content"])
        formatted = cls._format_sections(contents, focus)
        return (formatted + ("\n" if formatted and notices else "") + "\n".join(notices))[:520]

    @staticmethod
    def _ensure_required_notice(text: str, graph: dict[str, Any]) -> str:
        statuses = {
            str(edge.get("evidence_status") or "")
            for edge in graph.get("edges", [])
        }
        review_statuses = {"needs-review", "ontology-review", "derived-review"}
        if not statuses & review_statuses:
            text = re.sub(
                r"[^\n。！？.!?]*(?:待人工複核|尚待複核|需要人工複核)[。！？.!?]?",
                "",
                text,
            ).strip()
            if not text:
                center = graph.get("center") or {}
                name = str(center.get("name") or center.get("id") or "這個節點")
                text = name + "目前只有基本資料，還沒有足夠內容可以整理。"
        suffixes = []
        if statuses & review_statuses:
            if "尚待人工複核" not in text:
                suffixes.append("資料提醒｜部分內容仍在整理確認中。")
        if "derived" in statuses and "不代表療效" not in text:
            suffixes.append("資料提醒｜圖中連線用於知識導覽，不代表療效或個人建議。")
        return (text + ("\n" if text and suffixes else "") + "\n".join(suffixes))[:520]

    @staticmethod
    def _messages(
        graph: dict[str, Any],
        *,
        viewer_context: dict[str, Any] | None = None,
        focus: str = "overview",
    ) -> list[dict[str, str]]:
        evidence = json.dumps(
            KnowledgeExplorerService._evidence(graph),
            ensure_ascii=False,
            separators=(",", ":"),
        )[:8_000]
        safe_focus = focus if focus in _EXPLANATION_FOCUSES else "overview"
        context = json.dumps(
            KnowledgeExplorerService._viewer_context(viewer_context),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return [
            {"role": "system", "content": _EXPLAINER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "explanation_focus=" + safe_focus + "\n"
                    "focus_instruction=" + _EXPLANATION_FOCUSES[safe_focus] + "\n"
                    "section_labels=" + json.dumps(_EXPLANATION_SECTION_LABELS[safe_focus], ensure_ascii=False) + "\n"
                    "viewer_context=" + context + "\n"
                    "supplied_graph_evidence=" + evidence
                ),
            },
        ]

    async def _explain_with_terra(
        self,
        graph: dict[str, Any],
        *,
        model: str,
        viewer_context: dict[str, Any] | None = None,
        focus: str = "overview",
    ) -> str:
        timeout = max(
            1.0,
            float(os.getenv("KNOWLEDGE_TERRA_TIMEOUT_SECONDS", "20")),
        )
        max_tokens = max(
            80,
            int(os.getenv("KNOWLEDGE_TERRA_MAX_OUTPUT_TOKENS", "180")),
        )
        client = self._terra_client_factory()
        return await asyncio.to_thread(
            client.complete,
            messages=self._messages(
                graph,
                viewer_context=viewer_context,
                focus=focus,
            ),
            model=model,
            max_tokens=max_tokens,
            temperature=0.15,
            timeout_seconds=timeout,
        )

    async def _explain_with_taide(
        self,
        graph: dict[str, Any],
        *,
        model: str,
        viewer_context: dict[str, Any] | None = None,
        focus: str = "overview",
    ) -> str:
        base_url = os.getenv(
            "KNOWLEDGE_TAIDE_BASE_URL",
            "http://127.0.0.1:8200/v1",
        ).strip().rstrip("/")
        timeout = max(1.0, float(os.getenv("KNOWLEDGE_TAIDE_TIMEOUT_SECONDS", "8")))
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("KNOWLEDGE_TAIDE_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        payload = {
            "model": model,
            "messages": self._messages(
                graph,
                viewer_context=viewer_context,
                focus=focus,
            ),
            "temperature": 0.15,
            "max_tokens": 180,
            "stream": False,
        }
        async with self._taide_client_factory(timeout=timeout) as client:
            response = await client.post(
                base_url + "/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        return body["choices"][0]["message"]["content"]

    async def explain(
        self,
        node_id: str,
        *,
        viewer_context: dict[str, Any] | None = None,
        focus: str = "overview",
    ) -> dict[str, Any] | None:
        graph = await self._graph_gate.run(self.neighborhood, node_id, limit=12)
        if graph is None:
            return None

        safe_context = self._viewer_context(viewer_context)
        safe_focus = focus if focus in _EXPLANATION_FOCUSES else "overview"

        provider = explanation_provider()
        if provider == "invalid":
            return self._fallback(
                graph,
                reason="invalid_provider",
                viewer_context=safe_context,
                focus=safe_focus,
            )
        if provider == "off":
            return self._fallback(
                graph,
                reason="explainer_off",
                viewer_context=safe_context,
                focus=safe_focus,
            )
        if provider == "taide" and not env_enabled("KNOWLEDGE_TAIDE_ENABLED"):
            return self._fallback(
                graph,
                reason="taide_disabled",
                viewer_context=safe_context,
                focus=safe_focus,
            )

        model = explanation_model(provider)
        if model is None:
            return self._fallback(
                graph,
                reason="model_not_configured",
                viewer_context=safe_context,
                focus=safe_focus,
            )
        evidence_version = hashlib.sha256(
            json.dumps(graph, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()[:16]
        prompt_version = hashlib.sha256(_EXPLAINER_SYSTEM_PROMPT.encode()).hexdigest()[:10]
        context_version = hashlib.sha256(
            json.dumps(safe_context, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:12]
        cache_key = ":".join((
            provider,
            model,
            prompt_version,
            str(graph["center"]["id"]),
            evidence_version,
            safe_focus,
            context_version,
        ))
        ttl = max(
            30,
            int(
                os.getenv(
                    "KNOWLEDGE_EXPLAINER_CACHE_TTL_SECONDS",
                    os.getenv("KNOWLEDGE_TAIDE_CACHE_TTL_SECONDS", "86400"),
                )
            ),
        )
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < ttl:
                return dict(cached[1])

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=0.25)
        except asyncio.TimeoutError:
            return self._fallback(
                graph,
                reason=provider + "_busy",
                viewer_context=safe_context,
                focus=safe_focus,
            )

        started = time.perf_counter()
        try:
            if provider == "terra":
                raw_text = await self._explain_with_terra(
                    graph,
                    model=model,
                    viewer_context=safe_context,
                    focus=safe_focus,
                )
                public_provider = "terra"
            else:
                raw_text = await self._explain_with_taide(
                    graph,
                    model=model,
                    viewer_context=safe_context,
                    focus=safe_focus,
                )
                public_provider = "taide-vllm"
            explanation = self._clean_model_text(raw_text, safe_focus)
            if not explanation:
                raise ValueError(provider + " returned an empty explanation")
            explanation = self._ensure_required_notice(explanation, graph)
            explanation = self._relabel_sections(explanation, safe_focus)
            result = {
                "ok": True,
                "node_id": graph["center"]["id"],
                "explanation": explanation,
                "sections": self._sections_from_text(explanation),
                "citations": self._citations(graph),
                "provider": public_provider,
                "model": model,
                "fallback": False,
                "reason": None,
                "focus": safe_focus,
                "personalized": bool(safe_context),
                "personalization_used": sorted(safe_context),
                "latency_ms": round((time.perf_counter() - started) * 1_000, 1),
            }
            async with self._cache_lock:
                if len(self._cache) >= 500:
                    oldest = min(self._cache, key=lambda key: self._cache[key][0])
                    self._cache.pop(oldest, None)
                self._cache[cache_key] = (time.monotonic(), result)
            return dict(result)
        except Exception:
            result = self._fallback(
                graph,
                reason=provider + "_unavailable",
                viewer_context=safe_context,
                focus=safe_focus,
            )
            result["latency_ms"] = round((time.perf_counter() - started) * 1_000, 1)
            return result
        finally:
            self._semaphore.release()



from __future__ import annotations

import os
from collections import Counter
from datetime import date as Date, timedelta

from agents.skin_tracking.observation_taxonomy import (
    catalog_by_slug,
    extract_observation_metadata,
)
from agents.skin_tracking.session_store import SkinSessionStore
from agents.skin_tracking.tracking_agent import SkinTrackingAgent
from tools.agent.tracking.report_note_display import build_report_display_notes
from tools.agent.tracking.skin_report_tool import build_report


_CONCERN_LABELS = {
    "concern_acne": "痘痘／粉刺",
    "concern_barrier_disruption": "皮膚屏障受損",
    "concern_clogged_pores": "毛孔堵塞",
    "concern_comedonal_acne": "粉刺型痘痘",
    "concern_dehydrated_skin": "肌膚缺水",
    "concern_dry_skin": "乾燥肌",
    "concern_enlarged_pores": "毛孔粗大",
    "concern_freckles": "雀斑",
    "concern_inflammatory_acne": "發炎型痘痘",
    "concern_melasma": "黃褐斑",
    "concern_photosensitivity": "光敏感",
    "concern_postinflammatory_hyperpigmentation": "發炎後色素沉澱",
    "concern_rosacea": "酒糟肌",
    "concern_sagging_skin": "皮膚鬆弛",
    "concern_seborrhoea": "脂漏性膚況",
    "concern_sensitive_skin": "敏感肌",
    "concern_solar_lentigo": "曬斑",
    "concern_wrinkles": "皺紋",
}

_INGREDIENT_LABELS = {
    "centella asiatica extract": "積雪草萃取物",
    "glyceryl glucoside": "甘油葡糖苷",
    "ficus carica (fig) fruit extract": "無花果果實萃取物",
    "panthenol": "維他命原 B5",
    "ceramide": "神經醯胺",
    "squalane": "角鯊烷",
    "mineral water": "礦物水",
    "pentavitin": "糖類同分異構物",
    "mineral": "礦物質",
    "lha": "辛醯水楊酸",
    "ulmus davidiana root extract": "榆樹根萃取物",
    "glycerin": "甘油",
    "niacinamide": "菸鹼醯胺",
    "hyaluronic acid": "玻尿酸",
    "salicylic acid": "水楊酸",
    "algae extract": "藻類萃取物",
}

def _display_ingredient_name(value: str) -> str:
    name = str(value or "").strip()
    return _INGREDIENT_LABELS.get(name.casefold(), name)

SKIN_TRIGGER = "cycle_skin_tracking"


def _sync_report_llm_enabled() -> bool:
    """Opt-in only: report reads must not wait for generative enrichment."""
    return os.getenv("SKIN_REPORT_SYNC_LLM", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


class SkinTrackingService:
    """Root／HTTP 層唯一需要呼叫的 Skin Tracking facade。"""

    def __init__(self, agent: SkinTrackingAgent, sessions: SkinSessionStore):
        self.agent = agent
        self.sessions = sessions

    def send(self, user_id: str, session_id: str, message: str) -> dict:
        entry = self.sessions.get(session_id, user_id)
        if message == SKIN_TRIGGER:
            message = "開始記錄膚況"

        result = self.agent.handle(
            message,
            user_id=user_id,
            session=entry.state if entry else None,
        )

        data = {
            k: result[k]
            for k in ("records", "eligibility", "report", "operation")
            if k in result
        }
        response = {
            "reply": result["reply"],
            "quick_replies": result.get("quick_replies", []),
            "cards": result.get("cards", []),
            "stage": result["stage"],
            "done": result.get("done", False),
            "data": data or None,
        }
        if result.get("done"):
            self.sessions.delete(session_id, user_id)
        else:
            self.sessions.put(session_id, user_id, result["session"], response)
        return response

    def get_session(self, user_id: str, session_id: str) -> dict | None:
        entry = self.sessions.get(session_id, user_id)
        if not entry:
            return None
        return {"session_id": session_id, **entry.last_response}


class SkinRecordService:
    def __init__(self, store, llm=None):
        self.store = store
        self.llm = llm

    def list_records(self, user_id: str, date_from=None, date_to=None, issue=None, zone=None):
        records = self.store.query(user_id, date_from, date_to, issue, zone)
        return [self._with_status_defaults(record) for record in records]

    def update_record(self, user_id: str, record_id: str, fields: dict) -> bool:
        return self.store.update(user_id, record_id, fields)

    def delete_record(self, user_id: str, record_id: str) -> bool:
        return self.store.delete(user_id, record_id)

    def current_report(self, user_id: str, period_days: int = 14) -> dict:
        today = Date.today()
        eligibility = self.store.check_eligibility(user_id, today, min_days=5)
        records = [
            self._with_status_defaults(record)
            for record in self.store.query(
                user_id,
                str(today - timedelta(days=period_days - 1)),
                str(today),
            )
        ]
        eligibility = {
            **eligibility,
            "eligible": True,
            "days_needed": 0,
        }
        if not records:
            return {"eligibility": eligibility, "report": None, "records": []}
        enrichment_llm = self.llm if _sync_report_llm_enabled() else None
        display_notes = build_report_display_notes(self.llm, records)
        report_records = []
        for record in records:
            report_record = dict(record)
            display_note = display_notes.get(str(record.get("record_id") or ""))
            if display_note:
                report_record["display_note"] = display_note
            report_records.append(report_record)
        report = build_report(
            user_id=user_id,
            records=records,
            date_from=today - timedelta(days=period_days - 1),
            date_to=today,
            consecutive_days=eligibility["consecutive_days"],
            llm_client=enrichment_llm if records else None,
        )
        return {
            "eligibility": eligibility,
            "report": report.model_dump(mode="json"),
            "records": report_records,
        }

    def current_recommendations(
        self,
        user_id: str,
        period_days: int = 14,
        limit: int = 3,
        date_from: Date | None = None,
        date_to: Date | None = None,
    ) -> dict:
        return self._build_recommendations(
            user_id, period_days, limit, date_from, date_to,
            strict_partner=False,
        )

    def partner_recommendations(
        self,
        user_id: str,
        period_days: int = 14,
        limit: int = 3,
        date_from: Date | None = None,
        date_to: Date | None = None,
    ) -> dict:
        """Only recommend catalog products explicitly marked is_partner=true."""
        return self._build_recommendations(
            user_id, period_days, limit, date_from, date_to,
            strict_partner=True,
        )

    def _build_recommendations(
        self,
        user_id: str,
        period_days: int,
        limit: int,
        date_from: Date | None,
        date_to: Date | None,
        *,
        strict_partner: bool,
    ) -> dict:
        if (date_from is None) != (date_to is None):
            raise ValueError("date_from and date_to must be provided together")
        today = Date.today()
        range_from = date_from or today - timedelta(days=period_days - 1)
        range_to = date_to or today
        basis_prefix = (
            f"依 {range_from:%Y/%m/%d}～{range_to:%Y/%m/%d} 的"
            if date_from is not None
            else f"依近 {period_days} 天的"
        )
        records = self.list_records(user_id, str(range_from), str(range_to))
        issue_options = {
            "acne": {
                "label": "痘痘／粉刺",
                "query": "臉部 痘痘 粉刺 溫和 清潔 保濕",
                "concerns": ["抗痘", "控油", "保濕"],
            },
            "dryness": {
                "label": "乾燥／脫皮",
                "query": "臉部 乾燥 脫皮 保濕 修護 溫和",
                "concerns": ["保濕", "修護", "舒敏"],
            },
            "redness": {
                "label": "泛紅",
                "query": "臉部 泛紅 舒緩 修護 保濕 低刺激",
                "concerns": ["舒敏", "修護", "保濕"],
            },
            "oiliness": {
                "label": "出油",
                "query": "臉部 出油 控油 清爽 保濕",
                "concerns": ["控油", "保濕"],
            },
            "itching": {
                "label": "搔癢／不適",
                "query": "臉部 搔癢 不適 舒緩 修護 低刺激",
                "concerns": ["舒敏", "修護"],
            },
            "pigmentation": {
                "label": "斑點",
                "query": "臉部 斑點 色斑 曬斑 雀斑 暗沉 防曬 淡斑 保濕",
                "concerns": ["淡斑", "防曬", "保濕"],
            },
            "acne_scar": {
                "label": "痘疤",
                "query": "臉部 痘疤 痘印 痘痕 修護 防曬 膚色不均",
                "concerns": ["修護", "防曬", "保濕"],
            },
            "dark_spot": {
                "label": "斑點／痘疤（舊紀錄）",
                "query": "臉部 痘疤 斑點 防曬 淡斑 保濕",
                "concerns": ["淡斑", "防曬", "保濕"],
            },
        }
        catalog = None
        other_options = {}
        issue_scores = Counter()
        for record in records:
            issue = str(record.get("issue") or "")
            if issue != "other":
                try:
                    weight = max(1, int(record.get("severity") or 1))
                except (TypeError, ValueError):
                    weight = 1
                issue_scores[issue] += weight
                continue
            if issue == "other":
                if catalog is None:
                    catalog = catalog_by_slug()
                note_slugs, _ = extract_observation_metadata(str(record.get("note") or ""))
                slugs = dict.fromkeys([*(record.get("tag_slugs") or []), *note_slugs])
                tags = [catalog.get(str(slug)) for slug in slugs]
                if any(tag is not None and tag.condition_flag for tag in tags):
                    continue
                tag = next((tag for tag in tags if tag is not None), None)
                if tag is None:
                    continue
                issue = f"other:{tag.slug}"
                region = "臉部"
                if tag.slug in {"periorbital_darkening", "under_eye_puffiness"}:
                    region = "眼周"
                elif any("eye" in zone or zone == "periorbital" for zone in tag.face_regions):
                    region = "眼周"
                elif any("lip" in zone or zone in {"mouth_corner", "perioral"} for zone in tag.face_regions):
                    region = "唇部"
                query = f"{region} {tag.name_zh_tw} 日常保養"
                if tag.slug == "periorbital_darkening":
                    query = "眼周 黑眼圈 暗沉 眼部保養 眼霜 保濕"
                other_options[issue] = {
                    "label": tag.name_zh_tw,
                    "query": query,
                    "concerns": ["日常"],
                }
            if issue not in issue_options and issue not in other_options:
                continue
            try:
                weight = max(1, int(record.get("severity") or 1))
            except (TypeError, ValueError):
                weight = 1
            issue_scores[issue] += weight

        if not issue_scores:
            return {
                "basis": f"{basis_prefix}紀錄目前沒有足夠明確的膚況類型可安全篩選商品。",
                "products": [],
                "excluded_ingredients": [],
                "message": "再補充症狀與部位後，商品候選會更準確。",
            }

        primary_issue = issue_scores.most_common(1)[0][0]
        option = issue_options.get(primary_issue) or other_options[primary_issue]
        profile = {}
        try:
            if os.getenv("AUTH_STORAGE_BACKEND", "neo4j").strip().lower() == "postgres":
                from database.postgres.repositories.profile import (
                    postgres_profile_repository,
                )
                profile = postgres_profile_repository.get_profile(user_id) or {}
            else:
                from database.profile_repository import profile_repository
                profile = profile_repository.get_profile(user_id) or {}
        except Exception:
            profile = {}

        skin_type_map = {
            "油性肌": "oily",
            "乾性肌": "dry",
            "混合肌": "combination",
            "敏感肌": "sensitive",
            "中性肌": "normal",
        }
        skin_type = skin_type_map.get(
            str(profile.get("skinType") or ""),
            str(profile.get("skinType") or "unknown"),
        )
        if skin_type not in {"oily", "dry", "combination", "sensitive", "normal"}:
            skin_type = None

        excluded = [
            str(item).strip()
            for item in profile.get("allergens", [])
            if str(item).strip()
        ]
        wellness_status = str(profile.get("wellnessStatus") or "")
        wellness_exclusions = []
        if wellness_status in {"trying", "pregnant", "breastfeeding"}:
            wellness_exclusions = [
                "retinol",
                "retinal",
                "retinoid",
                "a醇",
                "a酸",
                "視黃醇",
                "維生素a酸",
            ]
        filter_terms = list(dict.fromkeys(excluded + wellness_exclusions))

        try:
            from agents.product.product_agent import product_search
            from tools.search.graph_search import graph_search

            if strict_partner:
                from agents.product.product_agent import partner_product_search
                candidates = partner_product_search(option["query"], top_k=60)
                # The database flag, not the source label or search adapter, is authoritative.
                candidates = [item for item in candidates if item.get("is_partner") is True]
            else:
                try:
                    candidates = product_search(
                        option["query"],
                        category=None,
                        top_k=18,
                        partner_only=False,
                    )
                except TypeError as error:
                    if "partner_only" not in str(error):
                        raise
                    candidates = product_search(
                        option["query"], category=None, top_k=18
                    )
            graph = (
                graph_search(
                    [product["id"] for product in candidates],
                    user_skin_type=skin_type,
                )
                if candidates or not strict_partner else {"ingredients": []}
            )
        except Exception:
            return {
                "basis": f"{basis_prefix}紀錄以「{option['label']}」為主要關注。",
                "products": [],
                "excluded_ingredients": excluded,
                "message": "商品資料暫時無法讀取，稍後重新整理即可再試。",
            }

        graph_ingredients = {}
        for item in graph.get("ingredients", []):
            product_id = str(item.get("product_id") or "")
            name = str(item.get("ingredient_name_zh") or "").strip()
            if product_id and name:
                graph_ingredients.setdefault(product_id, []).append(name)

        def ingredient_names(product):
            names = []
            for item in product.get("ingredients_list") or []:
                if isinstance(item, dict):
                    value = (
                        item.get("name_zh")
                        or item.get("name")
                        or item.get("name_en")
                        or ""
                    )
                else:
                    value = item
                value = str(value).strip()
                if value:
                    names.append(value)
            names.extend(graph_ingredients.get(str(product.get("id") or ""), []))
            return list(dict.fromkeys(names))

        safe_candidates = []
        for product in candidates:
            names = ingredient_names(product)
            haystack = " ".join(names).casefold()
            if filter_terms and not names:
                continue
            if any(term.casefold() in haystack for term in filter_terms):
                continue
            if strict_partner and skin_type:
                skin_markers = {
                    "oily": ("油性肌", "油肌", "oily skin"),
                    "dry": ("乾性肌", "乾肌", "dry skin"),
                    "combination": ("混合肌", "combination skin"),
                    "sensitive": ("敏感肌", "sensitive skin"),
                    "normal": ("中性肌", "normal skin"),
                }
                cohort = str(product.get("cohort") or "").casefold()
                if any(
                    marker in cohort
                    for markers in skin_markers.values()
                    for marker in markers
                ) and not any(marker in cohort for marker in skin_markers[skin_type]):
                    continue
                if any(
                    f"{name} 不建議{skin_type}膚質使用" in graph.get("warnings", [])
                    for name in names
                ):
                    continue
            safe_candidates.append((product, names))

        if strict_partner:
            issue_terms = {
                "acne": ("痘痘", "粉刺", "抗痘"),
                "dryness": ("乾燥", "脫皮", "保濕", "修護"),
                "redness": ("泛紅", "舒敏", "舒緩", "修護"),
                "oiliness": ("出油", "控油", "油性"),
                "itching": ("搔癢", "癢", "舒敏", "舒緩"),
                "pigmentation": ("斑點", "淡斑", "色斑", "防曬"),
                "acne_scar": ("痘疤", "痘印", "痘痕", "修護"),
                "dark_spot": ("斑點", "痘疤", "淡斑", "防曬"),
                "other:periorbital_darkening": ("黑眼圈", "眼周暗沉", "眼霜"),
            }
            terms = issue_terms.get(primary_issue, (option["label"],))

            def product_text(product):
                fields = ("name", "name_zh", "description", "category", "concerns", "functional_categories", "common_names", "cohort")
                return " ".join(
                    " ".join(map(str, product.get(field)))
                    if isinstance(product.get(field), (list, tuple))
                    else str(product.get(field) or "")
                    for field in fields
                ).casefold()

            matched = [
                pair for pair in safe_candidates
                if any(term.casefold() in product_text(pair[0]) for term in terms)
            ]
            if matched:
                safe_candidates = matched
            elif skin_type:
                skin_terms = {
                    "oily": ("油性", "控油", "oily"),
                    "dry": ("乾性", "保濕", "dry"),
                    "combination": ("混合", "combination"),
                    "sensitive": ("敏感", "舒敏", "sensitive"),
                    "normal": ("中性", "normal"),
                }[skin_type]
                safe_candidates = [
                    pair for pair in safe_candidates
                    if any(term in product_text(pair[0]) for term in ("日常", "基礎保養"))
                    and any(term.casefold() in product_text(pair[0]) for term in skin_terms)
                ]
            else:
                safe_candidates = []

        safe_candidates.sort(key=lambda pair: -float(pair[0].get("score") or 0))

        products = []
        for product, names in safe_candidates[:limit]:
            display_names = list(dict.fromkeys(_display_ingredient_name(name) for name in names))
            try:
                price = float(product.get("price") or 0)
                price = int(price) if price.is_integer() else price
            except (TypeError, ValueError):
                price = None
            reason = (
                f"近期紀錄以「{option['label']}」為主要關注，"
                f"因此列為{option['concerns'][0]}保養的候選。"
            )
            if strict_partner and not any(
                term.casefold() in product_text(product) for term in terms
            ):
                reason = (
                    f"目前沒有直接對應「{option['label']}」的三友商品，"
                    "此商品依膚質列為日常保養候選；不代表能改善該膚況。"
                )
            if display_names:
                reason += f"商品資料標示含{'、'.join(display_names[:2])}，使用前仍請查看完整標示。"
            products.append(
                {
                    "id": str(product.get("id") or ""),
                    "name": (
                        str(product.get("name_zh") or "").strip()
                        if str(product.get("name_zh") or "").strip()
                        else str(product.get("name") or "")
                    ),
                    "brand": str(product.get("brand") or ""),
                    "category": str(product.get("category") or ""),
                    "price": price,
                    "ingredients": display_names[:3],
                    "concerns_zh": list(dict.fromkeys(_CONCERN_LABELS[code] for code in (product.get("concerns") or []) if code in _CONCERN_LABELS)),
                    "reason": reason,
                    "is_partner": bool(product.get("is_partner")),
                }
            )

        return {
            "basis": (
                f"{basis_prefix}「{option['label']}」紀錄"
                + (f"與{profile.get('skinType')}設定" if profile.get("skinType") else "")
                + "篩選；商品排序不代表療效高低。"
            ),
            "products": products,
            "excluded_ingredients": excluded,
            "message": (
                ""
                if products
                else (
                    "目前三友商品庫沒有通過個人條件篩選的商品候選。"
                    if strict_partner
                    else "目前沒有符合膚況與個人條件的商品候選。"
                )
            ),
        }

    @staticmethod
    def _with_status_defaults(record: dict) -> dict:
        """Expose old records safely without mutating repository-owned objects."""
        normalized = dict(record)
        normalized["record_status"] = normalized.get("record_status") or "recorded"
        normalized["stability_source"] = normalized.get("stability_source")
        normalized["tag_slugs"] = list(normalized.get("tag_slugs") or [])
        normalized["detail_zones"] = list(normalized.get("detail_zones") or [])
        return normalized

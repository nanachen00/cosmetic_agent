"""Skin Tracking conversation state machine.

Semantic extraction and persistence are injected boundaries. This module owns only
dialogue state, intent routing and explicit confirmation before writes.

root 呼叫方式：
    from agents.skin_tracking.tracking_agent import run as skin_tracking_run
    result = skin_tracking_run(user_message, user_id=..., session=state.skin_session,
                               initial_draft={"issue": "acne", "note": "..."} )
    state.skin_session = result["session"]
    if result["done"]: state.is_waiting_skin_tracking = False
"""
from __future__ import annotations

import re
import time
from datetime import date as Date, timedelta
from typing import Protocol

from agents.skin_tracking.extraction import SkinExtractionProvider
from core.observability import observe_pipeline_stage
from agents.skin_tracking.prompts import (
    CONFIRM_QUICK_REPLIES,
    OTHER_ISSUE_QUESTION,
    SLOT_QUESTIONS,
    SLOT_QUICK_REPLIES,
    STABILITY_CONFIRM_QUESTION,
    STABILITY_CONFIRM_QUICK_REPLIES,
)
from agents.skin_tracking.state import SkinSession
from agents.skin_tracking.location_semantics import (
    LocationParse,
    detail_location_labels,
    extract_face_locations,
    merge_detail_zones,
    resolve_pending_location,
    zones_from_action,
)
from agents.skin_tracking.issue_semantics import infer_explicit_issues
from agents.skin_tracking.observation_taxonomy import (
    extract_observation_metadata,
    tag_label,
)
from schemas.tracking_schema import (
    ISSUE_LABELS,
    Issue,
    RECORD_STATUS_LABELS,
    ZONE_LABELS,
    RecordDraft,
    RecordStatus,
    StabilitySource,
    TrackIntent,
    Zone,
)
from tools.agent.tracking.skin_extract_tool import (
    extract_skin_drafts_from_message,
    extract_skin_drafts_with_metadata,
)
from tools.agent.tracking.skin_save_tool import save_skin_record

NTH_RE = re.compile(r"第\s*(\d+)\s*筆")
MIN_REPORT_DAYS = 5
CONFIRM_WORDS = {"確認", "確定", "是", "對", "好", "ok", "yes", "confirm_yes"}
CANCEL_WORDS = {"取消", "算了", "不要", "停止", "離開", "cancel"}
EDIT_WORDS = {
    "修改",
    "我想改一下",
    "重新修改",
    "重新填寫",
    "edit_skin_record",
}
STABILITY_CONFIRM_WORDS = CONFIRM_WORDS | {"confirm_stable", "是，維持穩定", "維持穩定"}
OTHER_ISSUE_WORDS = {"其他", "其他狀況", "其他狀況…", "other"}
def _observe_skin_stage(stage: str, callback):
    # Time one external boundary without logging user content.
    started_at = time.perf_counter()
    status = 'success'
    try:
        return callback()
    except Exception:
        status = 'error'
        raise
    finally:
        observe_pipeline_stage('skin_tracking', stage, started_at, status=status)


STABILITY_DECLINE_WORDS = {
    "record_only",
    "先只記錄",
    "不是",
    "否",
    "不穩定",
    "不確定",
    "不知道",
    "不要",
}


class ChatModel(Protocol):
    """Optional text model used by report generation, not semantic extraction."""

    def chat(self, messages: list[dict], **kwargs) -> str: ...


class SkinRecordRepository(Protocol):
    def create(self, user_id: str, rec: dict) -> str: ...
    def query(self, user_id: str, date_from=None, date_to=None, issue=None, zone=None) -> list[dict]: ...
    def update(self, user_id: str, record_id: str, fields: dict) -> bool: ...
    def delete(self, user_id: str, record_id: str) -> bool: ...
    def check_eligibility(self, user_id: str, today=None, min_days: int = 5) -> dict: ...


class SkinTrackingAgent:

    def __init__(
        self,
        extractor: SkinExtractionProvider,
        store: SkinRecordRepository,
        report_model: ChatModel | None = None,
    ):
        """Dependencies are injected so the state machine stays deployment-neutral."""
        self.extractor = extractor
        self.store = store
        self.report_model = report_model

    # ─── 主入口：一輪對話 ───

    def handle(
        self,
        user_message: str,
        user_id: str,
        session: dict | None = None,
        initial_draft: dict | None = None,
        action: str | None = None,
    ) -> dict:
        ss = SkinSession.from_dict(session)
        msg = (user_message or "").strip()
        was_confirming_create = ss.phase == "confirm" and ss.pending_action is None
        skip_stability_prompt = False

        # Stability confirmation is independent from the final write confirmation.
        if ss.phase == "stability_confirm":
            if msg.lower() in STABILITY_CONFIRM_WORDS:
                self._mark_stability_confirmed(ss)
                self._clear_stability_confirmation(ss)
                return self._continue_create_flow(ss)
            if msg.lower() in STABILITY_DECLINE_WORDS:
                self._clear_stability_confirmation(ss)
                return self._continue_create_flow(ss)

            # Any other reply is not consent. Keep recorded and process its content once.
            self._clear_stability_confirmation(ss)
            skip_stability_prompt = True

        if msg.lower() in CANCEL_WORDS:
            return self._out(ss, "已取消這次膚況操作。", done=True)

        # Fixed issue chips are already structured user choices. They must never
        # pay the LLM/extractor latency or be reinterpreted by a stale session.
        if action == "record_other_issue":
            ss.phase = "describe_issue"
            ss.draft = {}
            ss.drafts = []
            ss.pending_location = None
            ss.pending_action = None
            ss.pending_record_id = None
            ss.pending_fields = {}
            ss.pending_stability_draft_indexes = []
            ss.concern_assessments = {}
            return self._out(ss, OTHER_ISSUE_QUESTION)

        if action == "record_issue":
            explicit_issues = [
                issue for issue in infer_explicit_issues(msg)
                if issue != Issue.other
            ]
            if not explicit_issues:
                ss.phase = "describe_issue"
                return self._out(ss, OTHER_ISSUE_QUESTION)

            tag_slugs, detail_zones = extract_observation_metadata(msg)
            ss.phase = "collect"
            ss.draft = RecordDraft(
                intent=TrackIntent.create,
                issue=explicit_issues[0],
                tag_slugs=tag_slugs,
                detail_zones=detail_zones,
            ).model_dump(mode="json")
            ss.drafts = [ss.draft]
            ss.pending_location = None
            ss.pending_action = None
            ss.pending_record_id = None
            ss.pending_fields = {}
            ss.pending_stability_draft_indexes = []
            ss.concern_assessments = {}
            return self._continue_create_flow(ss)

        # 最終確認畫面選擇「我想改一下」時，清除未儲存草稿並重新填寫。
        if ss.phase == "confirm" and msg.lower() in EDIT_WORDS:
            ss.phase = "collect"
            ss.draft = {}
            ss.drafts = []
            ss.last_listed = []
            ss.pending_action = None
            ss.pending_record_id = None
            ss.pending_fields = {}
            ss.pending_stability_draft_indexes = []
            ss.concern_assessments = {}

            return self._out(
                ss,
                (
                    "好的，這次紀錄還沒有儲存。"
                    "請重新描述想記錄的部位和肌膚狀況。"
                ),
            )

        # 確認是有副作用操作的唯一入口；其他文字視為修正內容，重新抽取。
        if ss.phase == "confirm" and msg.lower() in CONFIRM_WORDS:
            return self._execute_confirmed(ss, user_id)

        # 「其他」不是完整的膚況描述。先接住使用者，再讓下一句自由文字
        # 進入同一個語意抽取流程，而不是錯誤地直接追問臉部區域。
        current_drafts = ss.drafts or ([ss.draft] if ss.draft else [])
        issue_target = next(
            (draft for draft in current_drafts if draft.get("issue") is None),
            None,
        )
        if issue_target is not None and msg.lower() in OTHER_ISSUE_WORDS:
            ss.phase = "describe_issue"
            return self._out(ss, OTHER_ISSUE_QUESTION)

        # collect 快徑：純數字回答（「1」「3分」）直填第一個缺 severity 的草稿，
        # 免走 extract（GPT 對孤立數字抽不出 severity 會原地重問）。
        # fullmatch 嚴格限定純數字——「3天前開始」這類含數字的 onset 回答不會誤觸。
        if ss.phase == "collect":
            _drafts = ss.drafts or ([ss.draft] if ss.draft else [])
            # Always finish required severity values across every draft before
            # considering optional time data from an already-complete draft.
            _cur = next(
                (d for d in _drafts if d.get("severity") is None),
                None,
            )
            if _cur is None:
                _cur = next(
                    (
                        d for d in _drafts
                        if d.get("onset_date") is None
                        and d.get("duration_days") is None
                    ),
                    None,
                )
            if action == "record_location" and _cur is None:
                _action_zones = zones_from_action(msg)
                if _action_zones:
                    _cur = {}
                    _drafts = [_cur]
                    ss.drafts = _drafts
                    ss.draft = _cur
            if _cur is not None:
                _CN = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}

                # 固定部位按鈕不需要依賴 LLM 猜測；複選時將同一問題拆成多筆草稿。
                # Structured button actions are trusted values and bypass semantic
                # inference. Free text may ask one focused left/right/both question.
                if action == "record_location":
                    _action_zones = zones_from_action(msg)
                    if _cur.get("zone") is None and _action_zones:
                        return self._apply_zone_selection(ss, _drafts, _cur, _action_zones)
                    return self._out(
                        ss,
                        "這個部位選項已失效，請重新選擇一次。",
                        quick_replies=SLOT_QUICK_REPLIES.get("zone", []),
                    )

                _location = LocationParse()
                if ss.pending_location:
                    _location = resolve_pending_location(ss.pending_location, msg)
                if not _location.recognized:
                    _location = extract_face_locations(msg)

                if _cur.get("issue") is not None and _cur.get("zone") is None and _location.recognized:
                    if _location.needs_laterality:
                        if msg.strip() in {"眼周", "眼下"}:
                            # Short labels are fixed-chip choices, not free-form laterality.
                            ss.pending_location = None
                            return self._out(
                                ss,
                                "部位請直接使用下方固定按鈕選擇，可以複選。",
                                quick_replies=SLOT_QUICK_REPLIES.get("zone", []),
                            )
                        ss.pending_location = _location.pending
                        return self._out(
                            ss,
                            _location.clarification,
                            quick_replies=self._location_side_choices(),
                        )
                    return self._apply_location_parse(ss, _drafts, _cur, _location)

                if (
                    _cur.get("zone") is not None
                    and _location.recognized
                    and not _location.needs_laterality
                    and self._looks_like_concern_correction(msg)
                ):
                    return self._apply_location_parse(
                        ss, _drafts, _cur, _location, replace_existing=True
                    )
                _selected_zones = [
                    zone
                    for zone, label in ZONE_LABELS.items()
                    if label in msg
                ]
                if _cur.get("zone") is None and _selected_zones:
                    _current_index = _drafts.index(_cur)
                    _expanded = [
                        {**_cur, "zone": zone.value}
                        for zone in _selected_zones
                    ]
                    _drafts[_current_index:_current_index + 1] = _expanded
                    ss.drafts = _drafts
                    ss.draft = _drafts[0]
                    ss.concern_assessments.pop(f"{_current_index}:default", None)
                    return self._continue_create_flow(ss)

                if (
                    _cur.get("zone") is None
                    and _cur.get("issue") is not None
                    and not _selected_zones
                    and not self._looks_like_concern_correction(msg)
                ):
                    return self._out(
                        ss,
                        "部位請直接使用下方固定按鈕選擇，可以複選。",
                        quick_replies=SLOT_QUICK_REPLIES.get("zone", []),
                    )

                if _cur.get("severity") is None and re.fullmatch(r"[1-5]\s*分?", msg):
                    # 同一句描述出的左右對稱部位共用一次程度回答，但仍各存一筆。
                    severity = int(msg[0])
                    current_index = _drafts.index(_cur)
                    _cur["severity"] = severity
                    peer_index = self._bilateral_peer_index(_drafts, current_index)
                    if peer_index is not None and _drafts[peer_index].get("severity") is None:
                        _drafts[peer_index]["severity"] = severity
                    ss.drafts = _drafts
                    ss.draft = _drafts[0]
                    return self._continue_create_flow(ss)
                if (
                    _cur.get("severity") is None
                    and _cur.get("issue") is not None
                    and _cur.get("zone") is not None
                ):
                    current_index = _drafts.index(_cur)
                    current_draft = RecordDraft(**_cur)
                    peer_index = self._bilateral_peer_index(_drafts, current_index)
                    shared_bilateral_severity = (
                        peer_index is not None
                        and _drafts[peer_index].get("severity") is None
                    )
                    zone_label = (
                        "雙側臉頰區（含左右眼周）"
                        if shared_bilateral_severity
                        else ZONE_LABELS.get(current_draft.zone, "")
                    )
                    assessment = self._get_concern_assessment(
                        ss,
                        current_index,
                        current_draft,
                        zone_label_override=zone_label or None,
                    )
                    # 程度改由使用者自然描述後交給模型判斷，不再顯示 1～5 按鈕。
                    severity_replies = [{"label": "{}｜{}".format(item["level"], item["description"]), "value": str(item["level"])} for item in assessment["severity_levels"]]
                    if msg.strip("。.!！ ") in {"對", "是", "沒錯"}:
                        return self._out(ss, "請選下方最接近的 1～5 描述，也可以直接用自己的話補充。", quick_replies=severity_replies)
                    interpreter = getattr(self.extractor, "interpret_severity", None)
                    if callable(interpreter):
                        try:
                            interpreted = _observe_skin_stage(
                                'severity_interpretation',
                                lambda: interpreter(
                                    concern_summary=assessment["concern_summary"],
                                    zone_label=zone_label,
                                    user_description=msg,
                                    severity_levels=assessment["severity_levels"],
                                ),
                            )
                            decision = interpreted.model_dump(mode="json")
                        except Exception:
                            return self._out(
                                ss,
                                "我還需要多了解一點，請直接用自己的話補充範圍、疼痛、持續時間或對日常的影響。",
                                quick_replies=severity_replies,
                            )

                    else:
                        # Backward-compatible fallback for extractors that predate
                        # natural-language severity interpretation.
                        extracted_list = _observe_skin_stage(
                            'fallback_message_extract',
                            lambda: extract_skin_drafts_from_message(self.extractor, [], msg),
                        )
                        extracted = extracted_list[0] if extracted_list else {}
                        fallback_severity = extracted.get("severity")
                        if fallback_severity is None:
                            return self._out(
                                ss,
                                SLOT_QUESTIONS["severity"],
                                quick_replies=severity_replies,
                            )
                        for timing_field in ("onset_date", "duration_days"):
                            if extracted.get(timing_field) is not None:
                                _cur[timing_field] = extracted[timing_field]
                        decision = {
                            "severity": fallback_severity,
                            "understood_description": msg,
                            "needs_clarification": False,
                            "clarifying_question": None,
                        }

                    if decision["needs_clarification"]:
                        return self._out(
                            ss,
                            decision["clarifying_question"],
                            quick_replies=severity_replies,
                        )

                    severity = int(decision["severity"])
                    affected_indexes = {current_index}
                    if peer_index is not None and _drafts[peer_index].get("severity") is None:
                        affected_indexes.add(peer_index)
                    for index in affected_indexes:
                        draft = _drafts[index]
                        draft["severity"] = severity
                        existing_note = (draft.get("note") or "").strip()
                        if msg not in existing_note:
                            draft["note"] = f"{existing_note}；{msg}".strip("；")
                    ss.drafts = _drafts
                    ss.draft = _drafts[0]

                    next_step = self._continue_create_flow(ss)
                    if next_step.get("stage") != "skin_tracking_confirm":
                        return next_step

                    issue_label = (
                        assessment["concern_summary"]
                        if current_draft.issue == Issue.other
                        else ISSUE_LABELS.get(current_draft.issue, assessment["concern_summary"])
                    )
                    severity_label = {1: "輕微", 2: "輕微", 3: "中度", 4: "嚴重", 5: "嚴重"}[severity]
                    understood = decision["understood_description"] or msg
                    return self._out(
                        ss,
                        (
                            f"依照你的補充，我理解為「{understood}」。\n\n"
                            f"我會將這次狀況記為「{issue_label}，程度{severity_label}」，"
                            f"並補充「{msg}」。這樣記可以嗎？"
                        ),
                        quick_replies=CONFIRM_QUICK_REPLIES,
                    )

                if _cur.get("severity") is not None:
                    # 固定時間按鈕直接填值，避免模型漏抽取後重複同一題。
                    _quick_time_days = {
                        "今天開始": 0,
                        "3天前開始": 3,
                        "一週前開始": 7,
                        "一個月以上": 30,
                    }
                    if msg in _quick_time_days:
                        _cur["duration_days"] = _quick_time_days[msg]
                        ss.drafts = _drafts
                        ss.draft = _drafts[0]
                        return self._continue_create_flow(ss)

                    # 正在問 onset：只有明確包含「天」才視為 duration_days。
                    _m = re.fullmatch(r"([一二三四五六七八九十\d]+)\s*天", msg)
                    if _m:
                        _tok = _m.group(1)
                        _days = int(_tok) if _tok.isdigit() else _CN.get(_tok)
                        if _days:
                            _cur["duration_days"] = _days
                            ss.draft = _drafts[0]
                            return self._continue_create_flow(ss)

        if initial_draft and not ss.draft:
            ss.draft = self._safe_draft({**initial_draft, "intent": "create"})
            ss.drafts = [ss.draft]

        # 特殊流程：列表中的「第N筆」刪除/修改（若已有歷史查詢列表）
        nth = NTH_RE.search(msg)
        if nth and ss.last_listed:
            return self._handle_nth(ss, user_id, int(nth.group(1)), msg)

        # A free-text reply can correct the concern itself. Any earlier model
        # assessment must be discarded so the next scale is reasoned from the
        # corrected description instead of reusing stale wording.
        ss.concern_assessments = {}

        # The extractor reads only this message; the merge tool preserves prior slots.
        current_drafts = getattr(ss, "drafts", None) or ([ss.draft] if ss.draft else [])
        extraction = _observe_skin_stage(
            'message_extract',
            lambda: extract_skin_drafts_with_metadata(self.extractor, current_drafts, msg),
        )
        extracted_drafts = extraction.drafts
        explicit_issues = infer_explicit_issues(msg)
        if explicit_issues:
            # Preserve every provider draft for an explicit issue. The same concern
            # can legitimately produce multiple structured zones (for example both
            # cheeks), so collapsing by issue loses bilateral state.
            normalized_drafts = []
            for issue in explicit_issues:
                matches = [
                    draft
                    for draft in extracted_drafts
                    if draft.get("issue") == issue
                ]
                zoned_matches = [draft for draft in matches if draft.get("zone") is not None]
                if zoned_matches:
                    matches = zoned_matches
                normalized_drafts.extend(
                    matches
                    or [
                        RecordDraft(
                            intent=TrackIntent.create,
                            issue=issue,
                        ).model_dump(mode="json")
                    ]
                )
            extracted_drafts = normalized_drafts
        # The provider may suggest one of the formal observation tags. Validate
        # it against the curated 100-item catalogue, add deterministic aliases,
        # and never promote an appearance word to a diagnosis. For multi-concern
        # messages we keep the provider's per-draft attribution unchanged rather
        # than copying one detected label onto every concern.
        if len(extracted_drafts) == 1:
            draft = extracted_drafts[0]
            tag_slugs, detail_zones = extract_observation_metadata(
                msg,
                suggested_tag_slugs=draft.get("tag_slugs"),
                suggested_detail_zones=draft.get("detail_zones"),
            )
            draft["tag_slugs"] = tag_slugs
            draft["detail_zones"] = detail_zones
        location_parse = extract_face_locations(msg)
        if location_parse.needs_laterality:
            # Keep the extracted concern and resume the normal slot flow. Unknown
            # symptoms are explained once; known concerns skip model assessment.
            ss.drafts = extracted_drafts
            ss.draft = extracted_drafts[0] if extracted_drafts else {}
            ss.pending_location = None
            return self._continue_create_flow(ss)
        if location_parse.recognized:
            extracted_drafts = self._enrich_drafts_with_locations(
                extracted_drafts, location_parse
            )
        zoned_issues = {
            draft.get("issue")
            for draft in extracted_drafts
            if draft.get("zone") is not None
        }
        if zoned_issues:
            extracted_drafts = [
                draft
                for draft in extracted_drafts
                if draft.get("zone") is not None
                or draft.get("issue") not in zoned_issues
            ]

        ss.pending_location = None

        # 同時寫回 ss.draft (第一筆) 與 ss.drafts (完整列表)，維護雙向相容性
        ss.drafts = extracted_drafts
        ss.draft = extracted_drafts[0] if extracted_drafts else {}

        # 確認卡上的自然語言修正仍屬於原本的新增操作，不應被「改成」誤判成歷史更新。
        if was_confirming_create:
            for draft in ss.drafts:
                draft["intent"] = TrackIntent.create.value
            ss.draft = ss.drafts[0]

        first_draft_obj = RecordDraft(**ss.draft)

        # 2) 意圖路由 (Intent Routing)
        if first_draft_obj.intent == TrackIntent.report:
            return self._do_report(ss, user_id)

        if first_draft_obj.intent == TrackIntent.query:
            return self._do_query(ss, user_id)

        if first_draft_obj.intent in (TrackIntent.delete, TrackIntent.update) and not ss.last_listed:
            out = self._do_query(ss, user_id)
            out["reply"] += "\n\n要刪除或修改哪一筆呢？跟我說「第幾筆」即可。"
            return out

        if extraction.ambiguous_stability_indexes and not skip_stability_prompt:
            ss.pending_stability_draft_indexes = extraction.ambiguous_stability_indexes
            ss.phase = "stability_confirm"
            return self._out(
                ss,
                STABILITY_CONFIRM_QUESTION,
                quick_replies=STABILITY_CONFIRM_QUICK_REPLIES,
                cards=[
                    self._choice_card(
                        "確認膚況狀態",
                        STABILITY_CONFIRM_QUICK_REPLIES,
                    )
                ],
            )

        return self._continue_create_flow(ss)

    @staticmethod
    def _location_side_choices() -> list[dict]:
        return [
            {"label": "左邊", "value": "左邊"},
            {"label": "右邊", "value": "右邊"},
            {"label": "兩邊都有", "value": "兩邊都有"},
        ]

    def _apply_zone_selection(
        self,
        ss: SkinSession,
        drafts: list[dict],
        current: dict,
        zones: list[Zone],
    ) -> dict:
        """Apply trusted quick-reply values without invoking the extractor."""

        current_index = drafts.index(current)
        expanded = [{**current, "zone": zone.value} for zone in zones]
        drafts[current_index : current_index + 1] = expanded
        ss.drafts = drafts
        ss.draft = drafts[0]
        ss.pending_location = None
        ss.concern_assessments.pop(f"{current_index}:default", None)
        return self._continue_create_flow(ss)

    @staticmethod
    def _enrich_drafts_with_locations(
        drafts: list[dict],
        location: LocationParse,
    ) -> list[dict]:
        """Map precise anatomy onto compatible four-zone record drafts."""

        resolved = [item for item in location.locations if item.macro_zones]
        if not drafts or not resolved:
            return drafts

        def expand(base: dict, items: list) -> list[dict]:
            grouped: dict[Zone, list[str]] = {}
            for item in items:
                for zone in item.macro_zones:
                    grouped[zone] = merge_detail_zones(
                        grouped.get(zone, []), item.detail_zones
                    )
            return [
                {
                    **base,
                    "zone": zone.value,
                    "detail_zones": merge_detail_zones(
                        base.get("detail_zones") or [], details
                    ),
                }
                for zone, details in grouped.items()
            ]

        if len(drafts) == 1:
            return expand(drafts[0], resolved)

        enriched: list[dict] = []
        for index, draft in enumerate(drafts):
            if index < len(resolved):
                enriched.extend(expand(draft, [resolved[index]]))
            else:
                enriched.append(draft)
        return enriched

    def _apply_location_parse(
        self,
        ss: SkinSession,
        drafts: list[dict],
        current: dict,
        location: LocationParse,
        *,
        replace_existing: bool = False,
    ) -> dict:
        current_index = drafts.index(current)
        source = dict(current)
        if replace_existing:
            source["zone"] = None
            source["detail_zones"] = []
        expanded = self._enrich_drafts_with_locations([source], location)
        if not expanded:
            return self._continue_create_flow(ss)
        drafts[current_index : current_index + 1] = expanded
        ss.drafts = drafts
        ss.draft = drafts[0]
        ss.pending_location = None
        ss.concern_assessments.pop(f"{current_index}:default", None)
        return self._continue_create_flow(ss)

    def _continue_create_flow(self, ss: SkinSession) -> dict:
        """Advance slot filling after extraction or stability resolution."""
        first_draft_obj = RecordDraft(**ss.draft)

        target_draft = None
        target_idx = 0

        for idx, d_dict in enumerate(ss.drafts):
            d_obj = RecordDraft(**d_dict)
            if d_obj.missing_slots():
                target_draft = d_obj
                target_idx = idx
                break

        if target_draft:
            missing = target_draft.missing_slots()
            slot = missing[0]
            ss.phase = "collect"

            issue_phrase = self._issue_phrase(target_draft)
            zone_name = ZONE_LABELS.get(target_draft.zone, "") if target_draft.zone else ""
            bilateral_peer_index = self._bilateral_peer_index(ss.drafts, target_idx)
            shared_bilateral_severity = (
                slot == "severity"
                and bilateral_peer_index is not None
                and ss.drafts[bilateral_peer_index].get("severity") is None
            )

            if slot == "issue":
                question = SLOT_QUESTIONS[slot]
            elif slot == "zone":
                assessment = self._get_concern_assessment(ss, target_idx, target_draft)
                question = (
                    f"照你的描述，我先把這個膚況理解成「{assessment['concern_summary']}」。"
                    "這是依你提供的外觀與感受整理出的症狀描述，還不是確診。"
                    f"這樣理解對嗎？{assessment['clarifying_question']}"
                "請直接使用下方固定部位按鈕選擇，可以複選。"
                )
            elif slot == "severity":
                assessment = self._get_concern_assessment(
                    ss,
                    target_idx,
                    target_draft,
                    zone_label_override=(
                        "雙側臉頰區（含左右眼周）"
                        if shared_bilateral_severity
                        else None
                    ),
                )
                described_problem = assessment["concern_summary"]
                severity_zone_label = (
                    "雙側臉頰"
                    if shared_bilateral_severity
                    else zone_name.split("（", 1)[0].removesuffix("區")
                )
                if (
                    severity_zone_label
                    and severity_zone_label.replace(" ", "")
                    not in described_problem.replace(" ", "")
                ):
                    described_problem = f"{severity_zone_label}{described_problem}"
                question = (
                    f"我確認一下，你描述的是「{described_problem}」，對嗎？"
                    "如果我理解得不太準，可以直接打字修正；如果沒問題，"
                    "請選一個最接近現在狀況的描述，也可以直接用自己的話補充。"
                )
                if shared_bilateral_severity:
                    question += (
                        "左右兩側程度差不多的話只要選一次；"
                        "如果不同，也可以直接告訴我哪一側比較明顯。"
                    )
            else:
                question = SLOT_QUESTIONS[slot]

            if len(ss.drafts) > 1 and target_idx > 0 and not shared_bilateral_severity:
                question = f"再看另一個部位：{question}"
            if slot == "severity":
                quick_replies = [{"label": "{}｜{}".format(item["level"], item["description"]), "value": str(item["level"])} for item in assessment["severity_levels"]]
            elif slot == "zone":
                quick_replies = self._zone_quick_replies(target_draft)
            else:
                quick_replies = SLOT_QUICK_REPLIES.get(slot, [])

            return self._out(
                ss,
                question,
                quick_replies=quick_replies,
            )

        # 時間與使用產品仍會在使用者主動提到時保存，但不再強迫逐題回答。
        # 4) 核心欄位齊全後，用一段自然摘要確認再寫入。
        ss.phase = "confirm"
        return self._confirm_card(ss)

    @staticmethod
    def _zone_quick_replies(draft: RecordDraft) -> list[dict]:
        """Keep choices relevant to the location described by the user."""
        choices = SLOT_QUICK_REPLIES.get("zone", [])
        note = draft.note.strip()
        if any(term in note for term in ("眼下", "眼睛下面", "眼瞼", "眼皮", "眼周")):
            return [choice for choice in choices if "眼周" in choice["label"]]
        if any(term in note for term in ("臉頰", "兩頰", "雙頰")):
            return [choice for choice in choices if "臉頰區" in choice["label"]]
        return choices

    @staticmethod
    def _looks_like_concern_correction(message: str) -> bool:
        """Allow correcting the concern, while never accepting a typed location."""
        text = (message or "").strip()
        if any(term in text for term in ("不是", "不對", "改成", "其實", "應該是")):
            return True
        common_issues = (
            "痘痘", "粉刺", "乾燥", "脫皮", "泛紅", "紅腫", "出油", "搔癢",
            "斑點", "色斑", "痘疤", "痘印",
        )
        return any(term in text for term in common_issues) or any(
            label in text for label in ISSUE_LABELS.values()
        )

    @staticmethod
    def _bilateral_peer_index(drafts: list[dict], draft_index: int) -> int | None:
        """Return the matching opposite cheek produced from the same user concern."""
        if not 0 <= draft_index < len(drafts):
            return None
        target = RecordDraft(**drafts[draft_index])
        opposite = {
            Zone.left_cheek: Zone.right_cheek,
            Zone.right_cheek: Zone.left_cheek,
        }.get(target.zone)
        if opposite is None:
            return None

        for index, candidate_dict in enumerate(drafts):
            if index == draft_index:
                continue
            candidate = RecordDraft(**candidate_dict)
            if (
                candidate.zone == opposite
                and candidate.issue == target.issue
                and candidate.note.strip() == target.note.strip()
                and candidate.onset_date == target.onset_date
                and candidate.duration_days == target.duration_days
                and candidate.products == target.products
                and candidate.record_status == target.record_status
                and candidate.stability_source == target.stability_source
            ):
                return index
        return None

    def _get_concern_assessment(
        self,
        ss: SkinSession,
        draft_index: int,
        draft: RecordDraft,
        zone_label_override: str | None = None,
    ) -> dict:
        """Generate once per draft; keep only the conclusion, never chain-of-thought."""
        cache_key = f"{draft_index}:{zone_label_override or 'default'}"
        cached = ss.concern_assessments.get(cache_key)
        if cached:
            return cached

        issue_label = ISSUE_LABELS.get(draft.issue, "目前描述的膚況")
        zone_label = zone_label_override or (
            ZONE_LABELS.get(draft.zone, "") if draft.zone else ""
        )
        detail_labels = detail_location_labels(draft.zone, draft.detail_zones)
        display_zone_label = (
            f"{zone_label}（細部：{'、'.join(detail_labels)}）"
            if zone_label and detail_labels
            else ("、".join(detail_labels) if detail_labels else zone_label)
        )
        description = draft.note.strip() or issue_label
        reasoner = getattr(self.extractor, "assess_concern", None)
        # A known concern with an unresolved left/right location only needs the
        # fixed zone selector. A second model call adds latency without changing
        # the stored concern, severity, or location choices. Keep model reasoning
        # for uncategorised concerns and for concern-specific severity scales.
        should_reason = callable(reasoner) and (
            draft.zone is not None or draft.issue == Issue.other
        )

        if should_reason:
            try:
                reasoned = _observe_skin_stage(
                    'concern_assessment',
                    lambda: reasoner(
                        issue_label=issue_label,
                        user_description=description,
                        zone_label=zone_label,
                    ),
                )
                assessment = reasoned.model_dump(mode="json")
                if detail_labels and not assessment.get("concern_summary"):
                    assessment["concern_summary"] = f"{display_zone_label}有{self._issue_phrase(draft)}"
                ss.concern_assessments[cache_key] = assessment
                return assessment
            except Exception:
                # Recording should remain available during a temporary model outage.
                pass

        summary = (
            self._issue_phrase(draft)
            if not display_zone_label
            else f"{display_zone_label}有{self._issue_phrase(draft)}"
        )
        assessment = {
            "concern_summary": summary,
            "clarifying_question": "這個狀況主要出現在哪個部位呢？",
            "severity_levels": [
                {"level": 1, "description": "很輕微，幾乎不影響日常"},
                {"level": 2, "description": "有一點感覺，但不太困擾"},
                {"level": 3, "description": "狀況明顯，會特別注意"},
                {"level": 4, "description": "程度較強，已影響舒適感"},
                {"level": 5, "description": "非常明顯，已影響日常"},
            ],
        }
        ss.concern_assessments[cache_key] = assessment
        return assessment

    @staticmethod
    def _issue_phrase(draft: RecordDraft) -> str:
        """Use the user's own words for uncategorised concerns."""
        if draft.issue == Issue.other and draft.note.strip():
            phrase = draft.note.strip().rstrip("。！!？?")
            return phrase if len(phrase) <= 32 else f"{phrase[:31]}…"
        return ISSUE_LABELS.get(draft.issue, "這個狀況")

    @staticmethod
    def _mark_stability_confirmed(ss: SkinSession) -> None:
        """Apply agent-confirmed stability only to drafts named by the pending prompt."""
        for index in ss.pending_stability_draft_indexes:
            if not 0 <= index < len(ss.drafts):
                continue
            draft = {
                **ss.drafts[index],
                "record_status": RecordStatus.stable.value,
                "stability_source": StabilitySource.agent_confirmed.value,
            }
            ss.drafts[index] = RecordDraft(**draft).model_dump(mode="json")
        ss.draft = ss.drafts[0] if ss.drafts else {}

    @staticmethod
    def _clear_stability_confirmation(ss: SkinSession) -> None:
        ss.pending_stability_draft_indexes = []
        ss.phase = "collect"

    # ─── 執行與存取 ───

    def _execute_confirmed(self, ss: SkinSession, user_id: str) -> dict:
        if ss.pending_action == "delete":
            ok = self.store.delete(user_id, ss.pending_record_id)
            return self._out(
                ss,
                "已刪除該筆紀錄。" if ok else "找不到這筆紀錄，可能已被刪除。",
                done=True,
                extra={"operation": {"type": "delete", "ok": ok, "record_id": ss.pending_record_id}},
            )

        if ss.pending_action == "update":
            ok = self.store.update(user_id, ss.pending_record_id, ss.pending_fields)
            return self._out(
                ss,
                "已更新紀錄！" if ok else "更新失敗，找不到這筆紀錄。",
                done=True,
                extra={"operation": {"type": "update", "ok": ok, "record_id": ss.pending_record_id}},
            )

        # 🚀 呼叫獨立 Save Tool，批次存入 ss.drafts 中的所有草稿紀錄
        save_messages = []
        all_quick_replies = []
        saved_records = []
        latest_eligibility = None

        drafts_to_save = getattr(ss, "drafts", None) or [ss.draft]
        for d in drafts_to_save:
            save_result = save_skin_record(self.store, user_id, d)
            save_messages.append(save_result["message"])
            saved_records.append({"record_id": save_result["record_id"], **d})
            latest_eligibility = save_result["eligibility"]
            if save_result.get("quick_replies"):
                all_quick_replies.extend(save_result["quick_replies"])

        unique_quick_replies = []
        seen_actions = set()
        for item in all_quick_replies:
            key = (item.get("label"), item.get("value"))
            if key not in seen_actions:
                seen_actions.add(key)
                unique_quick_replies.append(item)

        return self._out(
            ss,
            reply=save_messages[-1] if save_messages else "膚況已完成記錄。",
            quick_replies=unique_quick_replies,
            done=True,
            extra={"records": saved_records, "eligibility": latest_eligibility},
        )

    def _do_report(self, ss: SkinSession, user_id: str) -> dict:
        d_from = Date.today() - timedelta(days=90)
        recs = self.store.query(user_id, str(d_from), str(Date.today()))
        if not recs:
            return self._out(
                ss,
                "目前還沒有可整理的膚況紀錄，先完成一次膚況紀錄就能查看報告。",
                done=True,
            )

        return self._out(
            ss,
            "文字訊息不再重複產生整份分析；圖表、趨勢與照護重點都整理在膚況分析報告頁。",
            quick_replies=[
                {
                    "label": "前往膚況分析報告",
                    "value": "open_skin_report",
                }
            ],
            done=True,
        )

    def _do_query(self, ss: SkinSession, user_id: str) -> dict:
        d_from = Date.today() - timedelta(days=14)
        recs = self.store.query(user_id, str(d_from))
        if not recs:
            return self._out(ss, "最近 14 天沒有膚況紀錄。要開始記錄今天的膚況嗎?", done=True)

        ss.last_listed = [r["record_id"] for r in recs]
        lines = ["最近 14 天的膚況紀錄："]
        for i, r in enumerate(recs, 1):
            lines.append(
                f"{i}. {r['date']}｜{ISSUE_LABELS.get(r['issue'], r['issue'])}｜"
                f"{ZONE_LABELS.get(r['zone'], r['zone'])}｜嚴重度 {r.get('severity', '—')}/5"
            )
        lines.append("\n要修改或刪除可以說「第2筆改成4分」「刪掉第3筆」。")
        return self._out(ss, "\n".join(lines), extra={"records": recs})

    def _handle_nth(self, ss: SkinSession, user_id: str, n: int, msg: str) -> dict:
        if not (1 <= n <= len(ss.last_listed)):
            return self._out(ss, f"沒有第 {n} 筆喔，目前列表只有 {len(ss.last_listed)} 筆。")
        rid = ss.last_listed[n - 1]

        if "刪" in msg:
            ss.phase, ss.pending_action, ss.pending_record_id = (
                "confirm",
                "delete",
                rid,
            )
            return self._out(
                ss,
                f"確定要刪除第 {n} 筆紀錄嗎？回「確認」就刪除。",
                quick_replies=CONFIRM_QUICK_REPLIES,
                cards=[self._choice_card("確認刪除紀錄", CONFIRM_QUICK_REPLIES)],
            )

        # Updates use the same structured extractor but only allow mutable record fields.
        extracted_list = _observe_skin_stage(
            'update_message_extract',
            lambda: extract_skin_drafts_from_message(self.extractor, [], msg),
        )
        extracted = extracted_list[0] if extracted_list else {}

        fields = {
            k: v
            for k, v in extracted.items()
            if k in (
                "issue",
                "severity",
                "zone",
                "tag_slugs",
                "detail_zones",
                "onset_date",
                "duration_days",
                "note",
                "record_status",
                "stability_source",
            )
            and v not in (None, "", [])
        }
        if not fields:
            return self._out(ss, "要把它改成什麼呢？例如：「第2筆改成4分」「第2筆部位改下巴」。")

        ss.phase, ss.pending_action, ss.pending_record_id = (
            "confirm",
            "update",
            rid,
        )
        ss.pending_fields = fields
        pretty = "、".join(f"{k}→{v}" for k, v in fields.items())
        return self._out(
            ss,
            f"要把第 {n} 筆改成：{pretty}，對嗎？回「確認」就更新。",
            quick_replies=CONFIRM_QUICK_REPLIES,
            cards=[self._choice_card("確認更新紀錄", CONFIRM_QUICK_REPLIES)],
        )

    # ─── 輸出格式化 ───

    def _confirm_card(self, ss: SkinSession) -> dict:
        drafts = getattr(ss, "drafts", None) or [ss.draft]
        summaries = []
        handled_indexes = set()

        for index, d_dict in enumerate(drafts):
            if index in handled_indexes:
                continue
            d = RecordDraft(**d_dict)
            peer_index = self._bilateral_peer_index(drafts, index)
            peer = RecordDraft(**drafts[peer_index]) if peer_index is not None else None
            if peer is not None and peer.severity == d.severity:
                zone_label = "雙側臉頰區（含左右眼周）"
                handled_indexes.add(peer_index)
            else:
                zone_label = ZONE_LABELS.get(d.zone, "未指定部位")

            issue_label = self._issue_phrase(d)
            severity_label = {1: "輕微", 2: "輕微", 3: "中度", 4: "嚴重", 5: "嚴重"}[d.severity]
            summary = f"{zone_label}有「{issue_label}」的狀況，目前程度{severity_label}"
            detail_labels = detail_location_labels(d.zone, d.detail_zones)
            if detail_labels:
                summary += f"，細部標籤：{'、'.join(detail_labels)}"
            observation_labels = [tag_label(slug) for slug in d.tag_slugs]
            if observation_labels:
                summary += f"，膚況標籤：{'、'.join(observation_labels)}"

            if d.onset_date:
                summary += f"，從 {d.onset_date} 左右開始"
            elif d.duration_days is not None:
                summary += "，今天才注意到" if d.duration_days == 0 else f"，大約持續 {d.duration_days} 天"

            if d.products:
                summary += f"，最近有用{'、'.join(d.products)}"
            if d.record_status == RecordStatus.stable:
                summary += "，最近大致維持穩定"
            summaries.append(summary)

        if len(summaries) == 1:
            reply = f"好，我先幫你整理成：{summaries[0]}。如果沒問題，我就存進今天的紀錄。"
        else:
            joined = "；".join(
                f"第 {index} 項是{summary}"
                for index, summary in enumerate(summaries, 1)
            )
            reply = f"好，我整理成 {len(summaries)} 項：{joined}。這樣記可以嗎？"

        return self._out(
            ss,
            reply,
            quick_replies=CONFIRM_QUICK_REPLIES,
        )

    @staticmethod
    def _choice_card(title: str, options: list[dict]) -> dict:
        return {
            "type": "choice",
            "title": title,
            "options": options,
        }

    @staticmethod
    def _safe_draft(d: dict) -> dict:
        try:
            return RecordDraft(
                **{k: v for k, v in d.items() if k in RecordDraft.model_fields}
            ).model_dump(mode="json")
        except Exception:
            return {}

    @staticmethod
    def _out(
        ss: SkinSession,
        reply: str,
        quick_replies: list = None,
        cards: list = None,
        done: bool = False,
        extra: dict = None,
    ) -> dict:
        out = {
            "reply": reply,
            "quick_replies": quick_replies or [],
            "cards": cards or [],
            "stage": f"skin_tracking_{ss.phase}",
            "done": done,
            "session": None if done else ss.to_dict(),
        }
        if extra:
            out.update(extra)
        return out


# ─── 舊 Root 呼叫點的薄相容層（必須由組裝層先 configure） ───

_agent: SkinTrackingAgent | None = None


def configure(
    extractor: SkinExtractionProvider,
    store: SkinRecordRepository,
    report_model: ChatModel | None = None,
) -> SkinTrackingAgent:
    """Configure the compatibility entrypoint from the application composition layer."""
    global _agent
    _agent = SkinTrackingAgent(extractor, store, report_model)
    return _agent


def reset_configuration() -> None:
    global _agent
    _agent = None


def run(
    user_message: str,
    user_id: str = "anonymous",
    session: dict | None = None,
    initial_draft: dict | None = None,
    action: str | None = None,
) -> dict:
    if _agent is None:
        raise RuntimeError(
            "Skin Tracking 尚未設定。請由組裝層先呼叫 "
            "agents.skin_tracking.tracking_agent.configure(extractor, store)。"
        )
    return _agent.handle(user_message, user_id, session, initial_draft, action)

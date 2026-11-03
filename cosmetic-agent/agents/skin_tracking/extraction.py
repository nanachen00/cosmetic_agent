"""Structured, message-local Skin Tracking extraction.

RECONSTRUCTED 2026-10-21. The original file was omitted from the source export.
The public contract is reproduced from:
  - test/test_skin_extraction_provider.py, test/test_skin_zone_extraction.py
  - tests/regression/test_observation_tag_terra_severity.py
  - agents/skin_tracking/tracking_agent.py, tools/agent/tracking/skin_extract_tool.py

The extractor talks to an OpenAI-compatible Responses API with Structured
Outputs. By default that is OpenAI (model ``gpt-5.6-terra``). Setting
SKIN_EXTRACTION_BASE_URL points it at a local vLLM server instead.
"""

from __future__ import annotations

import os
from datetime import date as Date
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from agents.skin_tracking.observation_taxonomy import (
    allowed_detail_zones,
    catalog_by_slug,
    extract_observation_metadata,
    tag_label,
)
from schemas.tracking_schema import Issue, TrackIntent, Zone

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_EXTRACTION_MODEL = "gpt-5.6-terra"


# ── errors ─────────────────────────────────────────────────────────
class SkinExtractionError(RuntimeError):
    """Base class for extraction failures."""


class SkinExtractionConfigurationError(SkinExtractionError):
    """Raised when the provider cannot be configured (for example no API key)."""


class SkinExtractionProviderError(SkinExtractionError):
    """Raised when the model call fails or returns unusable output."""


# ── structured output models ───────────────────────────────────────
class StabilitySignal(str, Enum):
    stable_explicit = "stable_explicit"      # user clearly says it is stable / unchanged
    stable_ambiguous = "stable_ambiguous"    # sounds stable, needs confirmation
    not_stable = "not_stable"                # user describes change / flare


class ExtractedSkinRecord(BaseModel):
    """One concern mentioned in a single message; absent fields stay None."""

    intent: Optional[TrackIntent] = None
    issue: Optional[Issue] = None
    zone: Optional[Zone] = None
    severity: Optional[int] = Field(None, ge=1, le=5)
    tag_slugs: Optional[list[str]] = None
    detail_zones: Optional[list[str]] = None
    onset_date: Optional[Date] = None
    duration_days: Optional[int] = Field(None, ge=0)
    products: Optional[list[str]] = None
    note: Optional[str] = None
    stability_signal: Optional[StabilitySignal] = None

    @field_validator("tag_slugs", mode="before")
    @classmethod
    def _known_tags_only(cls, value):
        if value is None:
            return None
        known = catalog_by_slug()
        return list(dict.fromkeys(str(v).strip() for v in value if str(v).strip() in known))

    @field_validator("detail_zones", mode="before")
    @classmethod
    def _known_zones_only(cls, value):
        if value is None:
            return None
        allowed = allowed_detail_zones()
        return list(dict.fromkeys(str(v).strip() for v in value if str(v).strip() in allowed))


class SkinExtractionResult(BaseModel):
    records: list[ExtractedSkinRecord] = Field(default_factory=list)


class ConcernSeverityLevel(BaseModel):
    level: int = Field(..., ge=1, le=5)
    description: str = Field(..., min_length=1, max_length=80)


class SkinConcernAssessment(BaseModel):
    concern_summary: str = Field(..., min_length=1, max_length=120)
    clarifying_question: str = Field(..., min_length=1, max_length=200)
    severity_levels: list[ConcernSeverityLevel]

    @model_validator(mode="after")
    def _exactly_five_ordered_levels(self):
        if [item.level for item in self.severity_levels] != [1, 2, 3, 4, 5]:
            raise ValueError("severity_levels must contain levels 1-5 in order")
        return self


class SeverityInterpretation(BaseModel):
    severity: Optional[int] = Field(None, ge=1, le=5)
    understood_description: Optional[str] = None
    needs_clarification: bool = False
    clarifying_question: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.needs_clarification and not (self.clarifying_question or "").strip():
            raise ValueError("clarifying_question is required when clarification is needed")
        if not self.needs_clarification and self.severity is None:
            raise ValueError("severity is required when no clarification is needed")
        return self


class SkinExtractionProvider(Protocol):
    def extract(self, message: str, *, today: Date) -> SkinExtractionResult: ...


# ── prompts ────────────────────────────────────────────────────────
ZONE_EXTRACTION_RULES = """【部位對照規則】
- 左右一律以使用者本人的左右為準，不是觀看者的左右。
- t_zone：額頭、眉心、鼻樑、鼻翼、鼻頭、鼻尖。
- left_cheek：左臉頰、左眼周（含左眼下、左眼尾）。
- right_cheek：右臉頰、右眼周（含右眼下、右眼尾）。
- chin：下巴、人中、嘴周、嘴角、上下唇周圍。
- 說「兩頰」「雙頰」「兩邊臉頰」時，輸出兩筆紀錄：left_cheek 與 right_cheek。
- 只說「眼下」「眼周」「臉頰」而沒有說左邊或右邊時，不要猜，zone 保持 null。
- 太陽穴、下顎線、耳朵、髮際線、脖子不屬於上述四區；zone 保持 null，細部位置放入 detail_zones。
- 「全臉」「整張臉」不是單一區域，zone 保持 null。"""

EXTRACTION_INSTRUCTIONS = (
    "你是 BLINK AI 膚況紀錄的結構化抽取器。今天日期是 {today}。\n"
    "只根據使用者這一則訊息抽取膚況紀錄，不要推測沒有說出的資訊；沒有提到的欄位一律為 null。\n"
    "一則訊息可能包含多個膚況或多個部位，每個（膚況, 部位）組合輸出一筆 records。\n"
    "欄位說明：\n"
    "- intent：create（要記錄）、query（查詢紀錄）、update、delete、report（要報告）、unknown。\n"
    "- issue：acne 痘痘、dryness 乾燥、redness 泛紅、oiliness 出油、itching 搔癢、"
    "pigmentation 斑點、acne_scar 痘疤、other 其他；無法歸類但確實是膚況時用 other。\n"
    "- severity：使用者明確給的 1～5 分；沒有給分數就是 null，不要自行評分。\n"
    "- onset_date：依今天日期換算「昨天」「三天前」等，格式 YYYY-MM-DD。\n"
    "- duration_days：持續天數。products：使用者提到正在使用的產品名稱。\n"
    "- note：用使用者原本的說法簡短描述膚況。\n"
    "- tag_slugs、detail_zones：只有非常確定時才填，否則 null。\n"
    "- stability_signal：使用者明確說「穩定」「跟之前一樣」「沒有變化」→ stable_explicit；"
    "語意像穩定但不確定 → stable_ambiguous；明確說變嚴重、變好或剛發作 → not_stable；未提及 → null。\n"
    "- 不做醫療診斷；使用者沒有親口說出疾病名稱時，不要輸出疾病。\n\n"
    + ZONE_EXTRACTION_RULES.replace("{", "{{").replace("}", "}}")
)

CONCERN_ASSESSMENT_INSTRUCTIONS = """你是 BLINK AI 的膚況理解助理，這不是診斷，也不是醫療建議。
根據使用者對膚況的描述：
1. concern_summary：用一句自然的繁體中文，摘要你理解的膚況與部位；要用自己的話歸納，不是照抄整句。
2. clarifying_question：只問一個最能幫助記錄的追問（例如左右側、範圍、是否疼痛）。
3. severity_levels：針對這個膚況寫出 1～5 級的具體程度描述，1 最輕、5 最嚴重，每級不超過 30 字，
   用使用者能自我判斷的可觀察描述。
不要使用診斷語氣，不要推薦藥物。"""

SEVERITY_INTERPRETATION_INSTRUCTIONS = """你是 BLINK AI 的膚況程度判讀助理，這不是診斷。
根據膚況摘要、部位、1～5 級描述與使用者的自然語言補充，判斷最接近的 severity（1～5）。
- 資訊足以判斷：needs_clarification=false，填 severity 與 understood_description（一句摘要）。
- 資訊不足：needs_clarification=true，severity=null，clarifying_question 只問一個具體問題。"""


# ── helpers ────────────────────────────────────────────────────────
def _promote_tag_only_record(record: ExtractedSkinRecord) -> ExtractedSkinRecord:
    """A catalog observation without a core issue still satisfies the issue slot as 'other'."""
    if record.issue is not None or not record.tag_slugs:
        return record
    note = (record.note or "").strip() or "、".join(tag_label(slug) for slug in record.tag_slugs)
    return record.model_copy(update={"issue": Issue.other, "note": note})


def _resolve_api_key(api_key: str | None, env_file: str | Path | None) -> str:
    if api_key and api_key.strip():
        return api_key.strip()
    process_value = os.environ.get("OPENAI_API_KEY", "").strip()
    if process_value:
        return process_value
    if env_file is not None and Path(env_file).exists():
        try:
            from dotenv import dotenv_values
        except ImportError as exc:
            raise SkinExtractionConfigurationError(
                "Reading OPENAI_API_KEY from an env file requires python-dotenv."
            ) from exc
        file_value = (dotenv_values(env_file).get("OPENAI_API_KEY") or "").strip()
        if file_value:
            return file_value
    if os.environ.get("SKIN_EXTRACTION_BASE_URL", "").strip():
        return "EMPTY"  # local vLLM does not require a key
    raise SkinExtractionConfigurationError("OPENAI_API_KEY is required for Skin Tracking extraction.")


class OpenAISkinExtractor:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        env_file: str | Path | None = DEFAULT_ENV_FILE,
        client: Any | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = (
            model
            or os.environ.get("OPENAI_SKIN_TRACKING_MODEL", "").strip()
            or DEFAULT_EXTRACTION_MODEL
        )
        if client is not None:
            self._client = client
            return
        key = _resolve_api_key(api_key, env_file)
        import openai

        kwargs: dict[str, Any] = {
            "api_key": key,
            "timeout": timeout_seconds or float(os.environ.get("SKIN_EXTRACTION_TIMEOUT_SECONDS", "20")),
            "max_retries": int(os.environ.get("SKIN_EXTRACTION_MAX_RETRIES", "1")),
        }
        resolved_base_url = base_url or os.environ.get("SKIN_EXTRACTION_BASE_URL", "").strip()
        if resolved_base_url:
            kwargs["base_url"] = resolved_base_url
        self._client = openai.OpenAI(**kwargs)

    def _parse(self, *, instructions: str, user_input: str, text_format: type[BaseModel]) -> BaseModel:
        try:
            response = self._client.responses.parse(
                model=self.model,
                instructions=instructions,
                input=user_input,
                text_format=text_format,
                store=False,
            )
        except Exception as error:
            raise SkinExtractionProviderError(
                f"{self.model} request failed: {type(error).__name__}"
            ) from error
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise SkinExtractionProviderError(f"{self.model} returned no structured output")
        if isinstance(parsed, text_format):
            return parsed
        try:
            payload = parsed.model_dump() if isinstance(parsed, BaseModel) else parsed
            return text_format.model_validate(payload)
        except ValidationError as error:
            raise SkinExtractionProviderError(
                f"{self.model} returned invalid structured output"
            ) from error

    def extract(self, message: str, *, today: Date) -> SkinExtractionResult:
        result = self._parse(
            instructions=EXTRACTION_INSTRUCTIONS.format(today=today.isoformat()),
            user_input=message,
            text_format=SkinExtractionResult,
        )
        records = []
        for record in result.records:
            tags, zones = extract_observation_metadata(
                message,
                suggested_tag_slugs=record.tag_slugs,
                suggested_detail_zones=record.detail_zones,
            )
            records.append(
                _promote_tag_only_record(record.model_copy(update={"tag_slugs": tags, "detail_zones": zones}))
            )
        return SkinExtractionResult(records=records)

    def assess_concern(self, *, issue_label: str, user_description: str, zone_label: str | None = None) -> SkinConcernAssessment:
        user_input = (
            f"膚況類別：{issue_label}\n"
            f"部位：{zone_label or '尚未確認'}\n"
            f"使用者描述：{user_description}"
        )
        return self._parse(
            instructions=CONCERN_ASSESSMENT_INSTRUCTIONS,
            user_input=user_input,
            text_format=SkinConcernAssessment,
        )

    def interpret_severity(
        self,
        *,
        concern_summary: str,
        zone_label: str | None,
        user_description: str,
        severity_levels: list[dict],
    ) -> SeverityInterpretation:
        levels = "\n".join(f"{item['level']}：{item['description']}" for item in severity_levels)
        user_input = (
            f"膚況摘要：{concern_summary}\n部位：{zone_label or '未指定'}\n"
            f"程度描述：\n{levels}\n使用者補充：{user_description}"
        )
        return self._parse(
            instructions=SEVERITY_INTERPRETATION_INSTRUCTIONS,
            user_input=user_input,
            text_format=SeverityInterpretation,
        )

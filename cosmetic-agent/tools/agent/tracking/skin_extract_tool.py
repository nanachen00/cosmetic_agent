"""Merge structured, message-local extraction into Skin Tracking drafts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from agents.skin_tracking.extraction import (
    ExtractedSkinRecord,
    SkinExtractionProvider,
    StabilitySignal,
)
from schemas.tracking_schema import RecordDraft, RecordStatus, StabilitySource


AMBIGUOUS_EYE_TERMS = ("眼下", "眼睛下面", "眼瞼下面", "眼皮下面", "眼周")
EXPLICIT_EYE_SIDES = ("左眼", "右眼", "左右眼", "兩眼", "雙眼", "兩邊")


def _without_guessed_eye_side(
    records: list[ExtractedSkinRecord],
    message: str,
) -> list[ExtractedSkinRecord]:
    """Never let the model invent left/right for a generically described eye area."""
    text = (message or "").strip()
    has_ambiguous_eye_area = any(term in text for term in AMBIGUOUS_EYE_TERMS)
    has_explicit_side = any(term in text for term in EXPLICIT_EYE_SIDES)
    if not has_ambiguous_eye_area or has_explicit_side:
        return records
    return [record.model_copy(update={"zone": None}) for record in records]


@dataclass(frozen=True)
class DraftExtraction:
    """Merged drafts plus transient stability questions for the state machine."""

    drafts: list[dict]
    ambiguous_stability_indexes: list[int]


def _target_indexes(current_drafts: list[dict], record_count: int) -> list[int]:
    if record_count == 1 and len(current_drafts) > 1:
        target = next(
            (
                index
                for index, draft in enumerate(current_drafts)
                if RecordDraft(**draft).missing_slots()
            ),
            0,
        )
        return [target]
    return list(range(record_count))


def _updates(record: ExtractedSkinRecord) -> dict:
    """Translate model evidence into validated domain fields."""
    updates = record.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"stability_signal"},
    )
    updates = {key: value for key, value in updates.items() if value not in ("", [])}

    if record.stability_signal == StabilitySignal.stable_explicit:
        updates["record_status"] = RecordStatus.stable.value
        updates["stability_source"] = StabilitySource.user_reported.value
    elif record.stability_signal == StabilitySignal.not_stable:
        updates["record_status"] = RecordStatus.recorded.value
    return updates


def merge_extraction(
    current_drafts: list[dict],
    extracted_records: list[ExtractedSkinRecord],
) -> DraftExtraction:
    """Apply extracted fields while retaining queued drafts and transient metadata."""
    target_indexes = _target_indexes(current_drafts, len(extracted_records))
    merged_drafts = list(current_drafts)
    ambiguous_indexes = []

    for record, target_index in zip(extracted_records, target_indexes):
        while len(merged_drafts) <= target_index:
            merged_drafts.append({})
        merged = {**merged_drafts[target_index], **_updates(record)}
        merged_drafts[target_index] = RecordDraft(**merged).model_dump(mode="json")
        if record.stability_signal == StabilitySignal.stable_ambiguous:
            ambiguous_indexes.append(target_index)

    return DraftExtraction(
        drafts=merged_drafts,
        ambiguous_stability_indexes=ambiguous_indexes,
    )


def merge_extracted_drafts(
    current_drafts: list[dict],
    extracted_records: list[ExtractedSkinRecord],
) -> list[dict]:
    """Compatibility helper for callers that only need merged drafts."""
    return merge_extraction(current_drafts, extracted_records).drafts


def extract_skin_drafts_with_metadata(
    extractor: SkinExtractionProvider,
    current_drafts: list[dict],
    message: str,
    *,
    today: Date | None = None,
) -> DraftExtraction:
    """Extract one message and preserve transient confirmation metadata."""
    result = extractor.extract(message, today=today or Date.today())
    records = _without_guessed_eye_side(result.records, message)
    return merge_extraction(current_drafts, records)


def extract_skin_drafts_from_message(
    extractor: SkinExtractionProvider,
    current_drafts: list[dict],
    message: str,
    *,
    today: Date | None = None,
) -> list[dict]:
    """Compatibility wrapper returning drafts without transient metadata."""
    return extract_skin_drafts_with_metadata(
        extractor,
        current_drafts,
        message,
        today=today,
    ).drafts

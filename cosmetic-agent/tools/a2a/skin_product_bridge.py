"""PostgreSQL skin-history bridge used by product ranking.

The bridge is fail-open: missing data or a database error must never block the
main recommendation response.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from database.postgres.repositories.skin_record import postgres_skin_record_store


# Values must be product_agent CONCERN_SYNONYM keys (the 2026-09-19 mapping).
# Keys cover schemas.tracking_schema.Issue values and their ISSUE_LABELS.
_CONDITION_TO_CONCERN = {
    "dryness": "保濕",
    "乾燥": "保濕",
    "acne": "抗痘",
    "痘痘": "抗痘",
    "redness": "舒敏",
    "泛紅": "舒敏",
    "oiliness": "控油",
    "出油": "控油",
    "sensitivity": "舒敏",
    "敏感": "舒敏",
    "itching": "舒敏",
    "搔癢": "舒敏",
    "wrinkle": "抗老",
    "細紋": "抗老",
    "dark_spot": "美白",
    "pigmentation": "美白",
    "斑點": "美白",
    "peeling": "修護",
    "脫皮": "修護",
}

_HIGH_SEVERITY = {"high", "severe", "嚴重", "重度", "3", "4", "5"}


def _condition(record: dict[str, Any]) -> str:
    return str(record.get("issue") or record.get("condition") or "").strip()


def get_skin_context(
    user_id: str,
    purpose: str = "product_recommendation",
) -> dict[str, Any] | None:
    """Return the existing bridge contract, backed only by PostgreSQL."""
    if not user_id:
        return None

    try:
        records = postgres_skin_record_store.query(user_id=user_id)
        if not records:
            return None

        recent_records = records[:90]
        conditions = [_condition(record) for record in recent_records]
        conditions = [condition for condition in conditions if condition]
        frequencies = Counter(conditions)

        concern_hints: list[str] = []
        for condition in conditions:
            concern = _CONDITION_TO_CONCERN.get(condition)
            if concern and concern not in concern_hints:
                concern_hints.append(concern)

        risk_flags: list[str] = []
        for condition, count in frequencies.items():
            if count >= 3:
                risk_flags.append(f"{condition}_frequently_reported")
        for record in recent_records:
            severity = str(record.get("severity") or "").strip().lower()
            condition = _condition(record)
            if condition and severity in _HIGH_SEVERITY:
                flag = f"{condition}_high_severity"
                if flag not in risk_flags:
                    risk_flags.append(flag)

        return {
            "raw": {
                "source": "postgres",
                "purpose": purpose,
                "records": recent_records[:20],
            },
            "concern_hints": concern_hints,
            "risk_flags": risk_flags,
            "trend_note": ", ".join(risk_flags[:2]) if risk_flags else "",
        }
    except Exception as exc:
        print(f"ℹ️ [SkinProductBridge] PostgreSQL context unavailable ({exc})")
        return None

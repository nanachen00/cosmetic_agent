"""Deterministic extraction for explicit skin concerns in user wording.

The LLM extractor remains responsible for open-ended language. These aliases are
only an authoritative fast path when the user explicitly names one of the
recordable concerns, so a transient model miss cannot reset the conversation.
"""

from __future__ import annotations

import re

from schemas.tracking_schema import Issue


_ISSUE_TERMS: tuple[tuple[Issue, tuple[str, ...]], ...] = (
    (Issue.acne_scar, ("痘疤", "凹疤", "凸疤", "痘坑")),
    (Issue.acne, ("痘痘", "粉刺", "面皰", "膿包", "丘疹", "長痘", "冒痘", "爆痘")),
    (Issue.dryness, ("乾燥", "很乾", "乾裂", "脫皮", "起皮", "脫屑", "緊繃", "粗糙", "缺水", "乾癢")),
    (Issue.redness, ("泛紅", "發紅", "紅腫", "紅疹", "潮紅", "臉紅", "有點紅", "變紅", "發紅")),
    (Issue.oiliness, ("出油", "油膩", "油光", "很油", "泛油")),
    (Issue.itching, ("搔癢", "發癢", "很癢", "刺癢", "會癢", "乾癢")),
    (Issue.pigmentation, ("斑點", "色素沉澱", "色素沈澱", "曬斑", "雀斑", "肝斑", "痘印")),
    (Issue.other, ("其他狀況", "其他問題")),
)

_NEGATION_SUFFIX = re.compile(r"(?:沒有|沒|無|不是|並非)\s*$")


def infer_explicit_issues(text: str) -> list[Issue]:
    """Return recordable concerns explicitly and non-negatively named in text."""

    normalized = re.sub(r"\s+", "", text or "")
    found: list[Issue] = []
    for issue, terms in _ISSUE_TERMS:
        matched = False
        for term in terms:
            for occurrence in re.finditer(re.escape(term), normalized):
                prefix = normalized[max(0, occurrence.start() - 4) : occurrence.start()]
                if not _NEGATION_SUFFIX.search(prefix):
                    matched = True
                    break
            if matched:
                break
        if matched:
            found.append(issue)
    return found


__all__ = ["infer_explicit_issues"]

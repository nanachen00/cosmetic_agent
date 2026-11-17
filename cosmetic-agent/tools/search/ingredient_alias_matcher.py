"""Conservative entity-name fast path using the existing curated vocabulary.

This does not answer questions or treat local graph relationships as evidence.
Unrecognised wording, multiple entities and chemical suffixes defer to Terra.
"""
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata


VOCABULARY = Path(__file__).resolve().parents[2] / "data/knowledge_graph/v1/knowledge_map.json"
_PREFIXES = frozenset(("", "請問", "我想問", "想知道", "我想知道", "什麼是", "介紹一下",
    "請問這款乳液有包含", "這款乳液有包含", "what is", "what's", "tell me about", "what does"))
_SUFFIXES = frozenset(("", "嗎", "是什麼", "的意思", "有什麼功用", "有什麼作用", "有什麼功效",
    "的功用是什麼", "的作用是什麼", "的功效", "功效", "怎麼用", "如何使用", "do"))


def _text(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class IngredientAliasMatcher:
    def __init__(self, nodes):
        self._patterns = []
        for node in nodes:
            if not isinstance(node, dict) or node.get("type") != "Ingredient":
                continue
            name = node.get("name_zh") or node.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            canonical = re.split(r"[（(]", name, maxsplit=1)[0].strip()
            aliases = node.get("aliases") or []
            if not isinstance(aliases, list):
                continue
            for value in {name, canonical, *[v for v in aliases if isinstance(v, str)]}:
                alias = _text(value)
                if 2 <= len(alias) <= 100:
                    # ASCII word boundaries prevent retinol matching retinolate,
                    # or BHA matching a longer INCI/trademark identifier.
                    pattern = re.compile(r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])")
                    self._patterns.append((canonical, pattern))

    def match(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            return None
        query = _text(text).strip(" ?？!！。.,，")
        matches = [(m.start(), m.end(), canonical) for canonical, pattern in self._patterns
                   for m in pattern.finditer(query)]
        if not matches or len({item[2] for item in matches}) != 1:
            return None
        start, end, canonical = max(matches, key=lambda item: item[1] - item[0])
        # Only overlapping synonyms within one complete mention are local.
        # A second unknown ingredient leaves text outside this span and cannot
        # be swallowed by a permissive regex or silently ignored.
        if any(left < start or right > end for left, right, _ in matches):
            return None
        if query[:start].strip() not in _PREFIXES or query[end:].strip() not in _SUFFIXES:
            return None
        return canonical


@lru_cache(maxsize=1)
def _matcher():
    try:
        if VOCABULARY.stat().st_size > 2 * 1024 * 1024:
            return IngredientAliasMatcher([])
        value = json.loads(VOCABULARY.read_text(encoding="utf-8"))
        nodes = value.get("nodes") if isinstance(value, dict) else None
        return IngredientAliasMatcher(nodes if isinstance(nodes, list) else [])
    except (OSError, ValueError, TypeError):
        return IngredientAliasMatcher([])


def match_single_ingredient(text):
    return _matcher().match(text)

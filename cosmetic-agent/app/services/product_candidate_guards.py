"""Pure eligibility checks shared by catalogue and web recommendation candidates.

These gates confirm supplied catalogue evidence, not clinical allergy safety.
Missing formula evidence cannot be replaced by highlighted ingredients or rank.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit


def positive_amount(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return amount if math.isfinite(amount) and amount > 0 else None


def _terms(value) -> list[str]:
    values = [value] if isinstance(value, str) else value or []
    return [str(item).strip() for item in values if str(item or "").strip()]


def _normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def brand_matches(brand: str, wanted: str) -> bool:
    actual, requested = _normalized(brand), _normalized(wanted)
    descriptive = len(requested) >= 3 or len(requested) >= 2 and bool(re.search(r"[\u3400-\u9fff]", requested))
    return bool(requested and (actual == requested or descriptive and requested in actual))


def full_inci_for_screening(product: dict) -> list[str] | None:
    if product.get("ingredient_evidence_level") != "full_inci" or product.get("inci_status") != "full":
        return None
    if product.get("recommendation_scope") not in (None, "", "full_formula"):
        return None
    ingredients = product.get("ingredients_inci")
    if not isinstance(ingredients, list) or not ingredients or any(not isinstance(item, str) or not item.strip() for item in ingredients):
        return None
    source = product.get("inci_source_url") or product.get("ingredient_source_url") or ""
    market = product.get("formula_market")
    if not isinstance(source, str) or not isinstance(market, str):
        return None
    try:
        parsed = urlsplit(source)
        parsed.port
        source_valid = not any(ord(x) <= 32 or ord(x) == 127 for x in source) and parsed.scheme in {"https", "http"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        source_valid = False
    if not source_valid or market.strip().casefold() in {"", "unknown", "unverified", "not_verified", "missing", "待核對", "未核對", "不明"}:
        return None
    # A verified single-ingredient formula may be complete; length is not proof.
    return list(ingredients)


def apply_product_hard_guards(
    products: Iterable[dict], *, excluded_product_ids=(), avoided_brands=(),
    required_brands=(), budget_max=None, disliked_ingredients=(), graph_rows=(),
    stable_key: Callable[[dict], str], ingredient_filter: Callable,
) -> list[dict]:
    excluded = set(_terms(excluded_product_ids))
    avoided, required = _terms(avoided_brands), _terms(required_brands)
    allergens = _terms(disliked_ingredients)
    limit = positive_amount(budget_max)
    eligible = []
    for original in products:
        if not isinstance(original, dict) or stable_key(original) in excluded:
            continue
        brands = _terms([original.get("canonical_brand"), original.get("brand")])
        if any(brand_matches(brand, term) for brand in brands for term in avoided):
            continue
        if required and not any(brand_matches(brand, term) for brand in brands for term in required):
            continue
        if limit is not None:
            price = positive_amount(original.get("price"))
            if price is None or price > limit or original.get("price_is_estimate") is True or str(original.get("currency") or "").upper() not in {"TWD", "NTD", "NT$"}:
                continue
        product = dict(original)
        if allergens:
            full_inci = full_inci_for_screening(product)
            if full_inci is None:
                continue
            product["ingredients_list"] = list(dict.fromkeys(full_inci + list(product.get("ingredients_list") or [])))
            # The existing synonym screen omits plain INCI Alcohol to avoid
            # confusing fatty alcohol names; match that exact ingredient here.
            if any(_normalized(term) in {"酒精", "alcohol", "ethanol", "alcoholdenat", "denaturedalcohol", "ethylalcohol"} for term in allergens):
                if any(_normalized(term) == "alcohol" for term in full_inci):
                    continue
        eligible.append(product)
    if allergens:
        constraints = []
        for term in allergens:
            normalized = _normalized(term)
            if normalized in {"香料", "香精", "fragrance", "parfum", "aroma"}:
                constraints.append("香料")
            elif normalized in {"酒精", "alcohol", "ethanol", "alcoholdenat", "denaturedalcohol", "ethylalcohol"}:
                constraints.append("酒精")
            else:
                constraints.append(term)
        eligible = ingredient_filter(eligible, constraints, list(graph_rows))
    return eligible


def recommendation_metadata(product: dict) -> dict:
    """Pass through recorded evidence without manufacturing missing metadata."""
    fields = (
        "ingredient_evidence_level", "inci_status", "recommendation_scope",
        "recommendation_limitations", "limitations", "price_observed_at",
        "price_source_url", "source_url", "currency", "formula_market",
        "inci_source_url", "ingredient_source_url", "reference_url", "price_is_estimate",
        "texture", "texture_source_url", "texture_verification_status",
        "product_version", "capacity", "source_title", "evidence_status",
        "evidence_updated_at",
    )
    return {key: product[key] for key in fields if key in product}

"""Explainable initial ranking policy, not a learned probability of suitability.

Hard exclusions belong to product_candidate_guards. Literature motivates the
features, not these coefficients; calibrate them on consented platform outcomes.
No brand fame, sales, KOL or novelty is inferred from a product name.
"""
from __future__ import annotations

import math
import re
import unicodedata
from urllib.parse import urlsplit
from app.services.product_candidate_guards import full_inci_for_screening

POLICY_VERSION = "personal-v3"
DEFAULT_WEIGHTS = {
    "need_fit": .35,
    "ingredient_evidence": .20,
    "texture_fit": .15,
    "budget_fit": .15,
    "brand_preference": .10,
    "review_evidence": .05,
}
LEGACY_KEYS = {"safety_score": "ingredient_evidence", "price_value_score": "budget_fit",
    "review_score": "review_evidence", "ingredient_match_score": "need_fit", "brand_score": "brand_preference"}
VERIFIED_ATTRIBUTE_STATES = {"verified", "official_product_label", "official_product_page",
    "authorized_retailer_product_page"}


def number(value, default=0.):
    if isinstance(value, bool): return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError): return default


def terms(value):
    values = value if isinstance(value, (list, tuple, set)) else [value] if value else []
    return list(dict.fromkeys(str(x).strip() for x in values if isinstance(x, str) and x.strip()))[:30]


def normalized(value):
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def source_url(value):
    try:
        text = str(value or "")
        if any(ord(x) <= 32 or ord(x) == 127 for x in text): return False
        p = urlsplit(text)
        p.port
        return p.scheme in {"https", "http"} and bool(p.hostname) and p.username is None and p.password is None
    except ValueError: return False


def verified_attribute(product, field):
    return (str(product.get(f"{field}_verification_status") or "").strip() in VERIFIED_ATTRIBUTE_STATES
        and source_url(product.get(f"{field}_source_url")))


def same_brand(brand, preference):
    a, b = normalized(brand), normalized(preference)
    return bool(a and b and (a == b or (len(b) >= 3 and b in a) or (len(b) >= 2 and re.search(r"[\u3400-\u9fff]", b) and b in a)))


def texture_labels(value):
    text = str(value or "").casefold()
    text = re.sub(r"不(?:太|夠|够)?(?:清爽|滋潤|滋润)|\bnot\s+(?:lightweight|rich)\b", "", text)
    labels = set()
    if any(x in text for x in ("清爽", "水感", "水凝", "輕盈", "轻盈", "不黏", "不油膩")) or re.search(r"\b(?:lightweight|non[- ]greasy|gel)\b", text):
        labels.add("light")
    # "enriched" or "rich in vitamin C" describes ingredients, not texture.
    if any(x in text for x in ("滋潤", "滋润", "濃潤", "丰润", "豐潤")) or re.search(r"\bbalm\b|\brich\s+(?:cream|lotion|texture|formula|moisturi[sz]er)\b", text) or text.strip() == "rich":
        labels.add("rich")
    return labels


SKIN_TYPES = {"乾性": "dry", "乾性肌": "dry", "油性": "oily", "油性肌": "oily",
    "混合性": "combination", "混合肌": "combination", "敏感": "sensitive", "敏感肌": "sensitive",
    "中性": "normal", "一般": "normal"}


def normalize_context(value=None):
    context = value if isinstance(value, dict) else {}
    skin = ""
    for value in (context.get("current_skin_type"), context.get("skin_type"), context.get("skinType")):
        candidate = str(value or "").strip()
        candidate = SKIN_TYPES.get(candidate, candidate.casefold())
        if candidate in {"dry", "oily", "combination", "sensitive", "normal"}:
            skin = candidate
            break
    wanted = terms(context.get("current_concerns") or context.get("preferred_effects"))
    texture = str(context.get("current_texture") or "")
    textures = texture_labels(texture) if texture not in {"", "unknown", "不明"} else set()
    if not textures:
        for item in terms(context.get("preferred_textures")): textures.update(texture_labels(item))
    budget = number(context.get("current_budget_max") if "current_budget_max" in context else context.get("budget_max"))
    budget = budget if budget > 0 else None
    brands = terms(context.get("preferred_brands"))
    weights = dict(DEFAULT_WEIGHTS)
    supplied = context.get("ranking_weights")
    # Legacy profiles contain model-inferred, five-feature weights. They omit
    # texture entirely and must not silently override this reviewed policy.
    # A deliberately versioned policy configuration may supply overrides.
    if isinstance(supplied, dict) and context.get("ranking_policy_version") == POLICY_VERSION:
        custom = {key: 0. for key in weights}
        for key, val in supplied.items():
            target = LEGACY_KEYS.get(key, key)
            if target in custom: custom[target] += max(0., min(100., number(val)))
        if sum(custom.values()) > 0:
            total = sum(custom.values())
            weights = {key: val / total for key, val in custom.items()}
    return {"enabled": bool(wanted or skin or textures or budget or brands),
        "skin_type": skin, "concerns": wanted, "textures": textures, "budget_max": budget,
        "weights": weights, "preferred_brands": brands, "avoided_brands": terms(context.get("avoided_brands")),
        "customer_type": str(context.get("customer_type") or "known_preferences"),
        "customer_type_label": str(context.get("customer_type_label") or "已知偏好")}


def ingredient_evidence(product):
    values = terms(product.get("ingredients_inci"))
    level = product.get("ingredient_evidence_level")
    if values and full_inci_for_screening(product) and source_url(product.get("inci_source_url") or product.get("ingredient_source_url")):
        return 1.
    if level in {"official_featured", "official_featured_ingredients", "featured_ingredients", "official_highlights"} and source_url(product.get("ingredient_source_url")):
        return .65
    if source_url(product.get("ingredient_source_url") or product.get("inci_source_url")) and terms(product.get("ingredients_list") or product.get("key_ingredients")):
        return .4
    return 0.


def review_evidence(product):
    if product.get("is_sponsored_review") is True or product.get("review_sponsored") is True: return 0.
    n = number(product.get("review_count"))
    if n < 1 or not n.is_integer() or not source_url(product.get("rating_source_url") or product.get("review_source_url")): return 0.
    raw = product.get("rating")
    if raw is not None:
        rating = number(raw, -1.) / 5.
    else:
        rating = number(product.get("review_score"), -1.)
        if rating > 1.: rating /= 5.
    if not 0 <= rating <= 1: return 0.
    # Fixed initial prior: 20 reviews at 3.5/5, not fitted effect sizes.
    return ((n * rating + 20 * .7) / (n + 20)) * (n / (n + 20))


def apply_scores(candidates, context, warned_ids=(), explicit_brands=None):
    prefs = normalize_context(context)
    if not candidates or not prefs["enabled"]: return False
    if explicit_brands: prefs["preferred_brands"] = terms(explicit_brands)
    active = dict(prefs["weights"])
    if not (prefs["concerns"] or prefs["skin_type"]): active["need_fit"] = 0.
    if not prefs["textures"]: active["texture_fit"] = 0.
    if not prefs["budget_max"]: active["budget_fit"] = 0.
    if not prefs["preferred_brands"]: active["brand_preference"] = 0.
    total = sum(active.values())
    if total <= 0: return False
    active = {key: value / total for key, value in active.items()}
    for product in candidates:
        reasons, missing = [], []
        concern = min(1., max(0., number(product.get("concern_hits"))) / (6 * max(1, len(prefs["concerns"])))) if prefs["concerns"] else .5
        if prefs["concerns"] and concern > 0:
            reasons.append("商品成分或用途資料對應目前的" + "、".join(prefs["concerns"][:2]) + "需求")
        skin_fit = .5
        skin_reason = None
        if prefs["skin_type"]:
            skin_fit = 0.
            types = ({SKIN_TYPES.get(x, x.casefold()) for x in terms(product.get("skin_types"))}
                if verified_attribute(product, "skin_types") else set())
            if types:
                skin_fit = 1. if prefs["skin_type"] in types or types & {"all", "all_skin_types", "所有膚質", "全膚質"} else 0.
                if skin_fit:
                    label = {"dry": "乾性", "oily": "油性", "sensitive": "敏感", "combination": "混合性", "normal": "中性"}[prefs["skin_type"]]
                    skin_reason = "商品適用膚質標示符合你設定的" + label + "膚質"
            else: missing.append("缺少可核對的適用膚質資料")
        need_fit = (.75 * concern + .25 * skin_fit) if prefs["skin_type"] and prefs["concerns"] else skin_fit if prefs["skin_type"] else concern
        observed = (texture_labels(product.get("texture"))
            if verified_attribute(product, "texture") else set())
        texture_fit = 0.
        if prefs["textures"]:
            if observed:
                texture_fit = 1. if observed & prefs["textures"] else 0.
                if texture_fit:
                    label = "清爽" if "light" in prefs["textures"] & observed else "滋潤"
                    reasons.append("符合你偏好的" + label + "質地（依商品標示）")
            else: missing.append("缺少可核對的質地標示")
        price = number(product.get("price"), -1.)
        price_confirmable = product.get("price_is_estimate") is not True
        budget_fit = max(0., min(1., 1. - price / prefs["budget_max"])) if prefs["budget_max"] and price > 0 and price_confirmable else 0.
        if prefs["budget_max"] and 0 < price <= prefs["budget_max"] and price_confirmable:
            reasons.append(f"記錄價格 NT${price:g}，在你的 NT${prefs['budget_max']:g} 單品預算內")
        if not product.get("price_observed_at"): missing.append("價格更新時間待確認")
        if not price_confirmable: missing.append("價格含外幣估算，實際台幣售價待核對")
        brand_fit = float(any(same_brand(product.get("brand"), x) for x in prefs["preferred_brands"]))
        if brand_fit: reasons.append("符合你明確偏好的品牌：" + str(product.get("brand") or ""))
        if skin_reason: reasons.append(skin_reason)
        # Put concrete discriminating preferences ahead of generic relevance.
        reasons.sort(key=lambda x: 0 if "質地" in x else 1 if "品牌" in x else 2 if "預算" in x else 3 if "膚質" in x else 4)
        evidence = ingredient_evidence(product)
        if evidence < 1.: missing.append("目前只有部分成分資料；完整配方與來源待核對")
        review = review_evidence(product)
        if not review: missing.append("缺少可核對的評論樣本數或來源，不推定熱銷或高口碑")
        values = {"need_fit": need_fit, "ingredient_evidence": evidence, "texture_fit": texture_fit,
            "budget_fit": budget_fit, "brand_preference": brand_fit, "review_evidence": review}
        score = sum(active[k] * values[k] for k in active)
        missing.sort(key=lambda x: 0 if "完整配方" in x else 1 if "價格" in x else 2 if "質地" in x else 3 if "膚質" in x else 4)
        product.update(personalization_score=round(score, 6), personalization_policy_version=POLICY_VERSION,
            personalization_components=values, personalization_weights=dict(active),
            personalization_reasons=list(dict.fromkeys(reasons)), personalization_missing=list(dict.fromkeys(missing)),
            personalization_customer_type=prefs["customer_type"])
    return True

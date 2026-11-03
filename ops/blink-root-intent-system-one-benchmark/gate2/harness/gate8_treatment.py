from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

MAPPING = {"product": "find_product", "knowledge": "knowledge_query", "clarify": "unclear"}
NONMAPPABLE = {"safety", "skin_tracking", "conversation"}
ROUTES = set(MAPPING) | NONMAPPABLE
QUERY_MARKER = "【本輪待判斷輸入】\n"


class TreatmentError(RuntimeError):
    pass


def sha256_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_parser(path: Path):
    path = path.resolve(strict=True)
    name = "gate8_qualified_parser_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise TreatmentError("PARSER_IMPORT_SPEC_FAILED")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    for attr in ("parse", "deterministic_route_hint"):
        if not callable(getattr(module, attr, None)):
            raise TreatmentError("PARSER_INTERFACE_MISSING:" + attr)
    return module


def load_policy(path: Path) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    policy = obj.get("selected", obj)
    if set(policy.get("thresholds", {})) != set(MAPPING):
        raise TreatmentError("POLICY_ROUTE_KEYS_CHANGED")
    for key, value in policy["thresholds"].items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1.1:
            raise TreatmentError("INVALID_THRESHOLD:" + key)
    for key in ("clarification_guard", "safety_guard", "clarify_noul_min"):
        value = policy.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1.1:
            raise TreatmentError("INVALID_POLICY_GUARD:" + key)
    return policy


def textual(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TreatmentError("INTENT_INPUT_NOT_TEXT")
    parts = []
    for item in content:
        if not isinstance(item, dict):
            raise TreatmentError("INTENT_INPUT_NOT_TEXT")
        text = item.get("text")
        if item.get("type") not in {"input_text", "text", "output_text"} or not isinstance(text, str):
            raise TreatmentError("INTENT_INPUT_NOT_TEXT")
        parts.append(text)
    return "\n".join(parts)


def inspect_intent_request(payload: dict, path: str) -> dict | None:
    """Identify only the current BLINK structured root-intent request.

    No inference from generic JSON-schema traffic is allowed. Unknown layouts
    are passed through untouched.
    """
    if path.rstrip("/") != "/v1/responses" or not isinstance(payload, dict):
        return None
    text = payload.get("text") or {}
    fmt = text.get("format") if isinstance(text, dict) else None
    if not isinstance(fmt, dict) or fmt.get("type") != "json_schema":
        return None
    inner = fmt.get("json_schema", fmt)
    if inner.get("name") != "intent_parse_result" or inner.get("strict") is not True:
        return None
    schema = inner.get("schema")
    if not isinstance(schema, dict):
        raise TreatmentError("INTENT_SCHEMA_MISSING")
    props = schema.get("properties")
    required = schema.get("required")
    if schema.get("type") != "object" or not isinstance(props, dict) or not isinstance(required, list):
        raise TreatmentError("INTENT_SCHEMA_INCOMPATIBLE")
    if "raw_query" not in props or "raw_query" not in required or "intent_type" not in props or "intent_type" not in required:
        raise TreatmentError("INTENT_SCHEMA_BINDING_FIELDS_MISSING")

    incoming = payload.get("input")
    if not isinstance(incoming, list) or not incoming:
        raise TreatmentError("INTENT_INPUT_LAYOUT_INCOMPATIBLE")
    last = incoming[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        raise TreatmentError("INTENT_FINAL_USER_MESSAGE_MISSING")
    content = textual(last.get("content"))
    if content.count(QUERY_MARKER) != 1:
        raise TreatmentError("INTENT_QUERY_MARKER_AMBIGUOUS")
    suffix = content.split(QUERY_MARKER, 1)[1]
    if len(suffix) < 3 or not suffix.startswith("「") or not suffix.endswith("」"):
        raise TreatmentError("INTENT_QUERY_ENVELOPE_INCOMPATIBLE")
    query = suffix[1:-1]
    if not query.strip() or len(query.encode("utf-8")) > 65536:
        raise TreatmentError("INTENT_QUERY_LENGTH_INVALID")

    enum = (props.get("intent_type") or {}).get("enum") or []
    if not all(value in enum for value in MAPPING.values()):
        raise TreatmentError("INTENT_SCHEMA_MAPPABILITY_CHANGED")
    return {
        "query": query,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "query_chars": len(query),
        "schema": schema,
        "schema_sha256": sha256_json(schema),
        "requested_model": payload.get("model"),
    }


def parse_kev(body: dict) -> dict:
    try:
        answers = body["answers"]
        route = answers["route"]
        probs = route["probabilities"]
        choice = route["choice"]
        confidence = float(route["confidence"])
        clarification = float(answers["needs_clarification"]["noul"])
        safety = float(answers["safety_sensitive"]["noul"])
    except Exception as exc:
        raise TreatmentError("KEV_CONTRACT_MISSING_FIELDS") from exc
    if choice not in ROUTES or set(probs) != ROUTES:
        raise TreatmentError("KEV_ROUTE_CONTRACT_CHANGED")
    values = [float(v) for v in probs.values()]
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in values + [confidence, clarification, safety]):
        raise TreatmentError("KEV_PROBABILITY_INVALID")
    if abs(sum(values) - 1.0) > 0.03:
        raise TreatmentError("KEV_PROBABILITY_SUM_INVALID")
    return {
        "pred_route": choice,
        "confidence": confidence,
        "clarification_probability": clarification,
        "safety_probability": safety,
        "probabilities": {k: float(v) for k, v in probs.items()},
    }


def selective(prediction: dict, policy: dict) -> tuple[str | None, str]:
    route = prediction["pred_route"]
    if route not in MAPPING:
        return None, "existing_path_fallback"
    if prediction["clarification_probability"] >= policy["clarification_guard"] and route != "clarify":
        return None, "confidence_fallback"
    if prediction["safety_probability"] >= policy["safety_guard"]:
        return None, "safety_fallback"
    if route == "clarify" and prediction["clarification_probability"] < policy["clarify_noul_min"]:
        return None, "clarification_signal_fallback"
    if prediction["confidence"] < policy["thresholds"][route]:
        return None, "confidence_fallback"
    return route, "kev"


def _schema_accepts(schema: dict, value: Any) -> bool:
    try:
        import jsonschema
        jsonschema.validate(value, schema)
        return True
    except Exception:
        return False


def _neutral_enum(values: list) -> Any:
    # Schema-driven neutral values only. The production BLINK enums currently
    # mix English and Traditional-Chinese wire values (for example some
    # product-category/texture enums use `不明`). Never invent a non-schema
    # value: choose only a value explicitly present in the incoming schema.
    for candidate in (
        "unknown", "UNKNOWN", "Unknown",
        "unspecified", "UNSPECIFIED", "Unspecified",
        "不明", "未知", "未指定",
        "none", "NONE", "None", "無", "",
    ):
        if candidate in values:
            return candidate
    if None in values:
        return None
    raise TreatmentError("NO_SAFE_NEUTRAL_ENUM_VALUE")


def _neutral(schema: dict) -> Any:
    if "default" in schema and _schema_accepts(schema, schema["default"]):
        return schema["default"]
    # Pydantic/JSON-schema may express Optional[T] through anyOf/oneOf rather
    # than a type list. Prefer an explicit null branch, then recurse into
    # branches and accept the first neutral value that validates against the
    # complete parent schema.
    for union_key in ("anyOf", "oneOf"):
        branches = schema.get(union_key)
        if isinstance(branches, list):
            if _schema_accepts(schema, None):
                return None
            for branch in branches:
                if not isinstance(branch, dict):
                    continue
                try:
                    value = _neutral(branch)
                except TreatmentError:
                    continue
                if _schema_accepts(schema, value):
                    return value
            raise TreatmentError("NO_SAFE_NEUTRAL_UNION_VALUE")
    enum = schema.get("enum")
    if isinstance(enum, list):
        return _neutral_enum(enum)
    types = schema.get("type")
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list):
        types = []
    if "null" in types:
        return None
    if "array" in types:
        return []
    if "boolean" in types:
        return False
    if "number" in types:
        minimum = schema.get("minimum", 0.0)
        maximum = schema.get("maximum", 1.0)
        value = min(max(0.0, float(minimum)), float(maximum))
        return value
    if "integer" in types:
        minimum = int(schema.get("minimum", 0))
        maximum = int(schema.get("maximum", max(0, minimum)))
        return min(max(0, minimum), maximum)
    if "string" in types and int(schema.get("minLength", 0)) == 0:
        return ""
    raise TreatmentError("NO_SAFE_NEUTRAL_VALUE")


def _value_or_neutral(prop_schema: dict, value: Any) -> Any:
    if _schema_accepts(prop_schema, value):
        return value
    return _neutral(prop_schema)


def _surface_concerns(query: str) -> list[str]:
    out = []
    for words, label in (
        (("保濕", "缺水", "乾燥"), "保濕"),
        (("敏感", "舒敏", "舒緩"), "舒緩"),
        (("痘痘", "痘肌", "痤瘡"), "痘痘"),
        (("粉刺", "不致粉刺"), "粉刺"),
        (("控油", "出油"), "控油"),
        (("美白", "亮白", "提亮", "淡斑", "暗沉"), "美白"),
        (("修護", "修復", "屏障", "脫皮"), "修護"),
        (("抗老", "抗皺", "細紋", "緊緻"), "抗老"),
    ):
        if any(w in query for w in words):
            out.append(label)
    return list(dict.fromkeys(out))


def _category(slots: dict, query: str) -> str | None:
    value = slots.get("product_category")
    if value != "moisturizer":
        return value
    lotion = "乳液" in query
    cream = any(w in query for w in ("乳霜", "面霜", "保濕霜"))
    if lotion and not cream:
        return "lotion"
    if cream and not lotion:
        return "cream"
    return None


def build_wire_result(route: str, parsed: Any, query: str, schema: dict) -> dict:
    """Build only the declared wire object and validate it against the request schema.

    Unknown future required fields fail closed and use the current intent model.
    """
    if route not in MAPPING:
        raise TreatmentError("UNMAPPABLE_LOCAL_ROUTE")
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    supported = {
        "intent_type", "skin_type", "concern", "product_category",
        "texture_preference", "budget_range", "brand_preference",
        "urgency_flag", "ambiguity_score", "routing_confidence",
        "task_completeness", "query_expansion", "raw_query",
    }
    unknown_required = sorted(required - supported)
    if unknown_required:
        raise TreatmentError("UNKNOWN_REQUIRED_INTENT_FIELDS:" + ",".join(unknown_required))

    slots = parsed.to_dict()
    concerns = _surface_concerns(query)
    expansion = list(concerns)
    category = _category(slots, query)
    if category:
        expansion.insert(0, category)
    ingredient_terms = {
        "hyaluronic_acid": "玻尿酸", "ceramide": "神經醯胺",
        "niacinamide": "菸鹼醯胺", "salicylic_acid": "水楊酸",
        "retinol": "視黃醇", "aha": "果酸",
    }
    expansion.extend(ingredient_terms[x] for x in slots.get("ingredients", []) if x in ingredient_terms)
    if slots.get("fragrance_free"):
        expansion.append("無香料")

    raw_values = {
        "intent_type": MAPPING[route],
        "raw_query": query,
        "skin_type": slots.get("skin_type"),
        "concern": concerns,
        "product_category": category,
        "texture_preference": None,
        "budget_range": str(slots["budget_twd_max"]) if slots.get("budget_twd_max") is not None else None,
        "brand_preference": [],
        "urgency_flag": False,
        "ambiguity_score": 1.0 if route == "clarify" else 0.0,
        "routing_confidence": 1.0,
        "task_completeness": 0.0 if route == "clarify" else 1.0,
        "query_expansion": list(dict.fromkeys(expansion)),
    }

    result = {}
    for field, prop_schema in props.items():
        if field in raw_values:
            value = raw_values[field]
            if value is None and _schema_accepts(prop_schema, None):
                result[field] = None
                continue
            if value is None:
                try:
                    value = _neutral(prop_schema)
                except TreatmentError:
                    if field not in required:
                        continue
                    raise
            result[field] = _value_or_neutral(prop_schema, value)
        elif field in required:
            result[field] = _neutral(prop_schema)

    try:
        import jsonschema
        jsonschema.validate(result, schema)
    except Exception as exc:
        raise TreatmentError("LOCAL_RESULT_SCHEMA_VALIDATION_FAILED") from exc
    if result.get("raw_query") != query or result.get("intent_type") != MAPPING[route]:
        raise TreatmentError("LOCAL_RESULT_BINDING_MISMATCH")
    return result


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, TreatmentError):
        value = str(exc)
        # TreatmentError messages are internal symbolic contract codes and may
        # contain field names, never query/model text. Keep evidence bounded.
        if re.fullmatch(r"[A-Z0-9_:-]{1,240}", value):
            return value
        return "TREATMENT_ERROR_UNCLASSIFIED"
    return type(exc).__name__


class Treatment:
    def __init__(self, arm: str, parser_path: Path, policy_path: Path, questions_path: Path,
                 kev_url: str | None, kev_timeout: float = 10.0):
        if arm not in {"A", "B", "C"}:
            raise TreatmentError("INVALID_ARM")
        self.arm = arm
        self.parser = load_parser(parser_path)
        self.policy = load_policy(policy_path)
        self.questions = json.loads(questions_path.read_text(encoding="utf-8"))
        self.kev_url = kev_url.rstrip("/") if kev_url else None
        self.kev_timeout = float(kev_timeout)

    def decide(self, info: dict) -> dict:
        start = time.perf_counter()
        base = {
            "arm": self.arm,
            "query_sha256": info["query_sha256"],
            "query_chars": info["query_chars"],
            "schema_sha256": info["schema_sha256"],
            "local_bypass": False,
            "upstream_called": True,
            "kev_called": False,
            "route": None,
            "source": "baseline_upstream" if self.arm == "A" else "unresolved",
            "error": None,
            "error_code": None,
        }
        if self.arm == "A":
            base["decision_ms"] = (time.perf_counter() - start) * 1000
            return base

        query = info["query"]
        try:
            parsed = self.parser.parse(query)
            hint = self.parser.deterministic_route_hint(parsed)
        except Exception as exc:
            base.update(source="parser_error_fallback", error=type(exc).__name__, error_code=_safe_error_code(exc))
            base["decision_ms"] = (time.perf_counter() - start) * 1000
            return base

        if hint in MAPPING:
            try:
                wire = build_wire_result(hint, parsed, query, info["schema"])
            except Exception as exc:
                base.update(source="parser_assembly_fallback", route=hint, error=type(exc).__name__, error_code=_safe_error_code(exc))
                base["decision_ms"] = (time.perf_counter() - start) * 1000
                return base
            base.update(source="parser", route=hint, local_bypass=True, upstream_called=False, wire=wire)
            base["decision_ms"] = (time.perf_counter() - start) * 1000
            return base

        if hint in NONMAPPABLE:
            base.update(source="parser_nonmappable_fallback", route=hint)
            base["decision_ms"] = (time.perf_counter() - start) * 1000
            return base
        if hint is not None:
            base.update(source="parser_unknown_fallback", route=str(hint))
            base["decision_ms"] = (time.perf_counter() - start) * 1000
            return base
        if self.arm == "C":
            base.update(source="parser_abstain_fallback")
            base["decision_ms"] = (time.perf_counter() - start) * 1000
            return base

        if not self.kev_url:
            base.update(source="kev_unconfigured_fallback", error="KEV_URL_MISSING", error_code="KEV_URL_MISSING")
            base["decision_ms"] = (time.perf_counter() - start) * 1000
            return base

        base["kev_called"] = True
        try:
            import httpx
            kstart = time.perf_counter()
            response = httpx.post(
                self.kev_url + "/v1/systemone",
                json={"state": query, "model": "kev-latest", "questions": self.questions},
                timeout=self.kev_timeout,
            )
            base["kev_ms"] = (time.perf_counter() - kstart) * 1000
            if response.status_code != 200:
                raise TreatmentError("KEV_HTTP_" + str(response.status_code))
            pred = parse_kev(response.json())
            route, source = selective(pred, self.policy)
            base.update(
                source=source,
                route=route or pred["pred_route"],
                kev_prediction=pred,
            )
            if route in MAPPING:
                wire = build_wire_result(route, parsed, query, info["schema"])
                base.update(local_bypass=True, upstream_called=False, wire=wire)
        except Exception as exc:
            base.update(source="kev_error_fallback", error=type(exc).__name__, error_code=_safe_error_code(exc))
        base["decision_ms"] = (time.perf_counter() - start) * 1000
        return base

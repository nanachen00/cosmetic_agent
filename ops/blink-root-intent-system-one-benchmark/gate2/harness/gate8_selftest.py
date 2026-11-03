from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gate8_treatment import build_wire_result, inspect_intent_request, load_parser, load_policy, selective
from gate8_neo4j_routing import merge_exact_hosts, verify_backend_policy

parser = load_parser(HERE / "qualified_parser.py")
policy = load_policy(HERE / "selected_policy.json")

cases = {
    "product": ("請推薦適合混合肌、無香料、1000 元內的清爽保濕產品。", "product"),
    "knowledge": ("我想了解玻尿酸和神經醯胺的差別。", "knowledge"),
    "kev_probe": ("為甚麼油性肌需要注意保濕？", None),
    "safety": ("我擦視黃醇後眼周一直不舒服，還可以繼續使用嗎？", "safety"),
    "conversation": ("謝謝你的回答，我目前沒有其他需求。", "conversation"),
    "clarify": ("我還不確定需要推薦、知識還是追蹤。", "clarify"),
}
observed = {}
for name, (query, expected) in cases.items():
    parsed = parser.parse(query)
    hint = parser.deterministic_route_hint(parsed)
    observed[name] = hint
    if hint != expected:
        raise SystemExit(f"[ERROR] qualified parser selftest drift: {name} expected={expected} got={hint}")

schema = {
    "type": "object",
    "properties": {
        "intent_type": {"type": "string", "enum": ["symptom_solving", "find_product", "recommendation", "price_comparison", "knowledge_query", "unclear"]},
        "skin_type": {"type": "string", "enum": ["unknown", "normal", "dry", "oily", "combination", "sensitive"]},
        "concern": {"type": "array", "items": {"type": "string"}},
        # Production BLINK currently uses mixed wire conventions: skin_type is
        # English while product/texture enums may use Traditional-Chinese values.
        "product_category": {"type": "string", "enum": ["不明", "洗面乳", "化妝水", "面膜", "精華", "乳液", "乳霜", "棉片", "防曬"]},
        "texture_preference": {"type": "string", "enum": ["不明", "清爽", "滋潤"]},
        "budget_range": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "brand_preference": {"type": "array", "items": {"type": "string"}},
        "urgency_flag": {"type": "boolean"},
        "ambiguity_score": {"type": "number", "minimum": 0, "maximum": 1},
        "query_expansion": {"type": "array", "items": {"type": "string"}},
        "raw_query": {"type": "string"},
    },
    "required": [
        "intent_type", "skin_type", "concern", "product_category",
        "texture_preference", "budget_range", "brand_preference",
        "urgency_flag", "ambiguity_score", "query_expansion", "raw_query",
    ],
    "additionalProperties": False,
}
query = cases["knowledge"][0]
payload = {
    "model": "x",
    "input": [{"role": "user", "content": "前文\n【本輪待判斷輸入】\n「" + query + "」"}],
    "text": {"format": {"type": "json_schema", "name": "intent_parse_result", "strict": True, "schema": schema}},
}
info = inspect_intent_request(payload, "/v1/responses")
parsed = parser.parse(query)
wire = build_wire_result("knowledge", parsed, query, schema)
if wire["intent_type"] != "knowledge_query" or wire["raw_query"] != query:
    raise SystemExit("[ERROR] local knowledge wire assembly selftest failed")
if wire["product_category"] != "不明" or wire["texture_preference"] != "不明":
    raise SystemExit("[ERROR] schema-driven Traditional-Chinese neutral enum selection failed")
product_query = cases["product"][0]
product_wire = build_wire_result("product", parser.parse(product_query), product_query, schema)
if product_wire["intent_type"] != "find_product" or product_wire["raw_query"] != product_query:
    raise SystemExit("[ERROR] local product wire assembly selftest failed")

mock = {
    "pred_route": "knowledge",
    "confidence": 0.95,
    "clarification_probability": 0.0,
    "safety_probability": 0.0,
}
route, source = selective(mock, policy)
if route != "knowledge" or source != "kev":
    raise SystemExit("[ERROR] selected policy selftest failed")

routing_cfg = {
    "neo4j_routing_hosts": [
        "p-d0b78ab0-d369-0035.production-orch-1194.neo4j.io"
    ]
}
routing_proof = merge_exact_hosts(
    routing_cfg, HERE/"gate8_neo4j_routing_hosts.json"
)
expected_route_hosts = [
    "p-d0b78ab0-d369-0035.production-orch-1194.neo4j.io",
    "p-d0b78ab0-d369-0036.production-orch-1194.neo4j.io",
]
if routing_cfg["neo4j_routing_hosts"] != expected_route_hosts:
    raise SystemExit("[ERROR] exact Neo4j routing host merge failed")
backend_policy_proof = verify_backend_policy(
    {"host_ports": [
        [expected_route_hosts[0], 7687],
        [expected_route_hosts[1], 7687],
    ]},
    expected_route_hosts,
)
if not backend_policy_proof["pass"]:
    raise SystemExit("[ERROR] exact Neo4j backend policy binding selftest failed")

print(json.dumps({
    "pass": True,
    "parser_hints": observed,
    "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
    "wire_intent": wire["intent_type"],
    "wire_neutral_product_category": wire["product_category"],
    "wire_neutral_texture": wire["texture_preference"],
    "product_wire_intent": product_wire["intent_type"],
    "policy_route": route,
    "neo4j_exact_routing_hosts": routing_proof["effective_hosts"],
    "neo4j_backend_policy_binding": backend_policy_proof["pass"],
}, ensure_ascii=False, indent=2))

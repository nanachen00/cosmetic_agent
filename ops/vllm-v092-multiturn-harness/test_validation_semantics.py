#!/usr/bin/env python3
from benchmark_multiturn_chat import validate
nonce="ABC-123"
s,m,_,_=validate({"type":"json_array"}, "```json\n[\"apple\",\"banana\"]\n```", nonce); assert not s and m
s,m,_,_=validate({"type":"json_array"}, "{\"fruits\":[\"apple\",\"banana\"]}", nonce); assert not s and m
s,m,_,_=validate({"type":"exact_nonce"}, "代碼是 ABC-123", nonce); assert not s and m
s,m,_,_=validate({"type":"exact_nonce"}, "XYZ", nonce); assert not s and not m
s,m,_,_=validate({"type":"json_array"}, "[\"apple\",\"banana\"]", nonce); assert s and m
print("PASS")

# Alias-aware semantic match: strict exact Chinese fails, semantic alias passes.
s, m, _, _ = validate(
    {"type":"json_contains","key":"ingredients","value":"神經醯胺","semantic_values":["ceramide"]},
    '{"ingredients":["water","ceramide"]}',
    nonce,
)
assert s is False and m is True

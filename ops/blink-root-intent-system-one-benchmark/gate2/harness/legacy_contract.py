"""Explicit input-only JSON contract for the legacy :8000 Shadow treatment.

The original conversation stays intact. The original system instructions and the
requested schema are also carried in the final user turn, avoiding reliance on a
legacy template's system-role handling. No generated answer is edited, repaired,
completed, classified, mapped, or retried. Only an input echo may be constrained.
"""
from __future__ import annotations

import copy
import json
import re

from common import Refuse
from native_schema import current_query, digest

MODE = 'legacy_explicit_json'
VERSION = 'legacy_user_contract_v1'
MARKER = '【Shadow API 結構化輸出契約】'
MAX_CONTRACT_BYTES = 256 * 1024


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def enum_rules(schema: dict) -> list[str]:
    """Literal choices from the actual schema; never a predicted intent/answer.

    Conditional alternatives are not flattened into unconditional constraints.
    The complete schema, including local references, is always included below.
    """
    rules = []
    stack = [('$', schema)]
    while stack and len(rules) < 64:
        path, node = stack.pop()
        if not isinstance(node, dict):
            continue
        if isinstance(node.get('enum'), list):
            # raw_query's single input echo is specified separately in the schema.
            if path != '$["raw_query"]':
                rules.append(path + ' 的值只能逐字選自 ' + canonical(node['enum']) +
                             '；不得翻譯、改大小寫、填入其他同義詞或額外類別。')
        props = node.get('properties')
        if isinstance(props, dict):
            for key, value in reversed(list(props.items())):
                stack.append((path + '[' + canonical(key) + ']', value))
    return rules


def attach(out: dict, meta: dict, original_payload: dict, path: str) -> tuple[dict, dict]:
    """Append an explicit contract before generation; keep the caller immutable."""
    if meta.get('output_format_type') not in {'json_schema', 'json_object'}:
        return out, meta  # Free-text generation has identical bytes/parameters.
    messages = out.get('messages')
    if not messages or messages[-1].get('role') != 'user':
        raise Refuse('Legacy JSON contract requires a final user message; no turn is invented')
    if not isinstance(messages[-1].get('content'), str):
        raise Refuse('Legacy JSON contract requires text input')
    # Fail rather than stacking a second synthetic contract on an already adapted
    # request. This is not a test of the member's natural-language capabilities.
    if any(MARKER in str(m.get('content', '')) for m in messages):
        raise Refuse('Legacy JSON contract marker is already present; no double adaptation')
    fmt_type = meta['output_format_type']
    schema = meta.get('schema')
    bound = None
    query = None
    name = None
    if fmt_type == 'json_schema':
        if not isinstance(schema, dict):
            raise Refuse('Legacy JSON schema is missing')
        import jsonschema
        try:
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except jsonschema.exceptions.SchemaError:
            raise Refuse('Legacy JSON schema is invalid') from None
        fmt = ((original_payload.get('text') or {}).get('format')
               if path.rstrip('/') == '/v1/responses'
               else original_payload.get('response_format')) or {}
        inner = fmt.get('json_schema', fmt)
        name = inner.get('name', 'shadow_schema')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            raise Refuse('Legacy schema name is invalid')
        bound = copy.deepcopy(schema)
        props = schema.get('properties', {})
        if name == 'intent_parse_result':
            if (schema.get('type') != 'object' or not isinstance(props, dict)
                    or 'raw_query' not in schema.get('required', [])
                    or not isinstance(props.get('raw_query'), dict)
                    or props['raw_query'].get('type') != 'string'
                    or not isinstance(props.get('intent_type'), dict)):
                raise Refuse('Legacy intent schema contract is incompatible')
            query = current_query(messages)
            try:
                jsonschema.validate(query, props['raw_query'])
            except jsonschema.exceptions.ValidationError:
                raise Refuse('Current input violates the original raw_query schema') from None
            bound['properties']['raw_query']['enum'] = [query]
        elif isinstance(props, dict) and 'raw_query' in props:
            raise Refuse('Unrecognized raw_query schema; no guessed input echo applied')
    system_texts = [m['content'] for m in messages if m.get('role') == 'system']
    parts = [MARKER,
             '對上方本輪輸入執行原本任務。以下僅補充 API 的輸出格式與原始任務指令，'
             '不是新的會員訊息，也不預先指定分類答案。']
    if system_texts:
        parts += ['【原始任務指令，逐字保留】', '\n\n'.join(system_texts)]
    parts += ['【輸出格式】',
              '只回傳一個可直接解析的 JSON 值。禁止 Markdown 程式碼框、前言、後記、'
              '註解或 JSON 外的文字。字串使用雙引號；不得有尾隨逗號。',
              'enum 與 const 是必須逐字遵守的值，不是範例；required 欄位不可省略。'
              '不得自行發明 unknown、other、中文翻譯或其他未列出的分類。'
              '若輸入資訊不足，仍須依原始任務指令與 schema 允許的方式表達，不得補造事實。']
    if fmt_type == 'json_object' or (schema or {}).get('type') == 'object':
        parts.append('第一個非空白字元必須為 {，最後一個非空白字元必須為 }。')
    if bound is not None:
        parts += ['【完整 JSON Schema】', canonical(bound)]
        rules = enum_rules(bound)
        if rules:
            parts += ['【分類值提醒；不替你選答案】', '\n'.join(rules)]
        if query is not None:
            parts.append('raw_query 必須等於 schema 中該欄位 enum 的唯一原文；'
                         '不得摘要、翻譯或改寫。intent_type 及其他判斷欄位仍由你依輸入選擇。')
    parts.append('現在直接輸出 JSON，不使用程式碼框。')
    contract = '\n'.join(parts)
    if len(contract.encode('utf-8')) > MAX_CONTRACT_BYTES:
        raise Refuse('Legacy explicit contract exceeds the bounded input size; no truncation applied')
    result = copy.deepcopy(out)
    result['messages'][-1]['content'] += '\n\n' + contract
    info = dict(meta)
    info.update(schema_mode=MODE, legacy_contract_requested=True,
                legacy_contract_version=VERSION, schema_name=name,
                legacy_raw_query_bound=query is not None,
                classification_labels_pinned=False, response_repaired=False,
                input_adaptation_only=True,
                legacy_contract_fingerprint={
                    'source_messages_sha256': digest(messages),
                    'forwarded_messages_sha256': digest(result['messages']),
                    'original_schema_sha256': digest(schema) if schema is not None else None,
                    'prompt_schema_sha256': digest(bound) if bound is not None else None,
                    'contract_sha256': digest(contract),
                    'contract_chars': len(contract),
                    'system_instruction_copies': len(system_texts)},
                _legacy_bound_schema=bound)
    return result, info

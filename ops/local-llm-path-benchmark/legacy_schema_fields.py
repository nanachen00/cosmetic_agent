"""Schema-derived, input-only legacy :8000 treatment.

The original messages remain byte-for-byte as message values; only the final
user content receives the format contract. Unlike legacy_user_contract_v1, this
variant does not duplicate the original system/task instructions into the user
turn. No example answer is selected, no classification is fixed, and no model
reply is completed, mapped, repaired, retried or replaced.
"""
from __future__ import annotations

import copy

from common import Refuse
from native_schema import digest
import legacy_contract as previous

MODE = 'legacy_schema_fields'
VERSION = 'legacy_schema_fields_v2'
SERIALIZATION_PROFILE = 'compact_utf8_json_v1'
MARKER = '【Shadow API 逐欄輸出契約】'
MAX_FIELDS = 256


def field_rules(schema: dict) -> list[str]:
    """Render field requirements without turning annotations into answers.

    Local $refs are shown at the use site; alternatives/conditions stay explicit
    rather than being flattened into unconditional constraints. The unchanged
    complete schema accompanies this guide and remains the validation authority.
    """
    rules: list[str] = []
    count = 0

    def visit(node, path: str, required: bool | None, refs: tuple[str, ...] = ()):
        nonlocal count
        count += 1
        if count > MAX_FIELDS:
            raise Refuse('Legacy field guide exceeds its bounded size; no schema is truncated')
        status = '必要欄位' if required is True else '僅在完整 schema 允許時可省略' if required is False else '根節點／元素約束'
        lines = [path + '：' + status + '。']
        if isinstance(node, bool):
            lines.append('schema 布林約束：' + previous.canonical(node))
            rules.append('\n'.join(lines)); return
        if not isinstance(node, dict):
            raise Refuse('Invalid field schema; no guessed contract')
        if 'type' in node:
            lines.append('JSON 型別：' + previous.canonical(node['type']) + '。')
        choices = node.get('enum')
        if isinstance(choices, list):
            lines.append('只能選下列其中一個 JSON 常值；不是輸出整份選項清單，也不可改寫選項：')
            lines.extend('  ' + str(i) + ') ' + previous.canonical(v) for i, v in enumerate(choices, 1))
        if 'const' in node:
            lines.append('此欄位的 const 約束：' + previous.canonical(node['const']))
        if 'default' in node:
            # A JSON Schema default is an annotation, not proof about the member.
            lines.append('schema 含 default 註記；仍須依原始任務判斷，不自動把它當成會員事實。')
        if node.get('additionalProperties') is False:
            lines.append('不得新增此物件未定義的欄位。')
        for key in ('anyOf', 'oneOf', 'allOf', 'if', 'then', 'else', 'not',
                    'dependentRequired', 'dependentSchemas'):
            if key in node:
                lines.append('另有 ' + key + ' 條件，依下方完整 schema 判斷；不要把不同分支合併成一個要求。')
        rules.append('\n'.join(lines))
        ref = node.get('$ref')
        if isinstance(ref, str):
            if ref in refs:
                rules.append(path + '：遞迴參照 ' + ref + '，依完整 schema；此處不無限展開。')
            elif ref == '#' or ref.startswith('#/'):
                target = schema
                try:
                    for part in ([] if ref == '#' else ref[2:].split('/')):
                        part = part.replace('~1', '/').replace('~0', '~')
                        target = target[int(part)] if isinstance(target,list) and part.isdigit() else target[part]
                except (KeyError, TypeError, IndexError):
                    raise Refuse('Unresolved local schema reference; no guessed field contract') from None
                visit(target, path + '（參照約束）', required, refs + (ref,))
            else:
                # Never retrieve an external resource while constructing a prompt.
                raise Refuse('Legacy field guide supports local schema references only')
        props = node.get('properties')
        if isinstance(props, dict):
            needed = node.get('required', [])
            for key, child in props.items():
                visit(child, path + '[' + previous.canonical(key) + ']', key in needed, refs)
        items = node.get('items')
        if isinstance(items, (dict, bool)):
            visit(items, path + '[*]', None, refs)
        elif isinstance(items, list):
            for index, child in enumerate(items):
                visit(child, path + '[' + str(index) + ']', None, refs)
        for index, child in enumerate(node.get('prefixItems', [])):
            visit(child, path + '[' + str(index) + ']', None, refs)

    visit(schema, '$', None)
    return rules


def attach(out: dict, meta: dict, original_payload: dict, path: str) -> tuple[dict, dict]:
    if meta.get('output_format_type') not in {'json_schema', 'json_object'}:
        return out, meta
    messages = out.get('messages', [])
    if any(MARKER in str(m.get('content', '')) for m in messages):
        raise Refuse('Legacy field contract is already present; no double adaptation')
    # Reuse the established immutable input-echo/schema compatibility checks.
    # The v1 candidate is discarded BEFORE any HTTP call; no answer is involved.
    _, info = previous.attach(out, meta, original_payload, path)
    bound = info.get('_legacy_bound_schema')
    parts = [MARKER,
             '仍執行原本 system 指令與上方本輪輸入的任務。此區只說明 API 輸出契約；'
             '不是新的會員訊息，也不是要你回傳 schema。原始任務指令不在此重複。',
             '【序列化規則】',
             '只輸出一個完整、緊湊的 JSON 值。不得輸出 Markdown 程式碼框、註解、推理過程、前言或後記。'
             '不要排版或縮排，不要加入不必要的空白與換行；欄位之間使用逗號，最後一個欄位後不要逗號，並閉合所有括號。',
             '欄位名稱與字串值使用 JSON 雙引號。只對 JSON 必須跳脫的雙引號、反斜線與控制字元做跳脫。'
             '一般中文與其他 Unicode 字元必須直接輸出為 UTF-8 字元，不得改寫成 \\uXXXX escape；'
             '例如字串「測試」要直接寫成「測試」，不可寫成「\\u6e2c\\u8a66」。'
             '不得使用 Python 的 None、True、False、單引號或省略號。',
             'enum 清單內每一項都是一個可用的 JSON 常值。只選一項並逐字使用；'
             '不要把整個 enum 清單當作答案，也不要自行改成 null、空字串、unknown 或近義詞。'
             '只有該欄位約束本來允許的值才能輸出。',
             '若資訊不足，依原始任務與該欄位允許的值表達；不要捏造會員偏好。'
             '型別允許 null 不代表 enum 也允許 null，所有約束必須同時成立。']
    if meta.get('output_format_type') == 'json_object' or (bound or {}).get('type') == 'object':
        parts.append('第一個非空白字元必須為 {，最後一個非空白字元必須為 }。')
    if bound is not None:
        parts += ['【完整 JSON Schema；保留全部原始約束】', previous.canonical(bound),
                  '【逐欄核對表；每個分類仍由模型依本輪輸入判斷】', '\n\n'.join(field_rules(bound))]
        if info.get('legacy_raw_query_bound'):
            literal = previous.canonical(bound['properties']['raw_query']['enum'][0])
            parts += ['【原文回填的 JSON 字串常值；不是分類答案】',
                      'raw_query 的值須逐字使用下列 JSON 常值；其中一般中文／Unicode 保持直接字元，不要重新編碼為 \\uXXXX：', literal]
    parts.append('直接輸出最終緊湊 JSON。送出前檢查：必要欄位、逐欄 enum、JSON 型別、必要字串跳脫、'
                 '沒有 \\uXXXX Unicode escape、逗號及閉合括號；不要輸出檢查清單。')
    contract = '\n'.join(parts)
    if len(contract.encode('utf-8')) > previous.MAX_CONTRACT_BYTES:
        raise Refuse('Legacy field contract exceeds bounded input size; no truncation applied')
    result = copy.deepcopy(out)
    result['messages'][-1]['content'] += '\n\n' + contract
    info = dict(info)
    info.update(schema_mode=MODE, legacy_contract_version=VERSION,
                legacy_serialization_profile=SERIALIZATION_PROFILE,
                legacy_contract_fingerprint={
                    'source_messages_sha256': digest(messages),
                    'forwarded_messages_sha256': digest(result['messages']),
                    'original_schema_sha256': digest(meta.get('schema')) if meta.get('schema') is not None else None,
                    'prompt_schema_sha256': digest(bound) if bound is not None else None,
                    'contract_sha256': digest(contract), 'contract_chars': len(contract),
                    'serialization_profile': SERIALIZATION_PROFILE,
                    'system_instruction_copies': 0})
    return result, info

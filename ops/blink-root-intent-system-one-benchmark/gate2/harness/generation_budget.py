"""Explicit, paired intent-output budget treatment; no prompt or reply changes.

Only the reviewed intent_parse_result call with an established raw-query binding
is eligible. The production caller's limit remains separately recorded. A shared
server may still impose a lower internal limit: outgoing HTTP evidence does not
prove that the model worker honored this request parameter.
"""
from __future__ import annotations

from common import Refuse

VERSION = 'intent_output_floor_v1'
REVIEWED_FLOOR = 1024


def validate_floor(value):
    if value is None:
        return None
    if type(value) is not int or value != REVIEWED_FLOOR:
        raise Refuse('Only the explicit paired intent output floor of 1024 is reviewed')
    return value


def positive_int(value):
    return value if type(value) is int and value > 0 else None


def wire_limit(body):
    """Normalize only numeric API budget fields, without reading message values."""
    keys = [k for k in ('max_tokens', 'max_completion_tokens') if k in body]
    values = [positive_int(body[k]) for k in keys]
    if not keys:
        return {'status': 'absent', 'value': None}
    if None in values or len(set(values)) != 1:
        return {'status': 'invalid_or_conflicting', 'value': None}
    return {'status': 'observed', 'value': values[0]}


def attach(out, meta, *, floor=None):
    """Change only max_tokens for eligible intents; never mutate caller objects."""
    floor = validate_floor(floor)
    if floor is None:
        return out, meta
    applicable = meta.get('schema_name') == 'intent_parse_result'
    original = positive_int(out.get('max_tokens'))
    result = out
    if applicable:
        if (meta.get('output_format_type') != 'json_schema'
                or not (meta.get('legacy_raw_query_bound') is True
                        or meta.get('raw_query_bound') is True)):
            raise Refuse('Intent budget requires the existing verified input-echo binding')
        if original is None or positive_int(meta.get('requested_max_tokens')) != original:
            raise Refuse('Intent output budget is missing; no default is guessed')
        result = dict(out)
        result['max_tokens'] = max(original, floor)
    effective = positive_int(result.get('max_tokens'))
    info = dict(meta)
    info['effective_max_tokens'] = effective
    info['generation_budget'] = {
        'version': VERSION, 'intent_output_token_floor': floor,
        'applicable': applicable, 'caller_max_tokens': original,
        'effective_max_tokens': effective,
        'changed': applicable and effective != original,
        'scope': 'intent_parse_result_only',
        'prompt_changed': False, 'sampling_changed': False,
        'response_repaired': False, 'additional_model_calls': 0,
        'provider_internal_limit_verified': False,
    }
    return result, info


def call_budget_verified(model, floor):
    """Verify declared treatment before wire matching; absent metadata cannot pass."""
    try:
        validate_floor(floor)
    except Refuse:
        return False
    if floor is None:
        return True
    b = model.get('generation_budget')
    if not isinstance(b, dict):
        return False
    applicable = model.get('schema_name') == 'intent_parse_result'
    caller = b.get('caller_max_tokens')
    effective = b.get('effective_max_tokens')
    if (b.get('version') != VERSION or positive_int(b.get('intent_output_token_floor')) != floor
            or b.get('applicable') is not applicable
            or b.get('prompt_changed') is not False
            or b.get('sampling_changed') is not False
            or b.get('response_repaired') is not False
            or type(b.get('additional_model_calls')) is not int
            or b['additional_model_calls'] != 0):
        return False
    if applicable:
        if positive_int(caller) is None or positive_int(effective) != max(caller, floor) or positive_int(model.get('requested_max_tokens')) != caller:
            return False
        if not (model.get('legacy_raw_query_bound') is True or model.get('raw_query_bound') is True):
            return False
    elif effective != caller:
        return False
    return (b.get('changed') is (applicable and effective != caller)
            and model.get('effective_max_tokens') == effective)


def public_budget(value):
    """Allowlisted numbers/flags only; no schema, raw text, or arbitrary values."""
    if not isinstance(value, dict):
        return None
    return {
        'version': VERSION if value.get('version') == VERSION else 'UNKNOWN',
        'intent_output_token_floor': positive_int(value.get('intent_output_token_floor')),
        'applicable': value.get('applicable') is True,
        'caller_max_tokens': positive_int(value.get('caller_max_tokens')),
        'effective_max_tokens': positive_int(value.get('effective_max_tokens')),
        **{k: value.get(k) if type(value.get(k)) is bool else None for k in
           ('changed', 'prompt_changed', 'sampling_changed', 'response_repaired')},
        'additional_model_calls': value.get('additional_model_calls') if type(value.get('additional_model_calls')) is int else None,
        'provider_internal_limit_verified': False,
    }


def public_wire_limit(value):
    """Keep shareable outgoing budget evidence small and content-free."""
    if not isinstance(value, dict):
        return {'status':'unavailable','value':None}
    status=value.get('status')
    return {'status':status if isinstance(status,str) and status in {'observed','absent','invalid_or_conflicting'} else 'unavailable',
            'value':positive_int(value.get('value'))}

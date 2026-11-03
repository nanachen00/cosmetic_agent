"""Provider treatment selection and content-free outgoing endpoint evidence.

No HTTP, service control, model loading, output repair, or retry is performed here.
Existing prompt_only/native experiments retain their historical gate definitions.
"""
from __future__ import annotations
from collections import Counter
import json
import re
from common import Refuse
from native_schema import digest

COMPARISON_MODE = 'provider_specific'


class ProviderGateFailure(Refuse):
    """A bounded provider readiness/probe failure, not an ownership/auth failure."""
    def __init__(self, category: str):
        if category not in {'PROVIDER_UNAVAILABLE', 'PROVIDER_WITNESS_TRANSPORT_FAILED'}:
            raise ValueError('Unknown provider failure category')
        self.category = category
        super().__init__(category)


def strategy(config: dict, provider: int) -> dict:
    mode = config.get('schema_mode', 'prompt_only')
    if mode == COMPARISON_MODE:
        if provider not in (8000, 8200):
            raise Refuse('A/B treatment requires shared :8000 or :8200')
        legacy=config.get('legacy_contract_mode','legacy_prompt_contract')
        if legacy not in {'legacy_prompt_contract','legacy_explicit_json','legacy_schema_fields'}:
            raise Refuse('Unknown legacy input contract')
        wire = (legacy if legacy in {'legacy_explicit_json','legacy_schema_fields'} else 'prompt_only') if provider == 8000 else 'vllm_native_raw_query'
    elif mode in {'prompt_only', 'vllm_native_raw_query', 'legacy_explicit_json', 'legacy_schema_fields'}:
        wire = mode
    else:
        raise Refuse('Unknown provider schema strategy')
    if wire in {'legacy_explicit_json','legacy_schema_fields'} and provider!=8000:
        raise Refuse('Explicit legacy input contract may only target :8000')
    if provider == 8000 and wire not in {'prompt_only','legacy_explicit_json','legacy_schema_fields'}:
        raise Refuse('Legacy :8000 cannot use native structured generation')
    contract = (wire if wire in {'legacy_explicit_json','legacy_schema_fields'} else
                'legacy_prompt_contract' if provider == 8000 else
                'vllm_native_raw_query' if wire == 'vllm_native_raw_query' else 'prompt_only')
    from generation_budget import validate_floor, VERSION as budget_version
    floor=validate_floor(config.get('intent_output_token_floor'))
    if floor is not None and wire not in {'legacy_schema_fields','vllm_native_raw_query'}:
        raise Refuse('Intent output floor requires the reviewed provider schema adapters')
    adaptation={}
    if wire in {'legacy_explicit_json','legacy_schema_fields'}:
        if wire=='legacy_schema_fields':
            from legacy_schema_fields import VERSION as input_version, SERIALIZATION_PROFILE
            adaptation={'input_contract_version':input_version,
                        'legacy_serialization_profile':SERIALIZATION_PROFILE,
                        'input_adaptation_only':True,'classification_labels_pinned':False,'response_repaired':False}
        else:
            adaptation={'input_contract_version':'legacy_user_contract_v1',
                        'input_adaptation_only':True,'classification_labels_pinned':False,'response_repaired':False}
    return {'provider_port': provider, 'contract_mode': contract, 'schema_mode': wire,
            'require_endpoint_proof': mode == COMPARISON_MODE,
            'comparison_kind': 'deployed_integration_not_pure_engine',
            **({'intent_output_token_floor':floor,'generation_budget_version':budget_version} if floor is not None else {}),
            **adaptation,
            **({'endpoint_correlation': 'gateway_proxy_call_id_v1'} if mode == COMPARISON_MODE else {})}


def outgoing_metadata(raw) -> dict:
    """Hash only the stable message/format/model envelope; export no text values.

    Sampling/token parameters are deliberately not used as transport identity:
    LiteLLM may add documented defaults. They remain in existing call metadata.
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 4 * 1024 * 1024:
            return {'status': 'unavailable'}
        body = json.loads(raw)
        if (not isinstance(body, dict) or not isinstance(body.get('messages'), list)
                or not body['messages'] or body.get('model') != 'taide-12b'):
            return {'status': 'unavailable'}
        from generation_budget import wire_limit
        return {'status': 'observed', 'output_token_limit': wire_limit(body), 'request_sha256': digest({
            'model': body['model'], 'messages': body['messages'],
            'response_format': body.get('response_format')}),
            'observation_point': 'outgoing_chat_http'}
    except (UnicodeError, ValueError, TypeError):
        return {'status': 'unavailable'}


def legacy_endpoint_proof(models: list[dict], dependencies: list[dict], provider: int) -> dict:
    """Case-local destination/fingerprint multiplicity, never invented upstream IDs.

    Multiple equal requests cannot be uniquely paired when the proxy drops IDs.
    All matching outgoing calls must target the selected endpoint. Extra matching
    attempts remain visible as retries/duplicates, not silently deduplicated.
    This proves outgoing Python HTTP observations, not kernel/model execution.
    """
    wanted = Counter()
    observed = Counter()
    malformed = wrong_port = unknown_wire = 0
    def key(meta):
        value = (meta or {}).get('request_sha256')
        return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) else None
    for row in models:
        k = key(row.get('endpoint_request_fingerprint'))
        if row.get('provider_port') != provider:
            wrong_port += 1
        if k is None:
            malformed += 1
        else:
            wanted[k] += 1
    attempts = 0
    for row in dependencies:
        if (row.get('role') != 'proxy' or not row.get('model_http')
                or row.get('path') != '/v1/chat/completions'):
            continue
        attempts += 1
        if row.get('port') != provider or row.get('host') not in {'127.0.0.1', 'localhost', '::1'}:
            wrong_port += 1
            continue
        meta = row.get('endpoint_wire') or {}
        k = key(meta)
        if meta.get('status') != 'observed' or k is None:
            unknown_wire += 1
        else:
            observed[k] += 1
    missing = sum(max(0, n - observed[k]) for k, n in wanted.items())
    unexplained = sum(n for k, n in observed.items() if k not in wanted)
    extras = sum(max(0, observed[k] - n) for k, n in wanted.items())
    return {'pass': bool(models) and not (malformed or wrong_port or unknown_wire or missing or unexplained),
            'selected_port': provider, 'gateway_calls': len(models), 'outgoing_attempts': attempts,
            'missing': missing, 'malformed': malformed, 'wrong_destination': wrong_port,
            'unobserved_wire': unknown_wire, 'unexplained_outgoing_calls': unexplained,
            'extra_matching_attempts': extras,
            'correlation_scope': 'case-local destination/fingerprint multiset; not unique upstream request IDs',
            'provider_kernel_execution_verified': False}


def endpoint_proof(models: list[dict], dependencies: list[dict], provider: int,
                   *, correlation: str | None = None, ingress: list[dict] | None = None) -> dict:
    """Require exact per-call joins for new runs; never downgrade to fingerprints.

    Historical mode retains its old strict case-wide definition. New runs prove
    gateway call ID -> proxy ingress -> each outgoing attempt AND payload hash.
    Other selected-port proxy traffic stays visible, is never called a retry or
    credited to a member, and never compensates for a missing member attempt.
    Every observed inference destination is checked even for unassigned traffic.
    """
    if correlation is None:
        return legacy_endpoint_proof(models, dependencies, provider)
    if correlation != 'gateway_proxy_call_id_v1':
        raise Refuse('Unknown endpoint evidence contract')
    from proxy_trace import valid_call_id, VERSION
    from generation_budget import positive_int, public_wire_limit
    def fp(meta):
        value = (meta or {}).get('request_sha256')
        return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) else None
    wanted = {}
    starts = {}; ends = {}
    failures = Counter()
    for m in models:
        cid = m.get('gateway_call_id')
        if m.get('provider_port') != provider: failures['wrong_destination'] += 1
        if not valid_call_id(cid): failures['missing_gateway_call_id'] += 1; continue
        if cid in wanted: failures['duplicate_gateway_call_id'] += 1; continue
        wanted[cid] = m
        if fp(m.get('endpoint_request_fingerprint')) is None: failures['malformed'] += 1
    for row in ingress or []:
        cid = row.get('gateway_call_id')
        if not valid_call_id(cid): continue
        target = starts if row.get('event') == 'proxy_request_started' else ends if row.get('event') == 'proxy_request_completed' else None
        if target is not None: target.setdefault(cid, []).append(row)
    counts = Counter(); attempts = 0; unassigned = []; bound = []
    for row in dependencies:
        if row.get('role') != 'proxy' or not row.get('model_http'): continue
        if row.get('path') not in {'/v1/chat/completions', '/chat/completions', '/v1/responses'}: continue
        attempts += 1
        if row.get('port') != provider or row.get('host') not in {'127.0.0.1', 'localhost', '::1'}:
            failures['wrong_destination'] += 1
        if row.get('path') != '/v1/chat/completions': failures['unexpected_inference_path'] += 1
        if row.get('proxy_observer') != VERSION: failures['proxy_observer_missing'] += 1
        cid = row.get('gateway_call_id')
        if cid is None:
            if row.get('proxy_context') not in {'no_ingress_context', 'active_ingress', 'after_ingress'}:
                failures['proxy_context_unavailable'] += 1
            # Explicitly observed but not attributable. Do NOT use its hash to
            # satisfy any gateway call (including equal/duplicate messages).
            unassigned.append(row)
            continue
        if not valid_call_id(cid) or cid not in wanted:
            failures['orphan_correlated_attempt'] += 1; continue
        counts[cid] += 1
        model = wanted[cid]
        wire = row.get('endpoint_wire') or {}
        if wire.get('status') != 'observed' or fp(wire) is None:
            failures['unobserved_wire'] += 1
        elif fp(wire) != fp(model.get('endpoint_request_fingerprint')):
            failures['payload_fingerprint_mismatch'] += 1
        budget=model.get('generation_budget') or {}
        if budget.get('applicable') is True:
            expected=budget.get('effective_max_tokens')
            if type(expected) is not int or expected<=0 or (model.get('endpoint_request_fingerprint') or {}).get('output_token_limit')!={'status':'observed','value':expected}:
                failures['gateway_output_budget_unverified']+=1
            if wire.get('output_token_limit')!={'status':'observed','value':expected}:
                failures['output_budget_wire_mismatch']+=1
        if row.get('proxy_parent_request_id') != model.get('request_id'):
            failures['request_id_mismatch'] += 1
        if row.get('proxy_context') not in {'active_ingress', 'after_ingress'}:
            failures['proxy_context_unavailable'] += 1
        seen = starts.get(cid, [])
        if len(seen) == 1 and row.get('proxy_ingress_id') != seen[0].get('proxy_ingress_id'):
            failures['ingress_id_mismatch'] += 1
        bound.append(row)
    for cid, model in wanted.items():
        for groups, name in ((starts, 'proxy_ingress_missing'), (ends, 'proxy_completion_missing')):
            rows = groups.get(cid, [])
            if len(rows) != 1:
                failures[name] += 1
            elif (rows[0].get('proxy_observer') != VERSION or not rows[0].get('inference_path')
                  or rows[0].get('request_id') != model.get('request_id')):
                failures['proxy_ingress_mismatch'] += 1
        if not counts[cid]: failures['missing'] += 1
    # An ingress with a generated gateway ID must eventually have its gateway
    # completion row. Audit readers wait, not resend requests, before this gate.
    failures['orphan_gateway_ingress'] += len((set(starts) | set(ends)) - set(wanted))
    reasons = sorted(k for k, v in failures.items() if v)
    details = []
    for cid, m in wanted.items():
        details.append({'gateway_call_id': cid, 'request_id': m.get('request_id'),
                        'outgoing_attempts': counts[cid], 'provider_port': provider,
                        **({'caller_max_tokens':positive_int((m.get('generation_budget') or {}).get('caller_max_tokens')),
                            'effective_max_tokens':positive_int((m.get('generation_budget') or {}).get('effective_max_tokens')),
                            'observed_output_token_limits':[public_wire_limit((r.get('endpoint_wire') or {}).get('output_token_limit')) for r in bound if r.get('gateway_call_id')==cid],
                            'provider_internal_limit_verified':False} if m.get('generation_budget') else {})})
    return {'pass': bool(models) and not reasons, 'selected_port': provider,
            'gateway_calls': len(models), 'outgoing_attempts': attempts,
            'correlated_outgoing_attempts': len(bound),
            'unattributed_proxy_attempts': len(unassigned),
            'unattributed_errors': sum(r.get('status') != 200 or bool(r.get('error')) for r in unassigned),
            'unattributed_scope': 'observed without a gateway call ID; origin not established; excluded from member call counts',
            'missing': failures['missing'], 'malformed': failures['malformed'],
            'wrong_destination': failures['wrong_destination'], 'unobserved_wire': failures['unobserved_wire'],
            'unexplained_outgoing_calls': failures['orphan_correlated_attempt'],
            'extra_matching_attempts': sum(max(0, n - 1) for n in counts.values()),
            'failure_reasons': reasons, 'failure_counts': {k: v for k, v in sorted(failures.items()) if v},
            'calls': details,
            'correlation_scope': 'gateway call ID + proxy ASGI ingress + exact outgoing message/format/model fingerprint',
            'evidence_version': 'gateway_proxy_call_id_v1',
            'provider_kernel_execution_verified': False}


def native_dependencies(models, dependencies, treatment):
    """Exclude unassigned proxy traffic from a native call's multiplicity check.

    The independent endpoint gate still requires exact call IDs, fingerprints,
    all destinations, and no orphan correlated attempts. Historical native runs
    keep their previous evidence definition.
    """
    if treatment.get('endpoint_correlation') != 'gateway_proxy_call_id_v1':
        return dependencies
    ids = {m.get('gateway_call_id') for m in models if m.get('native_schema_requested')}
    return [d for d in dependencies if d.get('gateway_call_id') in ids and d.get('gateway_call_id')]

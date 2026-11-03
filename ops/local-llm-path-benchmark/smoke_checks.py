"""Evidence-only path checks shared by Smoke and formal reporting.

No BLINK modules are imported and no requests/signals/config writes are made.
These checks establish observed path/wire success, NOT cosmetic answer quality.
"""
from __future__ import annotations
import json
from pathlib import Path
import time
from common import Refuse, atomic_json

BLOCK_EVENTS = {'egress_blocked', 'production_file_access_blocked', 'import_origin_blocked'}
KNOWN_FAILURE_REPLIES = {
    '目前無法取得相關肌膚知識，請稍後再試。',
    '目前暫時無法完成知識查詢，請稍後再試一次。',
    '抱歉，系統忙碌中，請稍後再試一次 🙏',
}


def json_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # raw application stdout can also contain non-JSON lines
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _backend_stage_evidence(out: Path, backend_log: Path | None = None) -> tuple[list[dict], dict]:
    # Exact paths only. No recursive search and no substitution of another
    # provider/scenario or production log when an evidence file is absent.
    phase_root = out.parents[1]
    candidates = [Path(backend_log)] if backend_log is not None else [
        phase_root / 'service-logs' / out.parent.name / out.name / 'backend.log',
    ]
    if backend_log is None and phase_root.name in {'auth', 'smoke', 'formal'}:
        # Compatibility with the prior phase exporter (duplicated phase segment)
        # and the root cleanup exporter. Both occur in existing result archives.
        candidates.extend([
            phase_root / 'service-logs' / phase_root.name / out.parent.name / out.name / 'backend.log',
            phase_root.parent / 'service-logs' / phase_root.name / out.parent.name / out.name / 'backend.log',
        ])
    found = []
    for path in dict.fromkeys(candidates):
        if path.is_symlink():
            raise Refuse('Stage log must not be a symlink')
        if not path.is_file():
            continue
        rows = [r for r in json_events(path) if r.get('event') == 'pipeline_stage_completed']
        found.append((path, rows))
    if len(found) > 1 and any(rows != found[0][1] for _, rows in found[1:]):
        raise Refuse('Conflicting copies of this case stage evidence; refusing an arbitrary selection')
    selected = found[0] if found else None
    return (selected[1] if selected else []), {
        'backend_log_found': bool(found),
        'backend_log': str(selected[0]) if selected else None,
        'matching_log_copies': len(found),
        'stage_event_count': len(selected[1]) if selected else 0,
        'scope': 'exact provider/scenario paths; no fabricated stage events',
    }


def case_evidence(out: Path, backend_log: Path | None = None) -> dict:
    stage_rows, stage_source = _backend_stage_evidence(out, backend_log)
    audits = []
    for path in sorted((out / 'trace').glob('*audit.jsonl')):
        audits.extend(json_events(path))
    return {
        'treatment': json.loads((out/'treatment.json').read_text()) if (out/'treatment.json').is_file() else {},
        'stages': stage_rows,
        'stage_source': stage_source,
        'auth_resolution':json_events(out/'trace/auth-resolution.jsonl'),
        'model_calls': json_events(out / 'trace/model-calls.jsonl'),
        'asgi': json_events(out / 'trace/asgi-timing.jsonl'),
        'dependencies': [r for r in audits if r.get('event') == 'dependency_http'],
        'proxy_ingress': [r for r in audits if r.get('event') in {'proxy_request_started','proxy_request_completed'}],
        'blocked': [r for r in audits if r.get('event') in BLOCK_EVENTS],
        'allowed_subprocesses': [r for r in audits if r.get('event') == 'subprocess_allowed'],
    }


def evaluate_request(row: dict, scenario: str, evidence: dict, reply: str | None = None) -> dict:
    if scenario not in {'product_search', 'skin_consult', 'auto_root'}:
        raise ValueError('Unsupported scenario: ' + scenario)
    rid = row.get('request_id')
    stages = [s for s in evidence['stages'] if rid and s.get('request_id') == rid]
    models = [s for s in evidence['model_calls'] if rid and s.get('request_id') == rid]
    deps = [s for s in evidence['dependencies'] if rid and s.get('request_id') == rid and s.get('model_http')]
    asgi = [s for s in evidence['asgi'] if rid and s.get('request_id') == rid
            and s.get('path') == '/api/chat/message']
    expected = 'product' if scenario == 'product_search' else 'knowledge'
    route = 'product_search' if expected == 'product' else 'skin_consult'

    def stage_ok(pipeline: str, stage: str, prefix: bool = False) -> bool:
        return any(s.get('pipeline') == pipeline and s.get('status') == 'success'
                   and (str(s.get('stage', '')).startswith(stage) if prefix
                        else s.get('stage') == stage) for s in stages)

    terminal = stage_ok('product', 'vector_search') if expected == 'product' else stage_ok('knowledge', 'final_generation_', True)
    parent = True if scenario == 'skin_consult' else stage_ok('root', 'agent_dispatch')
    model_errors = [m for m in models if m.get('status') != 200 or m.get('error')]
    dependency_errors = [m for m in deps if m.get('status') != 200 or m.get('error')]
    schema_errors = [m for m in models if m.get('schema_validation') in {'fail', 'validator_unavailable'}
                     or (m.get('schema_mode') in {'prompt_only','vllm_native_raw_query','vllm_native_json_schema','legacy_explicit_json','legacy_schema_fields'} and m.get('schema_validation') != 'pass')]
    from native_schema import transport_proof
    from provider_strategy import native_dependencies
    native_proof = transport_proof(evidence['model_calls'], native_dependencies(
        evidence['model_calls'], evidence['dependencies'], evidence.get('treatment') or {}))
    native_requested=any(m.get('native_schema_requested') for m in models)
    truncated = [m for m in models if m.get('finish_reason') in {'length', 'content_filter'}]
    checks = {
        'http_200_nonempty': row.get('status') == 200 and bool(row.get('reply_nonempty')) and not row.get('error'),
        'request_id_match': bool(rid) and bool(row.get('correlated')),
        'asgi_complete_200': any(s.get('status') == 200 and s.get('total_ms') is not None for s in asgi),
        'response_dispatch_stage': row.get('stage') == 'dispatch',
        'expected_response_route': row.get('route') == route,
        'expected_terminal_stage': terminal,
        'root_dispatch_observed': parent,
        'no_stage_errors': not any(s.get('status') in {'error', 'failed', 'failure'} for s in stages),
        'model_call_observed': bool(models) if expected == 'knowledge' else True,
        'no_llm_http_errors': not model_errors and not dependency_errors,
        'schema_verified_when_requested': not schema_errors,
        'no_truncated_generation': not truncated,
        # A startup block with no request ID still contaminates this scenario.
        'no_scenario_policy_blocks': not evidence['blocked'],
        'not_known_failure_reply': not isinstance(reply, str) or (reply.strip() not in KNOWN_FAILURE_REPLIES and '系統忙碌中' not in reply and '目前對話服務無法使用' not in reply),
    }
    treatment=evidence.get('treatment') or {}
    if treatment.get('require_endpoint_proof'):
        from provider_strategy import endpoint_proof
        endpoint=endpoint_proof(evidence['model_calls'],evidence['dependencies'],treatment['provider_port'],
            correlation=treatment.get('endpoint_correlation'),ingress=evidence.get('proxy_ingress',[]))
        checks['selected_model_endpoint_observed']=bool(models) and endpoint['pass']
        checks['gateway_provider_label_matches']=bool(models) and all(m.get('provider_port')==treatment['provider_port'] for m in models)
    legacy_calls=[m for m in models if m.get('legacy_contract_requested')]
    if legacy_calls:
        checks['legacy_input_echo_and_schema_verified']=all(
            m.get('legacy_bound_schema_validation')=='pass' for m in legacy_calls)
    if treatment.get('schema_mode') in {'legacy_explicit_json','legacy_schema_fields'}:
        structured=[m for m in models if m.get('schema_mode') not in {'none',None}
                    or m.get('output_format_type') in {'json_schema','json_object'}]
        if treatment.get('schema_mode')=='legacy_schema_fields':
            from legacy_schema_fields import VERSION as expected_legacy_version, SERIALIZATION_PROFILE
            serialization_ok=lambda m: m.get('legacy_serialization_profile')==SERIALIZATION_PROFILE
        else:
            expected_legacy_version='legacy_user_contract_v1'
            serialization_ok=lambda m: True
        checks['legacy_explicit_contract_applied']=all(
            m.get('schema_mode')==treatment.get('schema_mode') and
            m.get('legacy_contract_requested') is True and
            m.get('legacy_contract_version')==expected_legacy_version and
            serialization_ok(m) and
            m.get('classification_labels_pinned') is False and
            m.get('response_repaired') is False for m in structured)
    if treatment.get('intent_output_token_floor') is not None:
        from generation_budget import call_budget_verified
        checks['intent_output_budget_treatment_verified']=bool(models) and all(
            call_budget_verified(m,treatment['intent_output_token_floor']) for m in models)
    if native_requested:
        checks['native_schema_reached_selected_provider']=native_proof['pass']
        checks['native_bound_schema_verified']=all(
            m.get('native_bound_schema_validation')=='pass'
            for m in models if m.get('native_schema_requested'))
    if row.get('auth_mode') in {'member','guest'}:
        checks['backend_resolved_same_identity']=any(
            r.get('request_id')==rid and r.get('auth_mode')==row['auth_mode'] and
            r.get('identity_fingerprint')==row.get('identity_fingerprint') and bool(row.get('identity_fingerprint'))
            for r in evidence.get('auth_resolution',[]))
        checks['identity_verified_before_chat']=row.get('session_verified') is True
        checks['correct_identity_cookie_attached']=(bool(row.get('member_cookie_attached')) and not row.get('guest_cookie_attached')) if row['auth_mode']=='member' else bool(row.get('guest_cookie_attached'))
        if row['auth_mode']=='member':
            checks['member_memory_stage_observed']=stage_ok('root','memory_begin_turn')
    return {
        'auth_mode':row.get('auth_mode','legacy_unspecified'),'account_alias':row.get('account_alias'),
        'http_classification':row.get('classification'),
        'request_id': rid, 'scenario': scenario, 'expected_pipeline': expected,
        'http_status': row.get('status'), 'body_ms': row.get('body_ms'),
        'reply_nonempty': bool(row.get('reply_nonempty')), 'checks': checks,
        'full_path_pass': all(checks.values()),
        'failure_reasons': [name for name, ok in checks.items() if not ok],
        'expected_stage_observed_success': terminal, 'stage_event_count': len(stages),
        'model_call_count': len(models), 'llm_http_attempts': len(deps),
        'llm_http_errors': len(model_errors) + len(dependency_errors),
        'schema_validation_failures': sum(m.get('schema_validation')=='fail' for m in models),
        'format_validation_unverified': sum(m.get('schema_mode') in {'prompt_only','vllm_native_raw_query','vllm_native_json_schema','legacy_explicit_json','legacy_schema_fields'} and m.get('schema_validation') not in {'pass','fail'} for m in models),
        'truncated_generations': len(truncated),
        'policy_block_count': len(evidence['blocked']),
        'scope': 'observed API/Agent/model wire path, not answer accuracy or safety acceptance',
    }


def smoke_row(data: dict) -> dict | None:
    rows = data.get('rows', [])
    index = data.get('valid_request_index', 2)
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(rows):
        return None
    return rows[index] if isinstance(rows[index], dict) else None


def assess_smoke(out: Path, scenario: str, backend_log: Path | None = None,
                 wait_seconds: float = 0) -> dict:
    source = out / 'smoke.json'
    data = json.loads(source.read_text()) if source.is_file() else {}
    row = smoke_row(data)
    if row is None:
        return {'pass': False, 'business_pass': False, 'transport_guard_pass': False,
                'failure_reasons': ['valid_smoke_request_missing'], 'path': None}
    deadline = time.monotonic() + wait_seconds
    while True:
        evidence = case_evidence(out, backend_log)
        done = any(s.get('request_id') == row.get('request_id') and s.get('status') == 200
                   for s in evidence['asgi'])
        treatment=evidence.get('treatment') or {}
        settled=True
        if treatment.get('endpoint_correlation'):
            from provider_strategy import endpoint_proof
            proof=endpoint_proof(evidence['model_calls'],evidence['dependencies'],treatment['provider_port'],
                correlation=treatment['endpoint_correlation'],ingress=evidence.get('proxy_ingress',[]))
            pending={'missing_gateway_call_id','proxy_ingress_missing','proxy_completion_missing',
                     'orphan_correlated_attempt','orphan_gateway_ingress','missing'}
            settled=bool(evidence['model_calls']) and not (set(proof['failure_reasons']) & pending)
        if (done and settled) or time.monotonic() >= deadline:
            break
        time.sleep(.05)
    reply_path = out / 'smoke-reply.txt'
    reply = reply_path.read_text() if reply_path.is_file() else None
    path = evaluate_request(row, scenario, evidence, reply)
    guard_pass = bool(data.get('checks')) and all(v is True for v in data['checks'].values())
    witness_path = out / 'local-route-witness.json'
    witnesses = json.loads(witness_path.read_text()) if witness_path.is_file() else []
    witness_pass = bool(witnesses) and all(w.get('status') == 200 and
                                         w.get('semantic_pass', w.get('has_output', False)) is True
                                         for w in witnesses)
    # Updated runs must include a live instructions+schema witness, not just OK.
    contract_witness = any(w.get('kind') == 'instructions_and_schema'
                           and w.get('semantic_pass') is True for w in witnesses)
    reasons = list(path['failure_reasons'])
    if not isinstance(reply,str) or not reply.strip(): reasons.append('smoke_reply_artifact_missing')
    if not guard_pass: reasons.append('transport_or_chatguard_checks_failed')
    if not witness_pass or not contract_witness: reasons.append('live_prompt_contract_not_verified')
    return {
        'pass': not reasons, 'business_pass': path['full_path_pass'],
        'transport_guard_pass': guard_pass, 'route_witness_pass': witness_pass,
        'live_prompt_contract_pass': contract_witness,
        'failure_reasons': reasons, 'path': path,
        'allowed_subprocess_count': len(evidence['allowed_subprocesses']),
        'stage_source': evidence['stage_source'],
    }


def save_smoke_assessment(out: Path, scenario: str, backend_log: Path | None = None,
                          wait_seconds: float = 0) -> dict:
    assessment = assess_smoke(out, scenario, backend_log, wait_seconds)
    atomic_json(out / 'smoke-assessment.json', assessment)
    return assessment


def assess_batch(out: Path, scenario: str, rows: list[dict], batch_dir: Path,
                 backend_log: Path | None = None, wait_seconds: float = 2) -> dict:
    """Bounded audit settlement only; never resend any chat/model request."""
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        evidence = case_evidence(out, backend_log)
        judged = []
        for row in rows:
            path = batch_dir / ('reply-vu-' + str(row['vu_index']) + '.txt')
            reply = path.read_text() if path.is_file() else None
            result = evaluate_request(row, scenario, evidence, reply)
            if reply is None:
                result['checks']['reply_artifact_present'] = False
                result['full_path_pass'] = False
                result['failure_reasons'].append('reply_artifact_missing')
            judged.append(result)
        passed = bool(judged) and all(r['full_path_pass'] for r in judged)
        if passed or time.monotonic() >= deadline:
            return {'pass': passed, 'requests': judged,
                    'scope': 'batch path review; no model retry or reply repair'}
        time.sleep(.05)

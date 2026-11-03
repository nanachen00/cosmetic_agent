"""Content-free classification of observed gates; not model-output repair."""
from __future__ import annotations
import json
import re
from pathlib import Path
from provider_strategy import endpoint_proof
from smoke_checks import case_evidence

EVIDENCE_CATEGORIES = {'ENDPOINT_EVIDENCE_INCOMPLETE', 'EVIDENCE_ARTIFACT_MISSING',
                       'UNCLASSIFIED_GATE_FAILURE', 'ROUTE_DESTINATION_MISMATCH'}


def load(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).is_file() else ({} if default is None else default)


def classify(reasons, *, endpoint=None, artifact_present=True):
    reasons = set(reasons)
    categories = set()
    if not artifact_present: categories.add('EVIDENCE_ARTIFACT_MISSING')
    if endpoint is not None and not endpoint.get('pass'):
        categories.add('ROUTE_DESTINATION_MISMATCH' if endpoint.get('wrong_destination')
                       else 'ENDPOINT_EVIDENCE_INCOMPLETE')
    if reasons & {'selected_model_endpoint_observed', 'gateway_provider_label_matches'}:
        categories.add('ENDPOINT_EVIDENCE_INCOMPLETE')
    if reasons & {'schema_verified_when_requested', 'native_bound_schema_verified',
                  'legacy_input_echo_and_schema_verified'}:
        categories.add('MODEL_SCHEMA_CONTRACT_FAILURE')
    if reasons & {'legacy_explicit_contract_applied','intent_output_budget_treatment_verified'}:
        categories.add('ENDPOINT_EVIDENCE_INCOMPLETE')
    if 'native_schema_reached_selected_provider' in reasons:
        categories.add('ENDPOINT_EVIDENCE_INCOMPLETE')
    if 'live_prompt_contract_not_verified' in reasons:
        categories.add('SYNTHETIC_CONTRACT_FAILURE')
    if reasons & {'backend_resolved_same_identity', 'identity_verified_before_chat',
                  'correct_identity_cookie_attached', 'transport_or_chatguard_checks_failed'}:
        categories.add('AUTH_OR_CHATGUARD_FAILURE')
    if 'no_scenario_policy_blocks' in reasons: categories.add('EGRESS_OR_FILE_POLICY_FAILURE')
    if reasons & {'no_llm_http_errors', 'http_200_nonempty', 'asgi_complete_200'}:
        categories.add('HTTP_OR_MODEL_TRANSPORT_FAILURE')
    if 'no_truncated_generation' in reasons: categories.add('TRUNCATED_MODEL_GENERATION')
    if reasons & {'valid_smoke_request_missing', 'smoke_reply_artifact_missing',
                  'reply_artifact_missing', 'reply_artifact_present'}:
        categories.add('EVIDENCE_ARTIFACT_MISSING')
    if reasons & {'expected_terminal_stage', 'root_dispatch_observed', 'response_dispatch_stage',
                  'expected_response_route', 'no_stage_errors', 'not_known_failure_reply',
                  'member_memory_stage_observed', 'model_call_observed'}:
        categories.add('APPLICATION_PATH_FAILURE')
    if 'request_id_match' in reasons: categories.add('ENDPOINT_EVIDENCE_INCOMPLETE')
    if reasons and not categories: categories.add('UNCLASSIFIED_GATE_FAILURE')
    return sorted(categories)


def case_diagnostic(cell, *, evidence=None):
    assessment = load(cell/'smoke-assessment.json')
    ev = evidence if evidence is not None else case_evidence(cell)
    treatment = ev.get('treatment') or {}
    proof = endpoint_proof(ev['model_calls'], ev['dependencies'], treatment.get('provider_port'),
                          correlation=treatment.get('endpoint_correlation'),
                          ingress=ev.get('proxy_ingress', []))
    for call in proof.get('calls', []):
        rid=call.get('request_id')
        if not isinstance(rid,str) or not re.fullmatch(r'(?:shadow-|witness-)[a-f0-9]{32}',rid):
            call['request_id']=None
    reasons = list(assessment.get('failure_reasons', []))
    batch_reasons = []
    for path in cell.glob('repetition-*/vu-*/path-assessment.json'):
        for row in load(path).get('requests', []):
            batch_reasons.extend(row.get('failure_reasons', []))
    categories = classify(reasons + batch_reasons, endpoint=proof, artifact_present=bool(assessment))
    witnesses = load(cell/'local-route-witness.json', [])
    return {'smoke_pass': assessment.get('pass') is True,
            'failure_reasons': sorted(set(reasons + batch_reasons)), 'categories': categories,
            'endpoint': proof,
            'witnesses': [{'kind': w.get('kind') if w.get('kind') in {'chat_text','responses_text','instructions_and_schema'} else 'UNKNOWN',
                           'http_status': w.get('status') if type(w.get('status')) is int else None,
                           'semantic_pass': w.get('semantic_pass') is True,
                           'has_output': w.get('has_output') is True} for w in witnesses],
            'evidence_complete': bool(assessment) and bool(witnesses) and not (set(categories) & EVIDENCE_CATEGORIES)}


def phase_diagnostic(run, provider, summary):
    cases = []
    for cell in sorted((run/f'provider-{provider}').glob('*')):
        if cell.is_dir() and (cell/'treatment.json').is_file():
            cases.append({'scenario': cell.name, **case_diagnostic(cell)})
    categories = {k for cell in cases for k in cell['categories']}
    if summary.get('schema_validation_failures') or summary.get('native_bound_schema_errors'):
        categories.add('MODEL_SCHEMA_CONTRACT_FAILURE')
    if summary.get('format_validation_unverified'): categories.add('EVIDENCE_ARTIFACT_MISSING')
    if summary.get('model_http_errors') or summary.get('backend_llm_http_errors'):
        categories.add('HTTP_OR_MODEL_TRANSPORT_FAILURE')
    if summary.get('truncated_generations'): categories.add('TRUNCATED_MODEL_GENERATION')
    if summary.get('blocked_attempts'): categories.add('EGRESS_OR_FILE_POLICY_FAILURE')
    if summary.get('auth_cleanup_pass') is False: categories.add('AUTH_CLEANUP_FAILURE')
    if not summary.get('benchmark_pass') and not categories:
        categories.add('UNCLASSIFIED_GATE_FAILURE')
    return {'category': 'PASS' if not categories else sorted(categories)[0],
            'categories': sorted(categories), 'cases': cases,
            'evidence_complete': bool(cases) and all(c['evidence_complete'] for c in cases)
                                 and not (categories & EVIDENCE_CATEGORIES)}

"""Offline, content-free A/B report from existing harness evidence.

Never reads accounts/.state/process environments and never exports raw replies or
application logs. Raw reply text is used in-memory only by the existing strict
path validator. No new answer grading, inferred retries, or engine-speed claim.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import html
import json
import math
from pathlib import Path
import re
import tarfile
import sys

# The CLI is intentionally run with -I; load only this trusted tool directory.
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import Refuse, atomic_json, read_json, stats, sha_file, utc
from provider_strategy import COMPARISON_MODE, strategy, endpoint_proof
from generation_budget import public_budget
from smoke_checks import case_evidence, evaluate_request, json_events, smoke_row
from failure_diagnostics import case_diagnostic, EVIDENCE_CATEGORIES

FILES=('report-data.json','batch-comparison.csv','request-comparison.csv','EVIDENCE.md','validation.json')
HTML_FILE='BLINK_Shadow_8000_vs_8200_Report.html'
SCENARIOS={'product_search','skin_consult','auto_root'}
METRICS=('cpu_host_pct','ram_host_used_gib','gpu_util_pct','gpu_memory_util_pct',
         'vram_used_gib_approx','gpu_power_w','gpu_temp_c','gpu_clock_mhz')


def number(value):
    return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


def label(value, default='UNKNOWN'):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}',value) else default


def request_id(value):
    if isinstance(value,str) and re.fullmatch(r'(?:shadow-|witness-)[a-f0-9]{32}',value):return value
    return 'redacted-'+hashlib.sha256(str(value).encode()).hexdigest()[:20] if value else None


def timestamp(value):
    return value if isinstance(value,str) and re.fullmatch(r'[0-9T:+.Z-]{19,40}',value) else None


def load(path, default=None):
    if path.is_symlink():raise Refuse('Report input must not be symlinked')
    return read_json(path) if path.is_file() else ({} if default is None else default)


def write_csv(path, rows):
    with path.open('w',newline='',encoding='utf-8') as stream:
        if not rows:return
        fields=list(dict.fromkeys(k for r in rows for k in r))
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for row in rows:
            writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})


def dependency_kind(row):
    host=str(row.get('host','')).lower()
    if host=='api.getzep.com' or host=='api.zep.ai' or host.endswith('.getzep.com'):return 'zep'
    if host.endswith('.neo4j.io'):return 'neo4j_http'
    if row.get('port')==8002:return 'embedding'
    return 'other_dependency_http'



def safe_contract_diagnostic(value):
    if not isinstance(value,dict): return None
    out={'status':label(value.get('status')), 'layout':label(value.get('layout')),
         'raw_reply_rewritten':value.get('raw_reply_rewritten') is True,
         'whole_markdown_fence':value.get('whole_markdown_fence') is True}
    reached=value.get('reported_completion_reached_requested_limit')
    out['reported_completion_reached_requested_limit']=reached if isinstance(reached,bool) else None
    signal=value.get('completion_limit_signal')
    out['completion_limit_signal']=signal if isinstance(signal,str) and signal in {'provider_reported_length','stop_reported_at_or_above_limit','below_requested_limit'} else 'UNKNOWN'
    shape=value.get('lexical_summary')
    if isinstance(shape,dict):
        out['lexical_summary']={**{k:number(shape.get(k)) for k in ('unclosed_containers','mismatched_closers','unicode_escape_count','incomplete_unicode_escape_count','invalid_unicode_escape_count')},
            **{k:shape.get(k) if type(shape.get(k)) is bool else None for k in ('unterminated_string','trailing_backslash')},'raw_values_exported':False,'scope':'lexical_observation_not_truncation_proof'}
    for key in ('raw','fence_payload_diagnostic'):
        row=value.get(key)
        if not isinstance(row,dict):continue
        out[key]={k:label(row.get(k)) for k in ('json_parse','schema_validation','root_type','validation_keyword','instance_type') if k in row}
        for path_key in ('instance_path','schema_path'):
            parts=row.get(path_key)
            if isinstance(parts,list):
                out[key][path_key]=[x if type(x) is int and 0<=x<=1000000 else
                    x if isinstance(x,str) and (x=='<field>' or re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$-]{0,63}',x)) else '<field>'
                    for x in parts[:24]]
        err=row.get('parse_error')
        if isinstance(err,dict):
            out[key]['parse_error']={'code':label(err.get('code')),
                **{k:number(err.get(k)) for k in ('offset','line','column')},
                'at_end_of_output':err.get('at_end_of_output') if isinstance(err.get('at_end_of_output'),bool) else None,
                'character_class':err.get('character_class') if isinstance(err.get('character_class'),str) and err.get('character_class') in {
                    'end_of_output','object_open','object_close','array_open','array_close','comma','colon',
                    'double_quote','single_quote','backslash','whitespace','digit','other'} else 'UNKNOWN'}
        enum=row.get('enum_mismatch')
        if isinstance(enum,dict):
            allowed_types=enum.get('allowed_types',[])
            out[key]['enum_mismatch']={'allowed_value_count':number(enum.get('allowed_value_count')),
                'allowed_types':[v for v in allowed_types if isinstance(v,str) and v in {'null','boolean','string','object','array','number','other'}] if isinstance(allowed_types,list) else [],
                **{k:enum.get(k) is True for k in ('value_is_empty_string','matches_after_strip','matches_after_casefold')},
                'values_exported':False,'value_rewritten':False}
    return out

def public_request(row, ev, reply, provider, scenario, phase, vu, rep, source):
    judged=evaluate_request(row,scenario,ev,reply)
    if reply is None:
        judged['full_path_pass']=False
        judged['failure_reasons'].append('reply_artifact_missing')
    if ev.get('_report_treatment_verified') is False:
        judged['full_path_pass']=False
        judged['checks']['treatment_metadata_verified']=False
        judged['failure_reasons'].append('treatment_metadata_unverified')
    rid=row.get('request_id')
    models=[r for r in ev['model_calls'] if rid and r.get('request_id')==rid]
    deps=[r for r in ev['dependencies'] if rid and r.get('request_id')==rid]
    backend=[r for r in deps if r.get('role')=='backend' and r.get('model_http')]
    external=[r for r in deps if not r.get('model_http')]
    stages=[r for r in ev['stages'] if rid and r.get('request_id')==rid]
    usage=[m.get('usage') or {} for m in models]
    token_sum=lambda key: sum(u[key] for u in usage) if usage and all(number(u.get(key)) is not None for u in usage) else None
    errors=lambda rows: sum(r.get('status')!=200 or bool(r.get('error')) for r in rows)
    timeout=lambda r: 'timeout' in str(r.get('error','')).lower() or r.get('status')==504
    return {'provider':provider,'contract_mode':ev['treatment']['contract_mode'],
        'scenario':scenario,'phase':phase,'vu':vu,'repetition':rep,'request_id':request_id(rid),
        'account_alias':row.get('account_alias') if re.fullmatch(r'A[0-9]{2,3}',str(row.get('account_alias',''))) else None,
        'http_status':number(row.get('status')),'http_200_nonempty':bool(judged['checks']['http_200_nonempty']),
        'body_ms':number(row.get('body_ms')),'full_path_pass':judged['full_path_pass'],
        'checks':{label(k):v is True for k,v in judged['checks'].items()},
        'failure_reasons':[label(k) for k in judged['failure_reasons']],
        'schema_failure_count':judged['schema_validation_failures'],
        'schema_unverified_count':judged['format_validation_unverified'],
        'schema_pass_when_requested':judged['checks']['schema_verified_when_requested'],
        'fallback_or_clarification_observed':row.get('stage') not in ('dispatch',None),
        'fallback_category':label(row.get('stage')) if row.get('stage')!='dispatch' else None,
        'model_calls':len(models),'backend_model_http_attempts':len(backend),
        'backend_model_http_errors':errors(backend),'gateway_model_http_errors':errors(models),
        'backend_timeout_attempts':sum(timeout(r) for r in backend),
        'gateway_timeout_calls':sum(timeout(r) for r in models),'client_timeout':timeout(row),
        'retry_count':None,'retry_count_status':'NOT_EXPLICITLY_INSTRUMENTED',
        'prompt_tokens':token_sum('prompt_tokens'),'completion_tokens':token_sum('completion_tokens'),
        'memory_persistence_pass':None,
        'dependency_http_count':len(external),'zep_429_count':sum(dependency_kind(r)=='zep' and r.get('status')==429 for r in external),
        'stage_timeline':[{'pipeline':label(r.get('pipeline')),'stage':label(r.get('stage')),
             'event':label(r.get('event')),'status':label(r.get('status')),
             'timestamp':timestamp(r.get('timestamp')),'duration_ms':number(r.get('duration_ms'))} for r in stages],
        'model_timeline':[{'status':number(r.get('status')),'duration_ms':number(r.get('duration_ms')),
             'provider_port':number(r.get('provider_port')),'timestamp':timestamp(r.get('timestamp')),
             'error':label(r.get('error')) if r.get('error') else None,
             'schema_mode':label(r.get('schema_mode')),'schema_validation':label(r.get('schema_validation')),
             'finish_reason':label(r.get('finish_reason')),
             'output_chars':number(r.get('output_chars')),
             'requested_max_tokens':number(r.get('requested_max_tokens')),
             'effective_max_tokens':number(r.get('effective_max_tokens')),
             'generation_budget':public_budget(r.get('generation_budget')),
             'prompt_tokens':number((r.get('usage') or {}).get('prompt_tokens')),
             'completion_tokens':number((r.get('usage') or {}).get('completion_tokens')),
             'gateway_call_id':r.get('gateway_call_id') if re.fullmatch(r'call-[a-f0-9]{32}',str(r.get('gateway_call_id',''))) else None,
             'validation_error_kind':label(r.get('validation_error_kind')),
             'validation_keyword':label(r.get('validation_keyword')),
             'legacy_contract_version':label(r.get('legacy_contract_version')) if r.get('legacy_contract_requested') else None,
             'legacy_serialization_profile':label(r.get('legacy_serialization_profile')) if r.get('legacy_contract_requested') else None,
             'legacy_bound_schema_validation':label(r.get('legacy_bound_schema_validation')) if r.get('legacy_contract_requested') else None,
             'legacy_raw_query_bound':r.get('legacy_raw_query_bound') is True,
             'input_adaptation_only':r.get('input_adaptation_only') is True,
             'classification_labels_pinned':r.get('classification_labels_pinned') is True,
             'response_repaired':r.get('response_repaired') is True,
             'contract_diagnostic':safe_contract_diagnostic(r.get('output_contract_diagnostics'))} for r in models],
        'dependency_summary':[{'kind':dependency_kind(r),'status':number(r.get('status')),
             'duration_ms':number(r.get('duration_ms')),'error':label(r.get('error')) if r.get('error') else None} for r in external],
        'evidence_source':source}


def build_report(run: Path, *, include_html: bool = False) -> dict:
    run=run.resolve(strict=True)
    cfg=load(run/'run-config.json')
    if cfg.get('schema_mode')!=COMPARISON_MODE or cfg.get('providers')!=[8000,8200]:
        raise Refuse('This report accepts one provider-specific A/B run, not historical merged runs')
    if cfg.get('vus')!=[1,4,8] or cfg.get('repetitions')!=1:
        raise Refuse('Report expects the first A/B workload VU1,4,8 / repetitions=1')
    # This walks only the public run tree, never the harness .state/.private trees.
    if any(p.is_symlink() for p in run.rglob('*')):raise Refuse('Report input contains a symlink')
    snapshot=cfg.get('snapshot_id')
    issues=[]
    if not isinstance(snapshot,str) or not re.fullmatch(r'[a-f0-9]{64}',snapshot):
        issues.append('SNAPSHOT_ID_MISSING_OR_INVALID');snapshot=None
    manifest=load(run/'snapshot-manifest.json')
    if manifest.get('snapshot_id')!=snapshot or snapshot is None:issues.append('SNAPSHOT_MANIFEST_UNVERIFIED')
    gates=load(run/'comparison-gates.json')
    cleanup=load(run/'owned-cleanup-proof.json')
    exit_status=load(run/'exit-status.json')
    if gates.get('snapshot_id')!=snapshot:issues.append('GATES_SNAPSHOT_MISMATCH')
    treatments={r.get('provider_port'):r for r in gates.get('providers',[]) if isinstance(r,dict)}
    requests=[];batches=[];endpoint_cells=[];case_diagnostics=[]
    scenarios=[s['name'] for s in cfg.get('scenarios',[]) if s.get('name') in SCENARIOS]
    if len(scenarios)!=3 or len(set(scenarios))!=3:issues.append('SCENARIO_PLAN_INCOMPLETE')
    for p in cfg['providers']:
        for sc in scenarios:
            for phase in ('smoke','formal'):
                cell=run/phase/f'provider-{p}'/sc
                if not cell.is_dir():continue
                treatment=load(cell/'treatment.json')
                expected=strategy(cfg,p)
                treatment_verified=not any(treatment.get(k)!=expected[k] for k in expected) and treatment.get('snapshot_id')==snapshot
                if not treatment_verified:
                    issues.append(f'TREATMENT_OR_SNAPSHOT_MISMATCH_{p}_{sc}_{phase}')
                ev=case_evidence(cell)
                ev['_report_treatment_verified']=treatment_verified
                # Do not invent absent endpoint requirements to make older results pass.
                if not ev.get('treatment'):ev['treatment']={'contract_mode':'UNKNOWN'}
                ep=endpoint_proof(ev['model_calls'],ev['dependencies'],p,
                    correlation=ev['treatment'].get('endpoint_correlation'),ingress=ev.get('proxy_ingress',[]))
                for call in ep.get('calls',[]):call['request_id']=request_id(call.get('request_id'))
                endpoint_cells.append({'provider':p,'scenario':sc,'phase':phase,**ep})
                case_diagnostics.append({'provider':p,'scenario':sc,'phase':phase,
                                         **case_diagnostic(cell,evidence=ev)})
                smoke=smoke_row(load(cell/'smoke.json'))
                if smoke:
                    text=(cell/'smoke-reply.txt').read_text() if (cell/'smoke-reply.txt').is_file() else None
                    # Formal setup Smoke is separate, never counted among the 39 requests.
                    requests.append(public_request(smoke,ev,text,p,sc,'smoke' if phase=='smoke' else 'formal_setup_smoke',1,0,
                        f'{phase}/provider-{p}/{sc}/smoke.json'))
                if phase!='formal':continue
                for vu in cfg['vus']:
                    bd=cell/'repetition-1'/f'vu-{vu}'
                    rows=load(bd/'client-results.json',[])
                    for index,row in enumerate(rows):
                        if type(row.get('vu_index')) is not int or not 1<=row['vu_index']<=vu:
                            raise Refuse('Invalid client VU index; no arbitrary reply path read')
                        if row.get('snapshot_id')!=snapshot:issues.append(f'REQUEST_SNAPSHOT_MISMATCH_{p}_{sc}_{vu}')
                        reply_file=bd/f'reply-vu-{row["vu_index"]}.txt'
                        text=reply_file.read_text() if reply_file.is_file() else None
                        requests.append(public_request(row,ev,text,p,sc,'formal',vu,1,
                            f'formal/provider-{p}/{sc}/repetition-1/vu-{vu}/client-results.json#{index}'))
    for p in cfg['providers']:
        for sc in scenarios:
            for vu in cfg['vus']:
                bd=run/'formal'/f'provider-{p}'/sc/'repetition-1'/f'vu-{vu}'
                summary=load(bd/'summary.json')
                subset=[r for r in requests if r['phase']=='formal' and r['provider']==p and r['scenario']==sc and r['vu']==vu]
                good=[r for r in subset if r['full_path_pass']]
                wall=number(summary.get('wall_seconds'))
                if wall is not None and wall<=0:wall=None
                active=[r for r in json_events(bd/'telemetry/telemetry.jsonl') if r.get('phase')=='active'] if bd.is_dir() else []
                tel=summary.get('telemetry',{})
                telemetry={key:stats(number(r.get(key)) for r in active) if active else
                           tel.get('stats',{}).get(key,stats([])) for key in METRICS}
                # Missing data stays null, not zero or an invented latency.
                row={'provider':p,'contract_mode':strategy(cfg,p)['contract_mode'],'scenario':sc,'vu':vu,'repetition':1,
                     'planned':vu,'completed':len(subset),'http_200_nonempty':sum(r['http_200_nonempty'] for r in subset),
                     'full_path_pass':len(good),'all_body_ms':stats(r['body_ms'] for r in subset),
                     'full_path_body_ms':stats(r['body_ms'] for r in good),
                     'batch_wall_seconds':wall,'http_rps':sum(r['http_200_nonempty'] for r in subset)/wall if wall else None,
                     'full_path_goodput':len(good)/wall if wall else None,
                     'model_calls':sum(r['model_calls'] for r in subset),
                     'model_calls_per_second':sum(r['model_calls'] for r in subset)/wall if wall else None,
                     'completion_tokens_per_second':sum(r['completion_tokens'] for r in subset)/wall if wall and subset and all(r['completion_tokens'] is not None for r in subset) else None,
                     'backend_timeout_attempts':sum(r['backend_timeout_attempts'] for r in subset),
                     'schema_failure_count':sum(r['schema_failure_count'] for r in subset),
                     'zep_429_count':sum(r['zep_429_count'] for r in subset),'telemetry':telemetry,
                     'telemetry_status':label(tel.get('status'),'NOT_MEASURED'),
                     'started_at':timestamp(summary.get('started_at')),'finished_at':timestamp(summary.get('finished_at')),
                     'performance_eligible':summary.get('deployment_window',{}).get('performance_eligible') is True,
                     'deployment_status':label(summary.get('deployment_window',{}).get('status'),'NOT_OBSERVED'),
                     'status':'NOT_ATTEMPTED' if not subset else 'PASS' if len(good)==vu else 'FAIL_OR_INCOMPLETE'}
                batches.append(row)
                if subset and (not summary or summary.get('completed')!=len(subset) or len(subset)>vu):
                    issues.append(f'BATCH_COUNT_INCONSISTENT_{p}_{sc}_{vu}')
    formal=[r for r in requests if r['phase']=='formal']
    identities=[r['request_id'] for r in formal]
    if any(i is None for i in identities) or len(identities)!=len(set(identities)):issues.append('REQUEST_IDS_MISSING_OR_DUPLICATE')
    providers=[]
    for p in cfg['providers']:
        rows=[r for r in formal if r['provider']==p];good=[r for r in rows if r['full_path_pass']]
        measured=[b for b in batches if b['provider']==p and b['batch_wall_seconds']]
        wall=sum(b['batch_wall_seconds'] for b in measured)
        smoke_rows=[r for r in requests if r['phase']=='smoke' and r['provider']==p]
        case_rows=[c for c in case_diagnostics if c['provider']==p]
        categories=sorted({k for c in case_rows for k in c['categories']} |
                          {k for ph in ('smoke','formal') for k in (treatments.get(p,{}).get(ph) or {}).get('categories',[])})
        providers.append({'provider':p,'contract_mode':strategy(cfg,p)['contract_mode'],
            'planned':len(scenarios)*sum(cfg['vus']),'completed':len(rows),'full_path_pass':len(good),
            'status':label(treatments.get(p,{}).get('status'),'NOT_ATTEMPTED'),
            'full_path_body_ms':stats(r['body_ms'] for r in good),'all_body_ms':stats(r['body_ms'] for r in rows),
            'measured_batch_goodput':len(good)/wall if wall else None,
            'backend_timeout_attempts':sum(r['backend_timeout_attempts'] for r in rows),
            'schema_failure_count':sum(r['schema_failure_count'] for r in rows),
            'smoke_pass':(treatments.get(p,{}).get('smoke') or {}).get('functional_pass',False),
            'gate_category':categories[0] if categories else ('PASS' if (treatments.get(p,{}).get('smoke') or {}).get('functional_pass') is True else 'UNCLASSIFIED_GATE_FAILURE'),
            'gate_categories':categories,
            'smoke_completed':len(smoke_rows),'smoke_full_path_pass':sum(r['full_path_pass'] for r in smoke_rows),
            'smoke_schema_failure_count':sum(r['schema_failure_count'] for r in smoke_rows),
            'formal_schema_failure_count':sum(r['schema_failure_count'] for r in rows),
            'unattributed_proxy_attempts':sum(c['endpoint'].get('unattributed_proxy_attempts',0) for c in case_rows)})
    complete_status={'SMOKE_GATE_FAILED','FORMAL_GATE_FAILED','PROVIDER_GATE_FAILED','FORMAL_PASS'}
    for provider in providers:
        p=provider['provider'];state=treatments.get(p,{}).get('status')
        if state in complete_status:
            phase='formal' if state in {'FORMAL_PASS','FORMAL_GATE_FAILED'} else 'smoke'
            # A textual gate label alone is not proof of a measured/reproducible stop.
            proof=load(run/phase/f'provider-{p}-gate.json')
            if not proof or proof.get('snapshot_id')!=snapshot:
                issues.append(f'PROVIDER_GATE_EVIDENCE_MISSING_{p}')
            if state=='FORMAL_PASS' and provider['full_path_pass']!=provider['planned']:
                issues.append(f'FORMAL_PASS_COUNT_CONTRADICTION_{p}')
    for cell in case_diagnostics:
        if not cell['endpoint'].get('pass'):
            issues.append('ENDPOINT_EVIDENCE_INCOMPLETE_'+str(cell['provider'])+'_'+cell['scenario']+'_'+cell['phase'])
    for p in providers:
        state=treatments.get(p['provider'],{}).get('status')
        if state in complete_status and state!='PROVIDER_GATE_FAILED':
            if not [c for c in case_diagnostics if c['provider']==p['provider']]:
                issues.append('CASE_EVIDENCE_MISSING_'+str(p['provider']))
            if any(c['evidence_complete'] is not True for c in case_diagnostics if c['provider']==p['provider']):
                issues.append('GATE_FAILURE_NOT_FULLY_EVIDENCED_'+str(p['provider']))
        if state=='PROVIDER_GATE_FAILED':
            proof=treatments.get(p['provider'],{}).get('smoke') or {}
            if proof.get('category') not in {'PROVIDER_UNAVAILABLE','PROVIDER_WITNESS_TRANSPORT_FAILED'}:
                issues.append('UNCLASSIFIED_PROVIDER_FAILURE_'+str(p['provider']))
    evidence_complete=(gates.get('shared_auth',{}).get('pass') is True and cleanup.get('pass') is True
        and not issues and all(treatments.get(p,{}).get('status') in complete_status for p in cfg['providers']))
    functional_pass=evidence_complete and bool(formal) and all(p['completed']==p['planned']==p['full_path_pass'] and p['smoke_pass'] for p in providers)
    validation={'report_inputs_consistent':not issues,'issues':sorted(set(issues)),
        'experiment_evidence_complete':evidence_complete,'functional_all_planned_pass':functional_pass,
        'cleanup_pass':cleanup.get('pass') is True,'exit_code':number(exit_status.get('exit_code')),
        'same_snapshot_verified':snapshot is not None and not any('SNAPSHOT' in i for i in issues),
        'private_state_removed':exit_status.get('private_state_removed') is True,
        'source':'LOCAL_SYNTHETIC' if cfg.get('synthetic_test_fixture') is True else 'SERVER_RUN_ARTIFACTS',
        'live_execution_verified_by_report_generator':False}
    data={'title':'BLINK Shadow :8000 vs :8200','generated_at':utc(),'snapshot_id':snapshot,
          'status':('COMPARISON_INCOMPLETE_EVIDENCE' if not evidence_complete and str(gates.get('status','')).startswith('COMPARISON_COMPLETED') else label(gates.get('status'),'INCOMPLETE')),'validation':validation,'providers':providers,
          'batches':batches,'requests':requests,'endpoint_proofs':endpoint_cells,'case_diagnostics':case_diagnostics,
          'harness_patch':'shadow-ab-intent-output-budget',
          'output_mode':'data_only',
          'input_treatments':[strategy(cfg,p) for p in cfg['providers']],
          'caveats':[
              '這是實際整合方案比較，不是純推論引擎效能比較；schema 與 template 是 treatment 差異。',
              'Formal 每組規劃 39 筆。Smoke、guest control、synthetic witness 與 Formal setup Smoke 均不計入。',
              '未執行的 batch 延遲及 goodput 為 null；執行過但零成功的 batch goodput 才是 0。',
              'repetitions=1；VU1 只有 1 筆，p95 等於該筆值，不能推論穩定尾端延遲或統計顯著性。',
              'A01–A08 長期記憶與外部依賴未重設；順序執行存在記憶、時間與 shared GPU 背景干擾。',
              '全機 CPU／全 GPU 指標不可歸為該 provider 專屬；GPU MEM util 不是 VRAM 使用百分比。',
              '延遲為完整 HTTP body，不是瀏覽器首字時間；僅成功樣本與所有完成樣本分開呈現。',
              'Stage 可能包含父子重疊，不可相加視為總延遲。Zep HTTP 429 不等於已證明記憶未寫入。',
              'full_path_pass 不代表答案品質或遠端記憶持久化；memory_persistence_pass 保留 null。',
              '新測量逐次驗證 gateway call ID、proxy ASGI 入口與實際送出內容指紋；不以另一筆相同內容的呼叫代替缺失證據。',
              '未歸屬 gateway 的 proxy 呼叫另列，不稱作會員 retry 或確定的 health check；背景來源未確定，不歸因為純模型延遲。',
              '未有明確 retry instrumentation，retry_count 顯示未知；不同層的 timeout 不相加冒充獨立失敗。',
              'Neo4j 原生 Bolt 活動不在 HTTP audit 內；HTTP 表格不是全部外部依賴活動。',
              '此分享包只包含去識別摘要。原始回覆、Profile、cookie、環境、backend log 不會打包。']}
    # A/B outcome completeness is not the same as every treatment passing.
    # Per the experiment contract, a fully evidenced provider gate failure is a
    # valid terminal treatment outcome. Performance comparison remains stricter.
    outcome_ready=(evidence_complete and validation['same_snapshot_verified'] and
                   cleanup.get('pass') is True and exit_status.get('private_state_removed') is True and
                   exit_status.get('exit_code') in {0,2})
    performance_ready=(functional_pass and exit_status.get('exit_code')==0 and
                       exit_status.get('private_state_removed') is True and
                       all(b.get('performance_eligible') is True for b in batches))
    validation['full_comparison_ready']=outcome_ready
    validation['full_performance_comparison_ready']=performance_ready
    validation['interactive_html_generated']=False
    if include_html and not outcome_ready:
        raise Refuse('Interactive HTML requires complete treatment outcomes and clean evidence; no HTML generated')
    if include_html:
        validation['interactive_html_generated']=True
        data['output_mode']='html_after_complete_comparison'
    atomic_json(run/'report-data.json',data)
    write_csv(run/'batch-comparison.csv',batches)
    write_csv(run/'request-comparison.csv',[{k:v for k,v in r.items() if k not in {'stage_timeline','model_timeline','dependency_summary'}} for r in requests])
    atomic_json(run/'validation.json',validation)
    (run/'EVIDENCE.md').write_text('# BLINK Shadow A/B 證據索引\n\n'
        '本報告只讀取同一個 run，不匯入歷史 :8000／:8200 數字。\n\n'
        '`report-data.json`：provider／batch／request／stage／endpoint 摘要。\n\n'
        '`request-comparison.csv` 的 `evidence_source` 指向 server 本輪相對路徑；原始檔不隨分享包匯出。\n\n'
        '`validation.json` 分開列出資料一致性、實驗證據完整性、所有 Formal 功能通過、cleanup。\n\n'
        '失敗 provider 的 latency 不填零；未進 Formal 不能推算其成功率或速度比。\n\n'
        '這些數值是 artifact 讀回結果，不表示報告產生器有連線驗證 server。\n',encoding='utf-8')
    if include_html:
        (run/HTML_FILE).write_text(render(data),encoding='utf-8')
    return data


def pack_comparison(run: Path):
    if load(run/'owned-cleanup-proof.json').get('pass') is not True:
        raise Refuse('Successful owned cleanup required before A/B share export')
    if any(not (run/name).is_file() or (run/name).is_symlink() for name in FILES):
        raise Refuse('A/B report allowlist is incomplete')
    out=run.parent.parent/(run.name+'-ab-results.tar.gz')
    if out.exists():
        print('[ARCHIVE EXISTS]',out,'(not overwritten)',flush=True);return out
    with tarfile.open(out,'w:gz') as archive:
        for name in FILES:archive.add(run/name,arcname=run.name+'/'+name,recursive=False)
    Path(str(out)+'.sha256').write_text(sha_file(out)+'  '+out.name+'\n')
    print('[A/B SHARE ARCHIVE]',out,flush=True)
    return out


def render(data):
    payload=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')
    return HTML.replace('__DATA__',payload)


HTML=r'''<!doctype html>
<html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'">
<title>BLINK Shadow :8000 vs :8200</title>
<style>
:root{color-scheme:dark;--bg:#0d1420;--panel:#162131;--line:#304055;--text:#e8eef6;--muted:#aab9cb;--a:#f1b76d;--b:#76d8cc}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.65 system-ui,sans-serif}main{max-width:1280px;margin:auto;padding:36px 28px 80px}h1{font-size:36px;line-height:1.2;margin:6px 0 14px}h2{font-size:22px;margin:36px 0 14px}.eyebrow{letter-spacing:.18em;color:var(--muted);font-size:12px}.sub,small{color:var(--muted)}.panel,.card{min-width:0;overflow-wrap:anywhere;border:1px solid var(--line);background:var(--panel);padding:20px;border-radius:10px}.cards{display:grid;grid-template-columns:1fr 1fr;gap:18px}.number{font-size:35px;font-weight:700;line-height:1.4}.a{color:var(--a)}.b{color:var(--b)}.tag{font-size:12px;border:1px solid var(--line);padding:4px 8px;border-radius:5px;display:inline-block;margin:3px 6px 3px 0}.filters{display:flex;gap:12px;flex-wrap:wrap;padding:14px 0}select{background:var(--panel);color:var(--text);border:1px solid var(--line);padding:8px;border-radius:5px}label{font-size:13px}.chart{margin-bottom:20px}.barrow{display:grid;grid-template-columns:180px minmax(80px,1fr) 145px;gap:12px;align-items:center;min-height:32px}.track{height:16px;background:#243044;border-radius:3px}.bar{height:100%;background:var(--a);border-radius:3px}.bar.b{background:var(--b)}.barlabel{font-size:12px;color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}.scroll{overflow:auto}details{margin:8px 0;border:1px solid var(--line);border-radius:6px;padding:10px 14px}summary{cursor:pointer;overflow-wrap:anywhere}pre{white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,monospace}.warning{border-left:4px solid var(--a)}.mono{font-family:ui-monospace,monospace;overflow-wrap:anywhere;font-size:12px}.legend{display:flex;gap:20px}p{margin:8px 0 14px}@media(max-width:700px){main{padding:20px 14px}.cards{grid-template-columns:1fr}h1{font-size:28px}.barrow{grid-template-columns:110px 1fr 85px;font-size:11px}}
</style><main>
<div class="eyebrow">BLINK AI / SHADOW INTEGRATION COMPARISON</div><h1>:8000 與 :8200 全路徑對照</h1>
<p class="sub">Legacy prompt contract 與 vLLM native raw-query contract · 固定 snapshot · 固定會員 · VU 1／4／8</p>
<div id="status" class="panel warning"></div><div id="cards" class="cards" style="margin-top:20px"></div>
<h2>Smoke 與停止原因</h2><div id="diagnostics" class="panel"></div>
<h2>Formal batch 比較</h2><p class="sub">成功樣本延遲與所有樣本延遲分開。缺值為「未測得」，不是 0。滑鼠停在長條上可查看定義。</p>
<div class="legend"><span class="a">■ :8000</span><span class="b">■ :8200</span></div>
<div class="filters"><label>Scenario <select id="scenario"><option value="">全部</option><option>product_search</option><option>skin_consult</option><option>auto_root</option></select></label>
<label>VU <select id="vu"><option value="">全部</option><option>1</option><option>4</option><option>8</option></select></label></div>
<div id="charts"></div><details><summary>完整 batch 數值表</summary><div id="batchtable" class="scroll"></div></details>
<h2>Stage 分項</h2><p class="sub">僅計入目前篩選的 Formal request；以每個 request 的同名 stage duration 小計求平均。父子 stage 不相加。</p><div id="stages" class="panel"></div>
<h2>Request Explorer</h2><div class="filters">
<label>Provider <select id="provider"><option value="">全部</option><option>8000</option><option>8200</option></select></label>
<label>階段 <select id="phase"><option value="formal">Formal</option><option value="smoke">Smoke</option><option value="formal_setup_smoke">Formal setup Smoke</option><option value="">全部</option></select></label>
<label>結果 <select id="outcome"><option value="">全部</option><option value="fail">失敗</option><option value="pass">通過</option><option value="timeout">Timeout</option><option value="fallback">Fallback／Clarification</option></select></label>
<label>會員 <select id="alias"><option value="">全部</option></select></label></div><p id="requestcount" class="sub"></p><div id="requests"></div>
<h2>Endpoint 證據與實驗限制</h2><details><summary>實際 outgoing endpoint 指紋核對</summary><pre id="endpoints"></pre></details><div id="caveats" class="panel"></div>
<p class="sub" style="margin-top:32px">分享包不含原始回覆、會員 Profile、cookie、process environment 或 backend log。所有資料均在本機瀏覽器呈現，不向外連線。</p>
</main><script id="data" type="application/json">__DATA__</script><script>
'use strict';
const D=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);
const E=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const F=(v,n=2)=>v===null||v===undefined?'未測得':Number(v).toFixed(n);
const base=r=>(!$('scenario').value||r.scenario===$('scenario').value)&&(!$('vu').value||r.vu===$('vu').value*1);
$('status').innerHTML=`<b>${E(D.status)}</b><br>資料來源：${E(D.validation.source)} · 證據完整：${D.validation.experiment_evidence_complete?'是':'否／尚未完整'} · Cleanup：${D.validation.cleanup_pass?'PASS':'未通過／未驗證'}<br><span class="mono">Snapshot ${E(D.snapshot_id??'未驗證')}</span>${D.validation.issues.length?'<p>'+E(D.validation.issues.join(' · '))+'</p>':''}`;
$('cards').innerHTML=D.providers.map(p=>`<section class="card"><div class="${p.provider===8000?'a':'b'}">:${p.provider} · ${E(p.contract_mode)}</div><div class="number">${p.full_path_pass} / ${p.planned}</div><div>Formal full-path pass／planned · 已完成 ${p.completed} 筆</div><span class="tag">${E(p.status)}</span><p>Smoke ${p.smoke_full_path_pass??0}／${p.smoke_completed??0} · Smoke schema FAIL ${p.smoke_schema_failure_count??0}<br>${E((p.gate_categories||[]).join(" / "))}</p><p>成功樣本 p50 / p95：${F(p.full_path_body_ms.p50===null?null:p.full_path_body_ms.p50/1000)} / ${F(p.full_path_body_ms.p95===null?null:p.full_path_body_ms.p95/1000)} 秒<br>量測 batch goodput：${F(p.measured_batch_goodput,4)} request/s</p><small>不含 login、cooldown、Smoke 與服務啟動時間；不是整輪 throughput。</small></section>`).join('');
$('endpoints').textContent=JSON.stringify(D.endpoint_proofs,null,2);
$('diagnostics').innerHTML=(D.case_diagnostics||[]).map(c=>`<details><summary>:${c.provider} · ${E(c.scenario)} · ${E(c.phase)} · ${c.smoke_pass?'PASS':'FAIL'} · ${E(c.categories.join(' / ')||'已定義檢查通過')}</summary><pre>${E(JSON.stringify(c,null,2))}</pre></details>`).join('')||'尚無完整 Smoke 診斷';
if(!D.requests.some(r=>r.phase==='formal'))$('phase').value='smoke';
$('caveats').innerHTML=D.caveats.map(x=>`<p>${E(x)}</p>`).join('');
[...new Set(D.requests.map(r=>r.account_alias).filter(Boolean))].sort().forEach(a=>{let o=document.createElement('option');o.textContent=a;$('alias').append(o)});
const metrics=[
 ['成功樣本完整回覆 p50','秒',b=>b.full_path_body_ms.p50===null?null:b.full_path_body_ms.p50/1000,'僅 full_path_pass 的完整 body latency；n=1 時 p50/p95 都等於單筆值。'],
 ['成功樣本完整回覆 p95','秒',b=>b.full_path_body_ms.p95===null?null:b.full_path_body_ms.p95/1000,'僅 full_path_pass；不可略過成功樣本數解讀尾端效能。'],
 ['全部已完成請求 p95','秒',b=>b.all_body_ms.p95===null?null:b.all_body_ms.p95/1000,'包括失敗回應的 client body latency；未發送請求不補 0。'],
 ['Full-path goodput','request/s',b=>b.full_path_goodput,'full_path_pass 數／該 batch wall seconds。'],
 ['HTTP RPS','request/s',b=>b.http_rps,'HTTP 200 且 nonempty 數／batch wall seconds；不代表契約通過。'],
 ['Model call count','次',b=>b.completed?b.model_calls:null,'只計入 Formal 會員 request ID 關聯的 completed gateway model_call。'],
 ['Host CPU 平均','%',b=>b.telemetry.cpu_host_pct.mean,'Active samples，全機 CPU，非該 provider 專屬。'],
 ['Host RAM 平均','GiB',b=>b.telemetry.ram_host_used_gib.mean,'Active samples，全機 RAM used。'],
 ['GPU GFX util 平均','%',b=>b.telemetry.gpu_util_pct.mean,'Active samples，整張 GPU0，包含背景服務。'],
 ['GPU MEM util 平均','%',b=>b.telemetry.gpu_memory_util_pct.mean,'記憶體控制器活動指標；不是 VRAM 容量百分比。'],
 ['VRAM used 最大值','GiB（約）',b=>b.telemetry.vram_used_gib_approx.max,'整張 GPU0 的已配置顯示記憶體；原始單位轉換為近似值。'],
 ['GPU 功耗平均','W',b=>b.telemetry.gpu_power_w.mean,'Active samples，全 GPU 功耗。'],
 ['GPU 溫度平均','°C',b=>b.telemetry.gpu_temp_c.mean,'Active samples，無數據不補 0。'],
 ['GPU 時脈平均','MHz',b=>b.telemetry.gpu_clock_mhz.mean,'Active samples，非 provider 專屬。']];
function chart(title,unit,rows,value,note){let vals=rows.map(value).filter(v=>v!==null&&v!==undefined&&Number.isFinite(v)),max=Math.max(1,...vals);return `<section class="panel chart"><b>${E(title)}</b><p class="sub">${E(note)}</p>`+rows.map(r=>{let v=value(r),missing=v===null||v===undefined;return `<div class="barrow"><span class="barlabel">${E(r.scenario)} VU${r.vu} · :${r.provider}</span><div class="track" title="${E(note+' '+(missing?'未測得':F(v)+' '+unit))}"><div class="bar ${r.provider===8200?'b':''}" style="width:${missing?0:Math.max(0,v/max*100)}%"></div></div><span>${missing?'未測得':F(v)+' '+E(unit)}</span></div>`}).join('')+'</section>'}
function render(){let rows=D.batches.filter(base).sort((a,b)=>a.scenario.localeCompare(b.scenario)||a.vu-b.vu||a.provider-b.provider);$('charts').innerHTML=metrics.map(([t,u,f,n])=>chart(t,u,rows,f,n)).join('');$('batchtable').innerHTML='<table><thead><tr><th>Provider / Scenario / VU</th><th>Planned / Completed / Pass</th><th>狀態</th><th>Goodput</th><th>干擾標記</th></tr></thead><tbody>'+rows.map(b=>`<tr><td>:${b.provider} / ${E(b.scenario)} / ${b.vu}</td><td>${b.planned} / ${b.completed} / ${b.full_path_pass}</td><td>${E(b.status)}</td><td>${F(b.full_path_goodput,4)}</td><td>${E(b.deployment_status)}<br>performance eligible: ${b.performance_eligible}</td></tr>`).join('')+'</tbody></table>';
 let grouped=new Map();for(let r of D.requests.filter(r=>base(r)&&r.phase==='formal')){let sums=new Map();for(let s of r.stage_timeline){if(s.duration_ms===null)continue;let key=s.pipeline+'.'+s.stage;sums.set(key,(sums.get(key)||0)+s.duration_ms)}for(let [key,value] of sums){let gkey=r.provider+'|'+key;if(!grouped.has(gkey))grouped.set(gkey,{p:r.provider,k:key,x:[]});grouped.get(gkey).x.push(value)}}$('stages').innerHTML='<div class="scroll"><table><thead><tr><th>Provider</th><th>Stage</th><th>觀測 request 數</th><th>每 request 平均 duration</th></tr></thead><tbody>'+[...grouped.values()].sort((a,b)=>a.k.localeCompare(b.k)||a.p-b.p).map(g=>`<tr><td>:${g.p}</td><td>${E(g.k)}</td><td>${g.x.length}</td><td>${F(g.x.reduce((a,b)=>a+b,0)/g.x.length/1000)} 秒</td></tr>`).join('')+'</tbody></table></div>';renderRequests()}
function renderRequests(){let out=$('outcome').value,rows=D.requests.filter(r=>base(r)&&(!$('provider').value||r.provider===$('provider').value*1)&&(!$('phase').value||r.phase===$('phase').value)&&(!$('alias').value||r.account_alias===$('alias').value)&&(!out||(out==='pass'&&r.full_path_pass)||(out==='fail'&&!r.full_path_pass)||(out==='timeout'&&(r.client_timeout||r.backend_timeout_attempts||r.gateway_timeout_calls))||(out==='fallback'&&r.fallback_or_clarification_observed)));$('requestcount').textContent=`符合篩選 ${rows.length} 筆；Smoke 不計入 Formal KPI。`; $('requests').innerHTML=rows.map(r=>`<details><summary><span class="${r.provider===8000?'a':'b'}">:${r.provider}</span> · ${E(r.scenario)} · VU${r.vu} · ${E(r.account_alias??'無 alias')} · ${r.full_path_pass?'PASS':'FAIL'} · ${F(r.body_ms===null?null:r.body_ms/1000)} 秒</summary><p class="mono">${E(r.request_id)}<br>${E(r.evidence_source)}</p><p>${E(r.failure_reasons.join(' · ')||'所有已定義 full-path 檢查通過')}</p><p>Model calls ${r.model_calls} · Backend attempts ${r.backend_model_http_attempts} · Zep 429 ${r.zep_429_count} · Retry count：未知 · Memory persistence：未驗證</p><b>Stage 時序</b><pre>${E(JSON.stringify(r.stage_timeline,null,2))}</pre><b>Model 時序</b><pre>${E(JSON.stringify(r.model_timeline,null,2))}</pre><b>完整檢查項目</b><pre>${E(JSON.stringify(r.checks,null,2))}</pre></details>`).join('')}
['scenario','vu'].forEach(k=>$(k).addEventListener('change',render));['provider','phase','outcome','alias'].forEach(k=>$(k).addEventListener('change',renderRequests));render();
</script></html>'''


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--run-dir',required=True)
    ap.add_argument('--pack',action='store_true');args=ap.parse_args()
    run=Path(args.run_dir);build_report(run)
    if args.pack:pack_comparison(run.resolve())
    print('[DATA EVIDENCE]',run/FILES[0],'; no interactive HTML generated')

if __name__=='__main__':main()

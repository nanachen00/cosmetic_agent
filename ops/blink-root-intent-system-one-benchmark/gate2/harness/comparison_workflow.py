"""One frozen Experiment; provider failures do not erase the other treatment.

Shared auth, ownership, policy, runtime drift, and unclassified exceptions remain
workflow-wide stops. No production service action or source recapture occurs.
"""
from __future__ import annotations
import copy
import time
from common import atomic_json, read_json, utc
import runtime_capture as runtime
from provider_strategy import ProviderGateFailure, strategy
from failure_diagnostics import phase_diagnostic


def run_comparison_phases(exp, cfg, aggregate, aggregate_auth):
    gates = {'snapshot_id': exp.snapshot_id, 'started_at': utc(),
             'shared_auth': {'pass': False, 'status': 'NOT_ATTEMPTED'},
             'providers': [{**strategy(cfg, p), 'status': 'NOT_ATTEMPTED',
                            'smoke': None, 'formal': None} for p in cfg['providers']],
             'status': 'IN_PROGRESS', 'automatic_chat_retry': False}
    proofs = []
    # A provider-local contract/transport failure is a valid treatment outcome.
    # It stops that provider's later phases but must not erase the other treatment.
    # Global auth/policy/cleanup/runtime failures still stop the whole experiment.
    gates['execution_policy']='collect_both_provider_outcomes'
    def save():
        atomic_json(exp.root_run/'comparison-gates.json', gates)
        atomic_json(exp.root_run/'phase-gates.json', proofs)
    def phase_config(phase, provider):
        value = copy.deepcopy(cfg)
        value.update(providers=[provider], auth_check_only=phase=='auth', smoke_only=phase=='smoke')
        if phase=='auth':
            value['scenarios'] = value['scenarios'][:1]
            if cfg.get('smoke_only'): value['vus'] = [1]
        return value
    def runtime_ok():
        if not hasattr(exp, 'capsule'): return True  # explicit synthetic test fixture
        result = runtime.runtime_changed(read_json(exp.capsule/'runtime-metadata.json'))
        atomic_json(exp.run/'runtime-observation-after.json', result)
        return result['status']=='NO_OBSERVED_RUNTIME_METADATA_DRIFT'
    def global_failure(summary):
        if summary.get('blocked_attempts') or summary.get('auth_cleanup_pass') is False:
            return True
        # Preserve member boundaries even if a model contract failure co-occurs.
        for out in exp.run.glob('provider-*/*'):
            if not out.is_dir() or int(out.parent.name.split('-')[-1]) not in exp.cfg['providers']:
                continue
            matrix = out/'auth-matrix.json'
            if matrix.is_file() and read_json(matrix).get('http_auth_contract_pass') is False:
                return True
            assessment = out/'smoke-assessment.json'
            if assessment.is_file():
                checks = (read_json(assessment).get('path') or {}).get('checks', {})
                if any(checks.get(k) is False for k in ('backend_resolved_same_identity',
                    'identity_verified_before_chat', 'correct_identity_cookie_attached')):
                    return True
        return False
    def finish_phase(phase, p):
        summary = aggregate(exp.run)
        windows=[]
        for file in (exp.run/f'provider-{p}').glob('*/deployment-window.json'):
            if read_json(file).get('performance_eligible') is not True: windows.append(file.parent.name)
        clean_runtime=runtime_ok()
        proof={'phase':phase,'provider_port':p,'snapshot_id':exp.snapshot_id,
               'functional_pass':summary['benchmark_pass'], 'pass':summary['benchmark_pass'],
               'interference_cases':windows,'runtime_unchanged':clean_runtime,
               'finished_at':utc()}
        proof.update(phase_diagnostic(exp.run,p,summary))
        atomic_json(exp.run/f'provider-{p}-gate.json', proof)
        proofs.append(proof);save()
        return summary, proof
    save()
    try:
        first=cfg['providers'][0]
        authcfg=phase_config('auth',first)
        exp.set_phase('auth',authcfg)
        exp.scenario(first,authcfg['scenarios'][0])
        auth=aggregate_auth(exp.run)
        gates['shared_auth']={'pass':auth['benchmark_pass'],
                              'status':'PASS' if auth['benchmark_pass'] else 'FAIL'}
        proofs.append({'phase':'auth','snapshot_id':exp.snapshot_id,'functional_pass':auth['benchmark_pass'],
                       'pass':auth['benchmark_pass']});save()
        if not auth['benchmark_pass']:
            gates['status']='GLOBAL_AUTH_GATE_FAILED';return 2
        if not runtime_ok():
            gates['status']='GLOBAL_RUNTIME_DRIFT';return 3
        if cfg.get('auth_check_only'):
            gates['status']='AUTH_ONLY_COMPLETE';return 0
        any_failed=False;interference=False
        for index,p in enumerate(cfg['providers']):
            treatment=gates['providers'][index]
            if index:
                print('[A/B COOLDOWN]',cfg['batch_cooldown_seconds'],'seconds; no automatic replay',flush=True)
                time.sleep(cfg['batch_cooldown_seconds'])
            for phase in ['smoke'] + ([] if cfg.get('smoke_only') else ['formal']):
                exp.set_phase(phase,phase_config(phase,p))
                exp.candidate_test()
                treatment['status']='RUNNING_'+phase.upper();save()
                print('[A/B PHASE]',p,phase,'snapshot='+exp.snapshot_id,flush=True)
                try:
                    for sc in cfg['scenarios']:
                        print('[A/B CASE]',p,phase,sc['name'],flush=True)
                        result=exp.scenario(p,sc)
                        # Still collect all three Smoke cases. A failed Formal
                        # cell never advances to another scenario/high-load cell.
                        if phase=='formal' and result.get('status') not in {
                                'HTTP_BATCHES_PASS_PENDING_PATH_REVIEW','PASS'}:
                            break
                except ProviderGateFailure as exc:
                    exp.release_cell()  # ownership/cleanup errors propagate globally
                    treatment[phase]={'pass':False,'functional_pass':False,'category':exc.category,
                        'categories':[exc.category],'evidence_complete':True,'snapshot_id':exp.snapshot_id}
                    atomic_json(exp.run/f'provider-{p}-gate.json',treatment[phase])
                    treatment['status']='PROVIDER_GATE_FAILED';any_failed=True;save()
                    break
                summary,proof=finish_phase(phase,p)
                treatment[phase]=proof
                if not proof['runtime_unchanged']:
                    treatment['status']='GLOBAL_RUNTIME_DRIFT';gates['status']='GLOBAL_RUNTIME_DRIFT';return 3
                if global_failure(summary):
                    treatment['status']='GLOBAL_AUTH_POLICY_OR_CLEANUP_FAILURE'
                    gates['status']=treatment['status'];return 2
                if proof['interference_cases']:
                    interference=True
                    if phase=='smoke':
                        treatment['status']='SMOKE_DEPENDENCY_INTERFERENCE';any_failed=True;save();break
                if not summary['benchmark_pass']:
                    treatment['status']=phase.upper()+'_GATE_FAILED';any_failed=True;save()
                    break
                treatment['status']=phase.upper()+'_PASS';save()
        gates['status']=('COMPARISON_COMPLETED_WITH_PROVIDER_FAILURES' if any_failed else
                         'COMPARISON_COMPLETED_WITH_INTERFERENCE' if interference else
                         'COMPARISON_SMOKE_PASS' if cfg.get('smoke_only') else 'COMPARISON_FUNCTIONAL_PASS')
        if any_failed and any((t.get(ph) or {}).get('evidence_complete') is False
                              for t in gates['providers'] for ph in ('smoke','formal')):
            gates['status']='COMPARISON_INCOMPLETE_EVIDENCE'
        atomic_json(exp.root_run/'workflow-summary.json',{'snapshot_id':exp.snapshot_id,
            'status':gates['status'],'functional_pass':not any_failed,
            'performance_has_interference':interference,'providers':gates['providers'],
            'scope':'integration comparison; failed or unmeasured requests are not success latency samples'})
        return 3 if interference else 2 if any_failed else 0
    finally:
        if gates['status']=='IN_PROGRESS':gates['status']='ABORTED_BEFORE_COMPARISON_COMPLETE'
        gates['finished_at']=utc();save()
        # Per-provider summaries were evaluated with provider-local expected cells.
        # Restore the complete plan for final combined reports, including omissions.
        for phase in ('smoke','formal'):
            directory=exp.root_run/phase
            if directory.is_dir():
                full=copy.deepcopy(cfg)
                full.update(snapshot_id=exp.snapshot_id,auth_check_only=False,smoke_only=phase=='smoke')
                atomic_json(directory/'run-config.json',full)

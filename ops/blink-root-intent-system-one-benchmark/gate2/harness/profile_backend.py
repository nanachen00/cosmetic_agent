"""Identity-explicit fresh-conversation batches. Backend timing, NOT browser TTFT."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
from pathlib import Path
import threading
import time
import uuid
from common import Refuse, atomic_json, event, stats, redact, utc
from auth_clients import Client, Account, AuthFailure
from telemetry import Sampler
from smoke_checks import save_smoke_assessment, assess_batch


def new_id(): return 'shadow-'+uuid.uuid4().hex


def write_rows(path,rows):
    atomic_json(path,rows)
    if rows:
        with path.with_suffix('.csv').open('w',newline='',encoding='utf-8') as f:
            fields=sorted(set().union(*(r.keys() for r in rows)))
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)


def run_scenario(base,scenario,config,out: Path,check_invariants,backend_log: Path | None=None,accounts: list[Account] | None=None,batch_hooks=None):
    out.mkdir(parents=True,exist_ok=True)
    mode=config.get('auth_mode','guest'); timeout=config['request_timeout_seconds']
    clients=[]; setup=[]; session_checks=[]; logout=[]
    def publish():
        atomic_json(out/'auth-setup.json',{'mode':mode,'accounts':setup,
            'all_pass':bool(setup) and all(x.get('pass') is True for x in setup),
            'automatic_guest_fallback':False,'credentials_in_reports':False,
            'auth_time_excluded_from_chat_latency':True})
    def add_client(index):
        alias=accounts[index].alias if mode=='member' else f'guest-VU{index+1:02d}'
        client=Client(base,timeout,identity=mode,alias=alias); clients.append(client)
        try:
            proof=client.login(accounts[index]) if mode=='member' else client.issue_guest()
            # Detect two configured accounts resolving to one member before concurrency.
            fp=proof.get('member_fingerprint')
            if fp and any(s.get('member_fingerprint')==fp for s in setup):
                raise AuthFailure({'pass':False,'alias':alias,'category':'DUPLICATE_RESOLVED_MEMBER','status':200})
            setup.append(proof);publish();return client
        except AuthFailure as exc:
            setup.append(exc.evidence);publish();raise
    if mode=='member' and (not accounts or len(accounts)<(1 if config.get('smoke_only') else max(config['vus']))):
        raise Refuse('Fixed member pool is insufficient; no guest fallback or account duplication')
    try:
        # Do not create 16 unused authenticated sessions when Smoke alone will fail.
        primary=add_client(0)
        anon=Client(base,timeout,alias='anonymous-control')
        unauth,_=anon.chat(scenario['message'],scenario['flow'],new_id(),new_id())
        invalid=Client(base,timeout,alias='invalid-cookie-control').invalid_session_probe(scenario['message'],scenario['flow'])
        guest=primary if mode=='guest' else Client(base,timeout,identity='guest',alias='guest-control')
        if mode=='member': guest_setup=guest.issue_guest()
        else: guest_setup=setup[0]
        guest_session=guest.verify_session()  # expected 401 at MEMBER-only /session; cookie still valid for guest chat
        matrix={'primary_mode':mode,'anonymous':unauth,'invalid_member_cookie':invalid,
                'guest_setup':guest_setup,'guest_member_session_probe':guest_session,
                'member_setup':setup[0] if mode=='member' else None,
                'browser_scope':'Direct loopback API. Browser busy text alone does not identify HTTP status or auth cause.',
                'http_auth_contract_pass':unauth['status']==401 and invalid['pass'] and guest_session['status']==401}
        atomic_json(out/'auth-matrix.json',matrix)
        if config.get('auth_check_only'):
            for index in range(1,max(config['vus'])): add_client(index)
            result={'status':'AUTH_CHECK_PASS' if matrix['http_auth_contract_pass'] else 'AUTH_CHECK_FAIL',
                    'pass':matrix['http_auth_contract_pass'],'auth_mode':mode,'no_positive_chat_or_model_request':True,
                    'verified_identities':len(setup),'batches':[]}
            atomic_json(out/'auth-only-result.json',result)
            return result
        # In member mode retain one signed-guest control for comparison. Do NOT
        # count it in member latencies/path success. It can fail without turning
        # the MEMBER workload into guest or being misreported as auth failure.
        if mode=='member':
            gr,reply=guest.chat(scenario['message'],scenario['flow'],new_id(),new_id())
            matrix['guest_chat']=gr
            if isinstance(reply,str):(out/'guest-control-reply.txt').write_text(redact(reply),encoding='utf-8')
        missing,_=primary.chat(scenario['message'],scenario['flow'],new_id(),new_id(),missing_key=True)
        rid=new_id();cid=new_id()
        row,reply=primary.chat(scenario['message'],scenario['flow'],cid,rid)
        duplicate,_=primary.chat(scenario['message'],scenario['flow'],cid,rid)
        matrix['primary_chat']=row
        matrix['interpretation']='See status/detail/classification per identity; no attribution from UI fallback text alone.'
        atomic_json(out/'auth-matrix.json',matrix)
        gates={'unauthenticated_401':unauth['status']==401,'invalid_member_cookie_401':invalid['pass'],
               'guest_is_not_member':guest_session['status']==401,
               'missing_idempotency_400':missing['status']==400,'duplicate_409':duplicate['status']==409,
               'smoke_http_nonempty':row['status']==200 and row['reply_nonempty'],'smoke_correlation':row['correlated'],
               'primary_identity_verified':row['session_verified'] and
                    (row['member_cookie_attached'] and not row['guest_cookie_attached'] if mode=='member' else row['guest_cookie_attached'])}
        atomic_json(out/'smoke.json',{'checks':gates,'rows':[unauth,missing,row,duplicate],
            'valid_request_index':2,'auth_mode':mode,
            'note':'Auth, transport, pipeline and model correctness are separately reported.'})
        if isinstance(reply,str):(out/'smoke-reply.txt').write_text(redact(reply),encoding='utf-8')
        assessment=save_smoke_assessment(out,scenario['name'],backend_log,wait_seconds=2)
        result_base={'scenario':scenario['name'],'auth_mode':mode,'smoke_pass':assessment['pass'],
            'transport_guard_pass':assessment['transport_guard_pass'],'business_path_pass':assessment['business_pass'],
            'failure_reasons':assessment['failure_reasons']}
        if not assessment['pass']:
            result={**result_base,'status':'SMOKE_PATH_OR_CONTRACT_FAIL','batches':[],
                    'formal_skipped':True,'reason':'Only a genuine qualified path may advance to performance batches'}
            atomic_json(out/'final.json',result)
            print('[SMOKE FAIL]',mode,scenario['name'],','.join(assessment['failure_reasons']),flush=True)
            return result
        print('[SMOKE PATH PASS]',mode,scenario['name'],flush=True)
        if config.get('smoke_only'):
            result={**result_base,'status':'SMOKE_PASS','mode':'smoke_only','batches':[]}
            atomic_json(out/'final.json',result);return result
        for index in range(1,max(config['vus'])):add_client(index)
        batches=[]
        for rep in range(config['repetitions']):
            for vu in config['vus']:
                print(f'[COOLDOWN] {config["batch_cooldown_seconds"]}s {mode}/{scenario["name"]} VU{vu}',flush=True)
                time.sleep(config['batch_cooldown_seconds']);check_invariants()
                if mode=='member':
                    for c in clients[:vu]:
                        try:session_checks.append({'repetition':rep+1,'vu':vu,**c.verify_session()})
                        except AuthFailure as exc:
                            session_checks.append({'repetition':rep+1,'vu':vu,**exc.evidence})
                            atomic_json(out/'session-checks.json',session_checks);raise
                    atomic_json(out/'session-checks.json',session_checks)
                bd=out/f'repetition-{rep+1}'/f'vu-{vu}';bd.mkdir(parents=True)
                token=batch_hooks.before(bd) if batch_hooks else None
                if batch_hooks and token is None:
                    window=batch_hooks.after(None,bd)
                    summary={'scenario':scenario['name'],'auth_mode':mode,'vu':vu,'repetition':rep+1,
                        'snapshot_id':config.get('snapshot_id'),'planned':vu,'completed':0,'http_200':0,'http_nonempty':0,
                        'correlated':0,'wall_seconds':None,'successful_rps':None,'success_body_ms':stats([]),'all_body_ms':stats([]),
                        'status_counts':{},'telemetry':Sampler(bd/'telemetry').summary(),'all_pass':False,
                        'deployment_window':window,'status':'NOT_ATTEMPTED_DEPENDENCY_UNAVAILABLE','chat_requests_retried':False}
                    write_rows(bd/'client-results.json',[]);atomic_json(bd/'summary.json',summary);batches.append(summary)
                    print('[BATCH NOT ATTEMPTED]',scenario['name'],vu,'dependency unavailable',flush=True)
                    if config.get('stop_on_path_failure'):
                        result={**result_base,'status':'DEPENDENCY_UNAVAILABLE_STOPPED','batches':batches,
                            'formal_skipped_after':{'vu':vu,'repetition':rep+1},'automatic_retry':False}
                        atomic_json(out/'final.json',result);return result
                    continue
                sampler=Sampler(bd/'telemetry',config['telemetry_interval_seconds']);sampler.start()
                barrier=threading.Barrier(vu+1,timeout=20);rows=[]
                def work(index):
                    rid=new_id();cid=new_id();barrier.wait()
                    r,reply=clients[index].chat(scenario['message'],scenario['flow'],cid,rid)
                    r={'vu':vu,'vu_index':index+1,'repetition':rep+1,'scenario':scenario['name'],'snapshot_id':config.get('snapshot_id'),**r}
                    event(bd/'client-events.jsonl','client_request',**r)
                    if isinstance(reply,str):(bd/f'reply-vu-{index+1}.txt').write_text(redact(reply),encoding='utf-8')
                    return r
                try:
                    with ThreadPoolExecutor(max_workers=vu) as pool:
                        fs=[pool.submit(work,i) for i in range(vu)]
                        started_at=utc();start=time.perf_counter();barrier.wait()
                        for future in as_completed(fs):rows.append(future.result())
                        wall=time.perf_counter()-start
                        finished_at=utc()
                finally:sampler.stop()
                deployment_window=batch_hooks.after(token,bd) if batch_hooks else {'status':'NOT_OBSERVED','performance_eligible':False}
                rows.sort(key=lambda r:r['vu_index']);write_rows(bd/'client-results.json',rows)
                good=[r for r in rows if r['status']==200 and r['reply_nonempty']]
                summary={'scenario':scenario['name'],'auth_mode':mode,'vu':vu,'repetition':rep+1,
                    'snapshot_id':config.get('snapshot_id'),'deployment_window':deployment_window,'planned':vu,'completed':len(rows),'http_200':sum(r['status']==200 for r in rows),
                    'http_nonempty':len(good),'correlated':sum(r['correlated'] for r in rows),
                    'started_at':started_at,'finished_at':finished_at,
                    'wall_seconds':wall,'successful_rps':len(good)/wall,
                    'success_body_ms':stats(r['body_ms'] for r in good),'all_body_ms':stats(r['body_ms'] for r in rows),
                    'status_counts':{str(k):sum(r['status']==k for r in rows) for k in set(r['status'] for r in rows)},
                    'telemetry':sampler.summary(),'all_pass':len(good)==vu and all(r['correlated'] for r in rows),
                    'workload':'independent fresh conversations; existing MEMBER long-term history retained'}
                if config.get('stop_on_path_failure'):
                    assessment_batch=assess_batch(out,scenario['name'],rows,bd,backend_log,wait_seconds=2)
                    atomic_json(bd/'path-assessment.json',assessment_batch)
                    summary['full_path_pass_count']=sum(r['full_path_pass'] for r in assessment_batch['requests'])
                    summary['full_path_gate_pass']=assessment_batch['pass']
                atomic_json(bd/'summary.json',summary);batches.append(summary)
                print('[BATCH]',mode,scenario['name'],vu,len(good),'/',vu,flush=True);check_invariants()
                if config.get('stop_on_path_failure') and not summary['full_path_gate_pass']:
                    result={**result_base,'status':'PATH_GATE_FAILED_STOPPED','batches':batches,
                        'formal_skipped_after':{'vu':vu,'repetition':rep+1},'automatic_retry':False}
                    atomic_json(out/'final.json',result);return result
                if any(r['status'] in (401,403) for r in rows) or (any(r.get('error') for r in rows) and deployment_window.get('status')!='DEPENDENCY_DRIFT'):
                    result={**result_base,'status':'AUTH_OR_TRANSPORT_FAILURE','batches':batches}
                    atomic_json(out/'final.json',result);return result
        result={**result_base,'status':'HTTP_BATCHES_PASS_PENDING_PATH_REVIEW' if all(b['all_pass'] for b in batches) else 'MEASURED_WITH_ERRORS','batches':batches}
        atomic_json(out/'final.json',result);return result
    finally:
        # Revoke only cookies issued during this test. Do not revoke_all or
        # delete user/history/profile records. Interrupted runs still try this.
        for client in clients:
            if getattr(client,'needs_logout',False):
                client.timeout=min(timeout,10)
                try:logout.append(client.logout())
                except Exception as exc:logout.append({'pass':False,'alias':client.alias,'error':type(exc).__name__})
        atomic_json(out/'auth-cleanup.json',{'pass':all(r['pass'] for r in logout),'mode':mode,'sessions':logout,
            'scope':'only this test login sessions; does not erase shared account history or profile',
            'unexpected_exit_caveat':'SIGKILL/host failure can leave sessions until normal server expiry'})
        if any(not r['pass'] for r in logout):
            raise Refuse('Test-session revocation failed; inspect auth-cleanup.json (do not revoke all member sessions)')

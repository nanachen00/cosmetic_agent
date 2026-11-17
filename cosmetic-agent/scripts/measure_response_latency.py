"""Bounded real-site SSE latency smoke. Dry-run unless --live is explicit.

Reports no credentials, identities, prompts or reply text. Single tail deltas
are recorded as first content, never claimed as true streaming TTFT.
"""
from __future__ import annotations
import argparse,asyncio,codecs,hashlib,json,math,re,statistics,time,uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
import httpx

CASES={
    'greeting':('auto','你好'),
    'skin_consult':('skin_consult','玻尿酸有什麼作用？'),
    'skin_concern':('skin_consult','臉頰乾燥脫皮，洗臉後緊繃，日常應怎麼保養？'),
    'auto':('auto','玻尿酸有什麼作用？'),
    'product_search':('product_search','請推薦一千元以下、適合敏感肌的保濕乳液。'),
}
RESPONSE_HEADERS=('content-encoding','cache-control','x-app-release')
SERVICE_FAILURE_REPLIES=(
    '目前暫時無法完成回覆','目前暫時無法載入對話','目前暫時無法提供回覆','服務暫時無法使用',
    '目前暫時無法完成知識查詢','目前無法取得相關肌膚知識','知識庫暫時維護中',
)

def public_label(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',value) else None

def validate_target(value):
    p=urlsplit(value)
    try:port=p.port
    except ValueError:raise ValueError('Invalid target port') from None
    if (p.scheme not in {'http','https'} or p.hostname not in {'127.0.0.1','localhost','::1'}
        or port not in {3000,8010,18110} or p.username or p.password
        or p.path not in {'','/'} or p.query or p.fragment):
        raise ValueError('Only credential-free loopback origins on 3000/8010/18110 are allowed')
    return value.rstrip('/')

def validate_limits(limit,concurrency):
    if type(limit) is not int or not 1<=limit<=12:raise ValueError('At most 12 measured chat requests are allowed')
    if type(concurrency) is not int or not 1<=concurrency<=2:raise ValueError('Concurrency must be 1 or 2; staging load tests are separate')

class SSEParser:
    def __init__(self):
        self.decoder=codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer='';self.lines=[];self.truncated=False
    def _frame(self):
        event='message';data=[]
        for line in self.lines:
            if line.startswith(':'):continue
            key,_,value=line.partition(':')
            if value.startswith(' '):value=value[1:]
            if key=='event':event=value
            elif key=='data':data.append(value)
        self.lines=[]
        if not data:return None
        return event,json.loads('\n'.join(data))
    def feed(self,chunk):
        self.buffer+=self.decoder.decode(chunk)
        if len(self.buffer)+sum(map(len,self.lines))>262144:raise ValueError('SSE frame exceeds limit')
        events=[]
        while True:
            match=re.search(r'\r\n|\n|\r',self.buffer)
            if not match or (match.group()=='\r' and match.end()==len(self.buffer)):break
            line=self.buffer[:match.start()];self.buffer=self.buffer[match.end():]
            if not line:
                event=self._frame()
                if event is not None:events.append(event)
            else:self.lines.append(line)
        return events
    def finish(self):
        self.buffer+=self.decoder.decode(b'',final=True)
        self.truncated=bool(self.buffer.strip() or any(line and not line.startswith(':') for line in self.lines))

def flagged(payload,names):
    if not isinstance(payload,dict):return False
    for key,value in payload.items():
        active=bool(value)
        if isinstance(value,str) and value.strip().lower() in {'','none','false','0','no'}:active=False
        if key in names and active:return True
        if key in {'metadata','diagnostics','metrics','generation'} and isinstance(value,dict) and flagged(value,names):return True
    return False

def cutoff(payload):
    if not isinstance(payload,dict):return False
    if payload.get('finish_reason') in {'length','max_tokens','max_output_tokens','content_filter','error'}:return True
    if payload.get('reason')=='max_output_tokens':return True
    return any(cutoff(payload.get(key)) for key in ('metadata','generation','incomplete_details'))

class Observation:
    def __init__(self):
        self.ttfb_ms=None;self.first_delta_ms=None;self.done_ms=None;self.eof_ms=None
        self.delta_count=0;self.largest_delta_chars=0;self.delta_chars=0;self.final_chars=0
        self.queue_ms=None;self.done_count=0;self.sse_error=False;self.fallback=False;self.truncated=False
        self.after_terminal=False
        self.http_chunk_count=0;self.content_chunk_count=0;self.response_headers={}
        self.final_stage=None;self.final_route=None;self.product_count=None
    def chunk(self,parser,chunk,elapsed):
        if chunk:
            self.http_chunk_count+=1
            if self.ttfb_ms is None:self.ttfb_ms=elapsed
        before=self.delta_count
        for event,data in parser.feed(chunk):self.event(event,data,elapsed)
        if self.delta_count>before:self.content_chunk_count+=1
    def event(self,event,payload,elapsed):
        if event in {'delta','done','error'} and self.done_count:self.after_terminal=True
        if event=='status' and isinstance(payload,dict):
            value=payload.get('queue_ms')
            if type(value) in {int,float} and math.isfinite(value) and value>=0:self.queue_ms=float(value)
        if event=='delta' and isinstance(payload,dict):
            text=payload.get('text')
            if isinstance(text,str) and text.strip():
                if self.first_delta_ms is None:self.first_delta_ms=elapsed
                self.delta_count+=1;self.delta_chars+=len(text)
                self.largest_delta_chars=max(self.largest_delta_chars,len(text))
        if event=='error':self.sse_error=True
        if event=='done':
            self.done_count+=1
            if self.done_ms is None:self.done_ms=elapsed
            if not isinstance(payload,dict):self.sse_error=True;return
            reply=payload.get('reply')
            self.final_chars=len(reply) if isinstance(reply,str) and reply.strip() else 0
            self.final_stage=public_label(payload.get('stage'));self.final_route=public_label(payload.get('route'))
            products=payload.get('products')
            self.product_count=len(products) if isinstance(products,list) else 0 if products is None else None
            self.fallback=flagged(payload,{'fallback','is_fallback','used_fallback','fallback_reason','error'})
            self.truncated=flagged(payload,{'truncated','is_truncated','output_truncated'})
            if cutoff(payload):self.truncated=True
            if isinstance(reply,str) and any(term in reply for term in SERVICE_FAILURE_REPLIES):self.fallback=True
    def result(self,status,error_type=None):
        tail=self.delta_count==1
        success=(status==200 and self.done_count==1 and self.eof_ms is not None and self.final_chars>0
                 and not any([error_type,self.sse_error,self.fallback,self.truncated,self.after_terminal]))
        reason=(error_type or ('sse_error' if self.sse_error else 'fallback' if self.fallback else
            'truncated_stream' if self.truncated else 'missing_done' if self.done_count==0 else
            'invalid_terminal' if self.done_count!=1 or self.after_terminal else
            'empty_final_reply' if not self.final_chars else 'missing_eof' if self.eof_ms is None else
            'http_error' if status!=200 else None))
        return dict(status=status,success=success,error_type=reason,
            ttfb_ms=self.ttfb_ms,first_assistant_delta_ms=self.first_delta_ms,
            ttft_ms=self.first_delta_ms if self.delta_count>1 else None,
            first_content_ms=self.first_delta_ms if self.first_delta_ms is not None else self.done_ms if self.final_chars else None,
            done_ms=self.done_ms,eof_ms=self.eof_ms,queue_ms=self.queue_ms,
            delta_count=self.delta_count,largest_delta_chars=self.largest_delta_chars,
            assistant_delta_chars=self.delta_chars,final_reply_chars=self.final_chars,
            response_headers=self.response_headers,http_chunk_count=self.http_chunk_count,
            content_chunk_count=self.content_chunk_count,client_streamed_chunks=self.content_chunk_count,
            client_incremental_streaming=self.content_chunk_count>1,
            final_stage=self.final_stage,final_route=self.final_route,product_count=self.product_count,
            single_tail_delta_or_unproven_stream=tail,done_only=self.delta_count==0 and self.done_count==1,
            fallback=self.fallback,truncated=self.truncated,timeout=error_type=='timeout',
            under15=bool(success and self.done_ms is not None and self.done_ms<=15000))

async def probe(case_id,target,timeout):
    flow,prompt=CASES[case_id];obs=Observation();status=0;error=None;started=None
    async def request():
        nonlocal status,started
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=httpx.Timeout(timeout)) as client:
            auth=await client.post(target+'/api/auth/guest-session')
            status=auth.status_code;auth.raise_for_status()
            user=auth.json().get('user_id')
            if not isinstance(user,str) or not user or len(user)>128:raise ValueError('Guest response missing identity')
            payload=dict(user_id=user,conversation_id='latency-'+str(uuid.uuid4()),message=prompt,flow=flow)
            started=time.perf_counter()
            async with client.stream('POST',target+'/api/chat/message/stream',json=payload,
                headers={'Idempotency-Key':str(uuid.uuid4())}) as response:
                status=response.status_code
                obs.response_headers={name:response.headers.get(name,'')[:256] for name in RESPONSE_HEADERS}
                if status!=200:raise RuntimeError('Non-success chat status')
                if 'text/event-stream' not in response.headers.get('content-type','').lower():raise ValueError('Not an SSE response')
                queue=re.search(r'(?:^|,)\s*queue\s*;\s*dur=([0-9.]+)',response.headers.get('server-timing',''))
                if queue:
                    value=float(queue.group(1))
                    if math.isfinite(value) and value>=0:obs.queue_ms=value
                parser=SSEParser();received=0
                async for chunk in response.aiter_bytes():
                    now=(time.perf_counter()-started)*1000
                    received+=len(chunk)
                    if received>2*1024*1024:raise ValueError('Stream exceeds bounded probe size')
                    obs.chunk(parser,chunk,now)
                parser.finish();obs.truncated=obs.truncated or parser.truncated
                obs.eof_ms=(time.perf_counter()-started)*1000
    try:await asyncio.wait_for(request(),timeout=timeout)
    except (asyncio.TimeoutError,httpx.TimeoutException):error='timeout'
    except Exception as exc:error=type(exc).__name__
    result=obs.result(status,error)
    result.update(case_id=case_id,flow=flow,prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
    return result

def distribution(values):
    if not values:return dict(n=0,p50=None,p95=None,max=None,p95_small_sample=True)
    values=sorted(values)
    return dict(n=len(values),p50=statistics.median(values),p95=values[math.ceil(len(values)*.95)-1],max=max(values),p95_small_sample=len(values)<20)

def summarize(results,concurrency):
    summaries=[]
    for case_id in dict.fromkeys(row['case_id'] for row in results):
        rows=[r for r in results if r['case_id']==case_id];success=[r for r in rows if r['success']]
        summaries.append(dict(case_id=case_id,flow=rows[0]['flow'],concurrency=concurrency,
            submitted=len(rows),succeeded=len(success),failed=len(rows)-len(success),timeouts=sum(r['timeout'] for r in rows),
            error_counts=dict(Counter(r['error_type'] for r in rows if r['error_type'])),
            under15_count=sum(r['under15'] for r in rows),under15_ratio=sum(r['under15'] for r in rows)/len(rows),
            success_only={key:distribution([r[key] for r in success if r[key] is not None]) for key in ['ttfb_ms','ttft_ms','first_content_ms','done_ms','eof_ms','queue_ms']}))
    return summaries

async def run(cases,limit,concurrency,target,timeout):
    validate_limits(limit,concurrency)
    semaphore=asyncio.Semaphore(concurrency)
    async def one(index):
        async with semaphore:return await probe(cases[index%len(cases)],target,timeout)
    return await asyncio.gather(*(one(index) for index in range(limit)))

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',default='http://127.0.0.1:3000');p.add_argument('--base-url',dest='target',help=argparse.SUPPRESS)
    p.add_argument('--live',action='store_true');p.add_argument('--case',choices=CASES,action='append',dest='cases')
    p.add_argument('--limit',type=int);p.add_argument('--concurrency',type=int,default=1)
    p.add_argument('--timeout',type=float,default=60);p.add_argument('--output',type=Path)
    args=p.parse_args(argv);cases=args.cases or list(CASES);limit=args.limit if args.limit is not None else len(cases)
    try:
        target=validate_target(args.target);validate_limits(limit,args.concurrency)
        if not math.isfinite(args.timeout) or not 1<=args.timeout<=180:raise ValueError('Timeout must be 1..180 seconds')
        if args.output and args.output.exists():raise ValueError('Choose a fresh output path; existing reports are never overwritten')
    except ValueError as exc:p.error(str(exc))
    report=dict(mode='live' if args.live else 'dry-run',target=target,concurrency=args.concurrency,
        planned_chat_requests=limit,planned_guest_auth_requests=limit,planned_total_HTTP_requests=2*limit,
        timing_origin='Chat POST after legal guest-session authentication; auth not included.',
        cold_warm_state='Not controlled: no restarts or hidden warmup calls. Do not pool with separately labelled cold/warm experiments.',
        ttft_definition='First nonblank delta.text, only when multiple nonblank deltas establish a stream. Single full-tail or done-only output has null TTFT; first_content_ms remains observed.',
        provider_TTFT_measured=False,p95_definition='Nearest rank; small sample n<20 explicitly flagged. Percentiles include successful complete requests only.',
        incremental_streaming_definition='client_streamed_chunks counts distinct nonempty HTTPX decoded-body chunks that deliver nonblank assistant deltas. Only >1 proves client-observed incremental arrival; many delta events in one chunk do not.',
        provider_execution_provenance='Live HTTP timing does not prove live Terra execution. Confirm target/provider separately; never pool with dummy/mock capacity measurements.',
        fallback_detection_limit='Structured fallback/truncation flags and known service-failure replies; absent markers cannot prove the upstream never fell back.',
        cases=[dict(case_id=name,flow=CASES[name][0],prompt_sha256=hashlib.sha256(CASES[name][1].encode()).hexdigest()) for name in cases])
    if args.live:
        results=asyncio.run(run(cases,limit,args.concurrency,target,args.timeout))
        report.update(results=results,summary=summarize(results,args.concurrency))
    rendered=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(rendered+'\n')
    print(rendered)
    return 0 if not args.live or all(row['success'] for row in report['results']) else 1

if __name__=='__main__':raise SystemExit(main())

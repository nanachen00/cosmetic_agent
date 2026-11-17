"""Offline SSE/guard tests. Synthetic transport only; never call a live API."""
import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

SPEC=importlib.util.spec_from_file_location('response_latency_probe',Path(__file__).parents[1]/'scripts/measure_response_latency.py')
probe=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(probe)


def frame(name,payload):
    return ('event: '+name+'\ndata: '+json.dumps(payload,ensure_ascii=False)+'\n\n').encode()


def replay(chunks):
    parser=probe.SSEParser();observed=probe.Observation()
    for index,chunk in enumerate(chunks):
        observed.chunk(parser,chunk,float(index))
    parser.finish();observed.truncated=observed.truncated or parser.truncated;observed.eof_ms=float(len(chunks))
    return observed.result(200)


def test_fragmented_utf8_status_comments_whitespace_are_not_ttft():
    data=(b': heartbeat\r\n\r\n'+frame('status',{'stage':'processing','message':'not assistant','queue_ms':3.5})
          +frame('delta',{'text':'  \n'})+frame('delta',{'text':'玻尿酸'})
          +frame('delta',{'text':'是保濕成分。'})+frame('done',{'reply':'玻尿酸是保濕成分。'}))
    result=replay([data[i:i+1] for i in range(len(data))])
    assert result['success'] and result['delta_count']==2
    assert result['ttfb_ms']==0 and result['ttft_ms']>0 and result['queue_ms']==3.5
    assert result['first_assistant_delta_ms']==result['ttft_ms']
    assert result['final_reply_chars']==len('玻尿酸是保濕成分。')


def test_single_tail_delta_and_done_only_never_claim_ttft():
    for data in [frame('delta',{'text':'整段答案'})+frame('done',{'reply':'整段答案'}),frame('done',{'reply':'完整答案'})]:
        result=replay([data]);assert result['success'] and result['ttft_ms'] is None
        assert result['first_content_ms'] is not None and result['under15']


def test_status_and_ping_without_done_fail():
    result=replay([b': heartbeat\n\n',frame('status',{'message':'processing'})])
    assert not result['success'] and result['error_type']=='missing_done'
    assert result['ttft_ms'] is None and not result['under15']


def test_sse_error_and_duplicate_done_fail():
    for data in [frame('error',{'message':'synthetic error'}),frame('done',{'reply':'one'})+frame('done',{'reply':'two'})]:
        result=replay([data]);assert not result['success'] and not result['under15']


def test_truncated_frame_output_cutoff_and_fallback_fail():
    partial=frame('done',{'reply':'incomplete'})[:-2]
    result=replay([partial]);assert result['truncated'] and not result['success']
    for metadata in [{'fallback_reason':'synthetic'},{'metadata':{'used_fallback':True}},
                     {'truncated':True},{'generation':{'finish_reason':'length'}},
                     {'incomplete_details':{'reason':'max_output_tokens'}}]:
        result=replay([frame('delta',{'text':'x'}),frame('done',dict(reply='synthetic',**metadata))])
        assert not result['success'] and not result['under15']


def test_empty_false_flags_do_not_invent_fallback():
    result=replay([frame('done',{'reply':'synthetic','fallback':False,'error':{},'metadata':{'fallback_reason':'','used_fallback':'false'}})])
    assert result['success'] and not result['fallback']


@pytest.mark.parametrize('reply',['目前暫時無法完成知識查詢','目前無法取得相關肌膚知識','知識庫暫時維護中'])
def test_knowledge_service_failure_reply_is_not_fast_success(reply):
    result=replay([frame('delta',{'text':reply})+frame('done',{'reply':reply})])
    assert result['fallback'] and not result['success'] and not result['under15']


def test_many_deltas_one_chunk_do_not_prove_client_incremental_streaming():
    one=frame('delta',{'text':'first'});two=frame('delta',{'text':'second'})
    done=frame('done',{'reply':'firstsecond','stage':'knowledge_complete','route':'skin_consult','products':[{'name':'not-retained'}]})
    buffered=replay([one+two+done]);streamed=replay([one,two+done])
    assert buffered['delta_count']==2 and buffered['http_chunk_count']==1
    assert buffered['content_chunk_count']==buffered['client_streamed_chunks']==1
    assert not buffered['client_incremental_streaming']
    assert streamed['client_streamed_chunks']==2 and streamed['client_incremental_streaming']
    assert buffered['final_stage']=='knowledge_complete' and buffered['final_route']=='skin_consult' and buffered['product_count']==1
    assert 'not-retained' not in json.dumps(buffered)


def test_target_guard_and_concurrency_limits():
    for target in ['http://127.0.0.1:3000','http://localhost:8010','http://[::1]:18110']:
        assert probe.validate_target(target)==target
    for target in ['https://example.com','http://127.0.0.1:8000','http://user:secret@localhost:3000',
                   'http://localhost:3000/path','http://localhost:3000?key=hidden',
                   'http://localhost:3000#hidden','http://localhost.evil:3000','http://localhost:bad']:
        with pytest.raises(ValueError):probe.validate_target(target)
    for limit,concurrency in [(0,1),(13,1),(1,0),(1,3),(True,1)]:
        with pytest.raises(ValueError):probe.validate_limits(limit,concurrency)


def test_failures_timeouts_are_not_mixed_into_success_percentiles():
    good=replay([frame('done',{'reply':'ok'})]);good.update(case_id='auto',flow='auto',done_ms=12000,eof_ms=12005)
    failed=dict(good,success=False,error_type='timeout',timeout=True,under15=False,done_ms=1,eof_ms=None)
    summary=probe.summarize([good,failed],2)[0]
    assert summary['submitted']==2 and summary['succeeded']==1 and summary['timeouts']==1
    assert summary['success_only']['done_ms']==dict(n=1,p50=12000,p95=12000,max=12000,p95_small_sample=True)
    assert summary['under15_ratio']==.5


def test_concurrency_is_bounded_by_semaphore():
    active=0;peak=0
    async def fake(*args):
        nonlocal active,peak
        active+=1;peak=max(active,peak);await asyncio.sleep(.001);active-=1
        return {'success':True}
    with patch.object(probe,'probe',side_effect=fake):
        result=asyncio.run(probe.run(['auto'],6,2,'http://127.0.0.1:3000',1))
    assert peak==2 and len(result)==6


def test_dry_run_never_opens_client_or_prints_prompt(capsys):
    with patch.object(probe.httpx,'AsyncClient',side_effect=AssertionError('network forbidden')):
        assert probe.main(['--case','skin_consult','--case','auto','--limit','2'])==0
    text=capsys.readouterr().out;report=json.loads(text)
    assert report['mode']=='dry-run' and report['planned_chat_requests']==2
    assert '玻尿酸' not in text and 'results' not in report


class Chunked(httpx.AsyncByteStream):
    def __init__(self,data):self.data=data
    async def __aiter__(self):
        for index in range(0,len(self.data),3):yield self.data[index:index+3]


def test_real_probe_uses_legal_guest_cookie_without_retaining_identity():
    requests=[]
    def handler(request):
        requests.append(request.url.path)
        if request.url.path=='/api/auth/guest-session':
            return httpx.Response(200,json={'user_id':'synthetic-private-user'},headers={'set-cookie':'blink_guest_session=synthetic-private-cookie; Path=/'})
        assert request.url.path=='/api/chat/message/stream'
        assert 'synthetic-private-cookie' in request.headers['cookie']
        payload=json.loads(request.content);assert payload['user_id']=='synthetic-private-user'
        assert payload['flow']=='skin_consult'
        data=frame('status',{'message':'waiting'})+frame('delta',{'text':'保濕'})+frame('delta',{'text':'成分'})+frame('done',{'reply':'保濕成分'})
        return httpx.Response(200,headers={'content-type':'text/event-stream','content-encoding':'identity',
            'cache-control':'no-cache, no-transform','x-app-release':'probe-test',
            'set-cookie':'not-retained=synthetic-secret','x-private-header':'not-retained'},stream=Chunked(data))
    original=httpx.AsyncClient
    def factory(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return original(transport=httpx.MockTransport(handler),**kwargs)
    with patch.object(probe.httpx,'AsyncClient',side_effect=factory):
        result=asyncio.run(probe.probe('skin_consult','http://127.0.0.1:3000',2))
    rendered=json.dumps(result)
    assert result['success'] and result['ttft_ms'] is not None
    assert requests==['/api/auth/guest-session','/api/chat/message/stream']
    assert 'synthetic-private' not in rendered and '保濕' not in rendered and 'cookie' not in result
    assert result['response_headers']=={'content-encoding':'identity','cache-control':'no-cache, no-transform','x-app-release':'probe-test'}
    assert 'synthetic-secret' not in rendered and 'x-private-header' not in rendered

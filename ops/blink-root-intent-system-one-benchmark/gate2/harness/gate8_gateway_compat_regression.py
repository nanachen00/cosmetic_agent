from __future__ import annotations

import io
import json
import sys
import threading
import urllib.request
from pathlib import Path
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))

import compat_gateway


def _exercise_without_gate8_context():
    """Original harness fixtures construct Server/Handler without Gate8 attrs."""
    events=[]

    def fake_event(path,event,**fields):
        events.append({"event":event,**fields})

    def fake_http_json(url,payload,timeout,headers):
        body={
            "choices":[{"message":{"content":"{\"intent_type\":\"knowledge_query\",\"raw_query\":\"fixture\"}"},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15},
        }
        return 200,body,{}

    with mock.patch.object(compat_gateway,"event",side_effect=fake_event), \
         mock.patch.object(compat_gateway,"http_json",side_effect=fake_http_json):
        server=compat_gateway.Server(("127.0.0.1",0),compat_gateway.Handler)
        server.schema_mode="prompt_only"
        server.intent_output_token_floor=None
        server.upstream="http://127.0.0.1:9"
        server.provider_port=8200
        server.proxy_port=8203
        server.local_key="local-test-key"
        server.audit_log="memory"
        server.request_timeout=2
        # Deliberately NO gate8_treatment / gate8_arm / decision audit.

        t=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.05},daemon=True)
        t.start()
        try:
            payload={
                "model":"x",
                "input":[{"role":"user","content":"一般 fixture"}],
            }
            req=urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/v1/responses",
                data=json.dumps(payload,ensure_ascii=False).encode(),
                headers={"Content-Type":"application/json","X-Request-ID":"no-gate8-context"},
                method="POST",
            )
            with urllib.request.urlopen(req,timeout=3) as resp:
                body=json.loads(resp.read().decode())
                assert resp.status==200
                assert body["status"]=="completed"
        finally:
            server.shutdown()
            server.server_close()
            t.join(timeout=2)

    assert any(x.get("event")=="model_call" for x in events), events
    return {
        "pass":True,
        "model_call_events":sum(x.get("event")=="model_call" for x in events),
    }


def _serve_without_gate8_context_contract():
    # This is the exact legacy-contract behavior exercised by the captured
    # harness: legacy schema mode on :8200 must fail in the original serve()
    # before any server bind occurs.
    try:
        compat_gateway.serve(
            12345, 12346, 8200, "x", "x", 1,
            "legacy_schema_fields",
        )
    except compat_gateway.Unsupported:
        return {"pass": True, "exception": "Unsupported"}
    except Exception as exc:
        return {"pass": False, "exception": type(exc).__name__}
    return {"pass": False, "exception": None}



def _exercise_gate8_local_bypass():
    events=[]

    def fake_event(path,event,**fields):
        events.append({"event":event,**fields})

    class LocalBypass:
        def decide(self,info):
            return {
                "arm":"C",
                "query_sha256":info["query_sha256"],
                "query_chars":info["query_chars"],
                "schema_sha256":info["schema_sha256"],
                "local_bypass":True,
                "upstream_called":False,
                "kev_called":False,
                "route":"knowledge",
                "source":"parser",
                "error":None,
                "error_code":None,
                "decision_ms":0.1,
                "wire":{
                    "intent_type":"knowledge_query",
                    "raw_query":"fixture query",
                },
            }

    with mock.patch.object(compat_gateway,"event",side_effect=fake_event):
        server=compat_gateway.Server(("127.0.0.1",0),compat_gateway.Handler)
        server.schema_mode="vllm_native_raw_query"
        server.intent_output_token_floor=None
        server.upstream="http://127.0.0.1:9"
        server.provider_port=8200
        server.proxy_port=8203
        server.local_key="local-test-key"
        server.audit_log="memory"
        server.request_timeout=2
        server.gate8_arm="C"
        server.gate8_treatment=LocalBypass()
        server.gate8_decision_audit="memory"
        t=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.05},daemon=True)
        t.start()
        try:
            schema={
                "type":"object",
                "properties":{
                    "intent_type":{"type":"string","enum":["knowledge_query","find_product","unclear"]},
                    "raw_query":{"type":"string"},
                },
                "required":["intent_type","raw_query"],
                "additionalProperties":False,
            }
            payload={
                "model":"x",
                "input":[{
                    "role":"user",
                    "content":"prefix\n【本輪待判斷輸入】\n「fixture query」",
                }],
                "text":{"format":{
                    "type":"json_schema",
                    "name":"intent_parse_result",
                    "strict":True,
                    "schema":schema,
                }},
            }
            req=urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/v1/responses",
                data=json.dumps(payload,ensure_ascii=False).encode(),
                headers={"Content-Type":"application/json","X-Request-ID":"gate8-local-bypass"},
                method="POST",
            )
            with urllib.request.urlopen(req,timeout=3) as resp:
                body=json.loads(resp.read().decode())
                assert resp.status==200
                assert body["status"]=="completed"
                text=body["output"][0]["content"][0]["text"]
                wire=json.loads(text)
                assert wire["intent_type"]=="knowledge_query"
                assert wire["raw_query"]=="fixture query"
        finally:
            server.shutdown();server.server_close();t.join(timeout=2)

    model=[x for x in events if x.get("event")=="model_call"]
    decision=[x for x in events if x.get("event")=="gate8_intent_decision"]
    assert len(model)==1,events
    assert len(decision)==1,events
    assert model[0].get("gate8_intent_bypass") is True,model[0]
    assert model[0].get("provider_http_called") is False,model[0]
    assert model[0].get("schema_validation")=="pass",model[0]
    return {
        "pass":True,
        "model_call_events":len(model),
        "decision_events":len(decision),
        "provider_http_called":model[0].get("provider_http_called"),
        "schema_validation":model[0].get("schema_validation"),
    }

def main():
    missing_context=_exercise_without_gate8_context()
    local_bypass=_exercise_gate8_local_bypass()
    serve_contract=_serve_without_gate8_context_contract()
    assert serve_contract["pass"], serve_contract
    events=[]

    def fake_event(path,event,**fields):
        events.append({"event":event,**fields})

    def fake_http_json(url,payload,timeout,headers):
        body={
            "choices":[{"message":{"content":"{\"intent_type\":\"knowledge_query\"}"},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14},
        }
        return 200,body,{}

    # Patch the wrapper module exactly like the captured harness contract does:
    # diagnostics raises, but HTTP/schema verdict must stay unchanged and audit
    # must record diagnostic status=unavailable.
    with mock.patch.object(compat_gateway,"event",side_effect=fake_event), \
         mock.patch.object(compat_gateway,"http_json",side_effect=fake_http_json), \
         mock.patch.object(compat_gateway,"output_contract_diagnostics",side_effect=RuntimeError("diagnostic boom")):

        server=compat_gateway.Server(("127.0.0.1",0),compat_gateway.Handler)
        server.schema_mode="prompt_only"
        server.intent_output_token_floor=None
        server.upstream="http://127.0.0.1:9"
        server.provider_port=8200
        server.proxy_port=8203
        server.local_key="local-test-key"
        server.audit_log="memory"
        server.request_timeout=2
        server.gate8_arm="A"
        class Baseline:
            def decide(self,info):
                return {
                    "arm":"A","query_sha256":info["query_sha256"],"query_chars":info["query_chars"],
                    "schema_sha256":info["schema_sha256"],"local_bypass":False,
                    "upstream_called":True,"kev_called":False,"route":None,"source":"baseline_upstream",
                    "error":None,"decision_ms":0.0,
                }
        server.gate8_treatment=Baseline()
        server.gate8_decision_audit="memory"
        t=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.05},daemon=True)
        t.start()
        try:
            schema={
                "type":"object",
                "properties":{
                    "intent_type":{"type":"string","enum":["knowledge_query","find_product","unclear"]},
                    "raw_query":{"type":"string"},
                },
                "required":["intent_type","raw_query"],
                "additionalProperties":True,
            }
            payload={
                "model":"x",
                "input":[{"role":"user","content":"一般測試，不含 Gate8 marker"}],
                "text":{"format":{"type":"json_schema","name":"other_schema","strict":True,"schema":schema}},
            }
            req=urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/v1/responses",
                data=json.dumps(payload,ensure_ascii=False).encode(),
                headers={"Content-Type":"application/json","X-Request-ID":"compat-regression"},
                method="POST",
            )
            with urllib.request.urlopen(req,timeout=3) as resp:
                body=json.loads(resp.read().decode())
                assert resp.status==200
                assert body["status"]=="completed"
        finally:
            server.shutdown()
            server.server_close()
            t.join(timeout=2)

    model=[x for x in events if x.get("event")=="model_call"]
    assert len(model)==1,events
    d=model[0].get("output_contract_diagnostics")
    assert isinstance(d,dict),model[0]
    assert d.get("status")=="unavailable",d
    assert model[0].get("schema_validation") in {"pass","fail","validator_unavailable"},model[0]
    print(json.dumps({
        "pass":True,
        "missing_gate8_context_passthrough":missing_context,
        "serve_without_gate8_context_contract":serve_contract,
        "gate8_local_bypass":local_bypass,
        "diagnostic_status":d.get("status"),
        "model_call_events":len(model),
    },indent=2))


if __name__=="__main__":
    main()

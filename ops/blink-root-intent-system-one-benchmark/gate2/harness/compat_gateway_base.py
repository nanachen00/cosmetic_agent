"""Text-only, non-streaming Responses/Chat compatibility bridge -> shadow LiteLLM.
Model reasoning, tools, browsing, stateful Responses IDs, multimodal and streams
are not emulated. Unsupported requests return 422/501 and are separately logged.
Default mode keeps the previous equal prompt-only comparison. Explicit
vllm_native_raw_query mode adds native response_format and binds the known
intent input echo before generation. No reply repair or automatic fallback.
"""
from __future__ import annotations
import argparse
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import time
import uuid
from common import Refuse,event,http_json

class Unsupported(Refuse): pass

def textual(content):
    if isinstance(content,str): return content
    if not isinstance(content,list): raise Unsupported('Only text messages are supported')
    pieces=[]
    for x in content:
        if not isinstance(x,dict) or x.get('type') not in {'input_text','output_text','text'} or not isinstance(x.get('text'),str):
            raise Unsupported('Non-text content cannot be silently discarded')
        pieces.append(x['text'])
    return '\n'.join(pieces)

def coalesce_system_prefix(messages: list[dict]) -> tuple[list[dict], dict]:
    """Preserve every leading instruction in a single system message.

    The observed local tokenizer retains [system, user] but loses both system
    contents for [system, system, user]. This adapter therefore joins only the
    contiguous system prefix, in its existing order, with two newlines. User and
    assistant messages (including their boundaries) are left untouched.

    A system/developer message after dialogue has started is not relocated or
    silently discarded: that unsupported layout fails closed. Developer roles
    have already been mapped to system by convert(), as in the original bridge.
    This is text preservation, not a claim of strict schema/model compliance.
    """
    prefix_count = 0
    for message in messages:
        if message['role'] != 'system':
            break
        prefix_count += 1
    if any(message['role'] == 'system' for message in messages[prefix_count:]):
        raise Unsupported(
            'System/developer messages after dialogue starts are unsupported '
            'by this local adapter; messages were not reordered'
        )
    if prefix_count > 1:
        system = {
            'role': 'system',
            'content': '\n\n'.join(
                message['content'] for message in messages[:prefix_count]
            ),
        }
        normalized = [system, *messages[prefix_count:]]
    else:
        normalized = messages
    # Counts/roles only. No instruction, schema or user text is added to logs.
    info = {
        'leading_system_messages_before': prefix_count,
        'leading_system_messages_after': 1 if prefix_count else 0,
        'system_prefix_merged': prefix_count > 1,
        'forwarded_message_roles': [message['role'] for message in normalized],
    }
    return normalized, info

def convert(payload: dict, path: str, *, schema_mode: str = 'prompt_only', intent_output_token_floor=None) -> tuple[dict,dict]:
    if schema_mode not in {'prompt_only', 'vllm_native_raw_query', 'legacy_explicit_json', 'legacy_schema_fields'}:
        raise Unsupported('Unknown schema experiment mode')
    if not isinstance(payload,dict): raise Unsupported('JSON object required')
    if payload.get('stream'): raise Unsupported('Streaming not included in this benchmark')
    for key in ('tools','previous_response_id','conversation','include'):
        if payload.get(key): raise Unsupported('Unsupported Responses feature: '+key)
    if payload.get('n',1)!=1: raise Unsupported('Only n=1 supported')
    if payload.get('tool_choice') not in (None,'none'): raise Unsupported('Tool selection unsupported')
    reasoning=payload.get('reasoning',{}) or {}
    effort=payload.get('reasoning_effort',reasoning.get('effort','none'))
    if effort not in ('none',None): raise Unsupported('Cannot reproduce reasoning effort='+str(effort))
    responses=path.rstrip('/')=='/v1/responses'
    allowed={'model','stream','tools','tool_choice','n','reasoning','reasoning_effort','store','metadata','user',
             'temperature','top_p','stop','seed','presence_penalty','frequency_penalty','max_tokens','max_completion_tokens',
             'response_format','messages','instructions','input','text','max_output_tokens','previous_response_id','conversation','include','service_tier'}
    unknown=set(payload)-allowed
    if unknown: raise Unsupported('Unreviewed request fields: '+','.join(sorted(unknown)))
    messages=[]
    if responses:
        if payload.get('instructions'):
            if not isinstance(payload['instructions'],str): raise Unsupported('Instructions must be text')
            messages.append({'role':'system','content':payload['instructions']})
        incoming=payload.get('input',[])
        if isinstance(incoming,str): incoming=[{'role':'user','content':incoming}]
        fmt=(payload.get('text') or {}).get('format')
        limit=payload.get('max_output_tokens')
    else:
        incoming=payload.get('messages',[]); fmt=payload.get('response_format')
        # response_format is admitted below via allowed set (see module definition).
        limit=payload.get('max_completion_tokens',payload.get('max_tokens'))
    if not isinstance(incoming,list) or not incoming: raise Unsupported('Input/messages must not be empty')
    for item in incoming:
        if not isinstance(item,dict) or item.get('role') not in {'system','developer','user','assistant'}:
            raise Unsupported('Only ordinary text message roles supported')
        if item.get('tool_calls'): raise Unsupported('Tool call history unsupported')
        messages.append({'role':'system' if item['role']=='developer' else item['role'],'content':textual(item.get('content'))})
    schema=None; fmt_type=None
    if fmt:
        if not isinstance(fmt,dict): raise Unsupported('Bad format')
        fmt_type=fmt.get('type')
        if fmt_type=='json_schema':
            schema=fmt.get('schema') or (fmt.get('json_schema') or {}).get('schema')
            if not isinstance(schema,dict): raise Unsupported('JSON schema missing')
            instruction='只輸出符合下列 JSON Schema 的 JSON；不要 Markdown 或說明。\n'+json.dumps(schema,ensure_ascii=False,separators=(',',':'))
            if schema_mode not in {'legacy_explicit_json', 'legacy_schema_fields'}:
                messages.insert(0,{'role':'system','content':instruction})
        elif fmt_type=='json_object':
            if schema_mode not in {'legacy_explicit_json', 'legacy_schema_fields'}:
                messages.insert(0,{'role':'system','content':'只輸出合法 JSON 物件，不要 Markdown 或說明。'})
        elif fmt_type not in (None,'text'): raise Unsupported('Unknown output format')
    messages,layout_info=coalesce_system_prefix(messages)
    out={'model':'taide-12b','messages':messages,'stream':False}
    if limit is not None:
        if isinstance(limit,bool) or int(limit)<=0: raise Unsupported('Invalid output token budget')
        out['max_tokens']=int(limit)
    for k in ('temperature','top_p','stop','seed','presence_penalty','frequency_penalty'):
        if k in payload: out[k]=payload[k]
    # Same explicit temperature for both providers when SDK request omitted it.
    out.setdefault('temperature',0.1)
    metadata={'requested_model':payload.get('model'), 'actual_model':'taide-12b',
              'request_api':'responses' if responses else 'chat_completions',
              'schema_mode':'prompt_only' if fmt_type in ('json_object','json_schema') else 'none',
              'schema':schema,'requested_max_tokens':limit,'output_format_type':fmt_type,
              'temperature_defaulted':'temperature' not in payload,
              'unsupported_provider_semantics':['reasoning_effort_none','store_false','verbosity_metadata_only']}
    metadata.update(layout_info)
    if schema_mode == 'vllm_native_raw_query':
        from native_schema import attach
        try: out,metadata=attach(out,metadata,payload,path)
        except Refuse as exc: raise Unsupported(str(exc)) from None
    if schema_mode == 'legacy_explicit_json':
        from legacy_contract import attach as attach_legacy
        try: out,metadata=attach_legacy(out,metadata,payload,path)
        except Refuse as exc: raise Unsupported(str(exc)) from None
    if schema_mode == 'legacy_schema_fields':
        from legacy_schema_fields import attach as attach_fields
        try: out,metadata=attach_fields(out,metadata,payload,path)
        except Refuse as exc: raise Unsupported(str(exc)) from None
    from generation_budget import attach as attach_budget
    try: out,metadata=attach_budget(out,metadata,floor=intent_output_token_floor)
    except Refuse as exc: raise Unsupported(str(exc)) from None
    from provider_strategy import outgoing_metadata
    metadata['endpoint_request_fingerprint']=outgoing_metadata(json.dumps(out,ensure_ascii=False))
    return out,metadata

# response_format is a normal Chat field. Kept in a single explicit set in convert.

def responses_envelope(chat: dict):
    choices=chat.get('choices') or []
    if not choices: raise Unsupported('Provider response has no choices')
    content=choices[0].get('message',{}).get('content')
    if not isinstance(content,str): raise Unsupported('Provider response has no text')
    u=chat.get('usage') or {}; truncated=choices[0].get('finish_reason')=='length'
    return {'id':'resp_shadow_'+uuid.uuid4().hex,'object':'response','created_at':int(time.time()),
            'model':'taide-12b','status':'incomplete' if truncated else 'completed',
            'incomplete_details':{'reason':'max_output_tokens'} if truncated else None,
            'error':None,'output':[{'id':'msg_shadow_'+uuid.uuid4().hex,'type':'message',
                'status':'incomplete' if truncated else 'completed','role':'assistant',
                'content':[{'type':'output_text','text':content,'annotations':[]}]}],
            'usage':{'input_tokens':u.get('prompt_tokens',0),'output_tokens':u.get('completion_tokens',0),
                'total_tokens':u.get('total_tokens',0),'input_tokens_details':{'cached_tokens':0},
                'output_tokens_details':{'reasoning_tokens':0}},
            'parallel_tool_calls':False,'tools':[],'store':False}


def _json_error_code(exc: json.JSONDecodeError) -> str:
    """Do not log JSONDecodeError.doc or arbitrary exception strings."""
    messages = {
        'Expecting value': 'expecting_value',
        'Extra data': 'extra_data',
        'Expecting property name enclosed in double quotes': 'expecting_property_name',
        "Expecting ',' delimiter": 'expecting_comma',
        "Expecting ':' delimiter": 'expecting_colon',
    }
    if exc.msg.startswith('Invalid \\uXXXX escape'):
        return 'invalid_unicode_escape'
    if exc.msg.startswith('Illegal trailing comma'):
        return 'trailing_comma'
    if exc.msg.startswith('Unterminated string'):
        return 'unterminated_string'
    if exc.msg.startswith('Invalid control character'):
        return 'invalid_control_character'
    if exc.msg.startswith('Invalid \\escape'):
        return 'invalid_escape'
    for prefix, code in messages.items():
        if exc.msg.startswith(prefix):
            return code
    return 'other_json_syntax'


def _syntax_character_class(text, offset):
    if offset >= len(text): return 'end_of_output'
    char = text[offset]
    named = {'{':'object_open', '}':'object_close', '[':'array_open', ']':'array_close',
             ',':'comma', ':':'colon', '"':'double_quote', "'":'single_quote', '\\':'backslash'}
    return named.get(char, 'whitespace' if char.isspace() else 'digit' if char.isdigit() else 'other')


def _enum_mismatch_diagnostic(instance, allowed):
    if not isinstance(allowed, list): return None
    return {'allowed_value_count': len(allowed),
            'allowed_types': sorted({_json_value_kind(v) for v in allowed}),
            'value_is_empty_string': instance == '' if isinstance(instance, str) else False,
            'matches_after_strip': isinstance(instance, str) and any(
                isinstance(v, str) and instance.strip() == v for v in allowed),
            'matches_after_casefold': isinstance(instance, str) and any(
                isinstance(v, str) and instance.casefold() == v.casefold() for v in allowed),
            'values_exported': False, 'value_rewritten': False}


def _json_value_kind(value) -> str:
    if value is None: return 'null'
    if isinstance(value, bool): return 'boolean'
    if isinstance(value, str): return 'string'
    if isinstance(value, dict): return 'object'
    if isinstance(value, list): return 'array'
    if isinstance(value, (int, float)): return 'number'
    return 'other'


def _public_schema_path(path, schema) -> list:
    """Expose bounded schema field names only, never model-created object keys.

    Unknown/dynamic keys become <field>. Values, prompts and validation exception
    messages are deliberately excluded. Known field names originate from the
    requested schema, not from a member's model reply.
    """
    keywords = {
        '$defs', 'definitions', 'properties', 'items', 'anyOf', 'allOf', 'oneOf',
        'not', 'if', 'then', 'else', 'type', 'enum', 'const', 'required',
        'additionalProperties', 'patternProperties', 'minLength', 'maxLength',
        'minItems', 'maxItems', 'minimum', 'maximum', 'exclusiveMinimum',
        'exclusiveMaximum', 'multipleOf', 'pattern', 'format', 'prefixItems',
        'contains', 'uniqueItems', 'dependentRequired', 'dependentSchemas',
        'minProperties', 'maxProperties', 'propertyNames',
    }
    names = set(keywords)
    nodes = [schema]
    for _ in range(2048):
        if not nodes: break
        node = nodes.pop()
        if isinstance(node, dict):
            for group in ('properties', '$defs', 'definitions'):
                mapping = node.get(group)
                if isinstance(mapping, dict):
                    for key in mapping:
                        if isinstance(key, str) and len(key) <= 64 and key.isascii() and key.replace('_', '').isalnum():
                            names.add(key)
            nodes.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            nodes.extend(v for v in node if isinstance(v, (dict, list)))
    result = []
    for part in list(path)[:24]:
        if isinstance(part, int) and not isinstance(part, bool): result.append(part)
        elif isinstance(part, str) and part in names: result.append(part)
        else: result.append('<field>')
    return result


def _inspect_json_candidate(text: str, schema: dict | None, fmt_type: str) -> dict:
    result = {'json_parse': None, 'schema_validation': 'not_run'}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        result.update(json_parse='fail', parse_error={
            'code': _json_error_code(exc), 'offset': exc.pos,
            'line': exc.lineno, 'column': exc.colno,
            'at_end_of_output': exc.pos >= len(text),
            'character_class': _syntax_character_class(text, exc.pos),
        })
        return result
    result.update(json_parse='pass', root_type=_json_value_kind(parsed))
    if fmt_type == 'json_object':
        result['schema_validation'] = 'pass' if isinstance(parsed, dict) else 'fail'
        if not isinstance(parsed, dict): result['validation_keyword'] = 'type'
        return result
    if not isinstance(schema, dict):
        result['schema_validation'] = 'schema_unavailable'
        return result
    try:
        import jsonschema
        jsonschema.validate(parsed, schema)
        result['schema_validation'] = 'pass'
    except ImportError:
        result['schema_validation'] = 'validator_unavailable'
    except Exception as exc:
        if isinstance(exc, jsonschema.exceptions.ValidationError):
            # Record the first validator failure, not the invalid value.
            result.update(schema_validation='fail',
                          instance_path=_public_schema_path(exc.absolute_path, schema),
                          schema_path=_public_schema_path(exc.absolute_schema_path, schema),
                          instance_type=_json_value_kind(exc.instance))
            keyword = getattr(exc, 'validator', None)
            if keyword == 'enum':
                result['enum_mismatch'] = _enum_mismatch_diagnostic(exc.instance, exc.validator_value)
            result['validation_keyword'] = keyword if keyword in {
                'type', 'enum', 'const', 'required', 'additionalProperties',
                'anyOf', 'oneOf', 'allOf', 'maxLength', 'minLength', 'pattern',
                'minItems', 'maxItems', 'minimum', 'maximum', 'uniqueItems',
                'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf',
            } else 'other'
        else:
            result['schema_validation'] = 'diagnostic_validator_error'
    return result


def json_lexical_summary(text):
    """Count closure/escape evidence without exposing key/value text or fixing it."""
    stack=[]; inside=False; pending=False; mismatches=0; invalid_u=0; incomplete_u=0; u_count=0
    i=0
    while i < len(text):
        c=text[i]
        if inside:
            if c=='"': inside=False
            elif c=='\\':
                if i+1>=len(text): pending=True; break
                if text[i+1]=='u':
                    u_count+=1; digits=text[i+2:i+6]
                    if len(digits)<4: incomplete_u+=1
                    elif any(x not in '0123456789abcdefABCDEF' for x in digits): invalid_u+=1
                    if len(digits)==4 and all(x in '0123456789abcdefABCDEF' for x in digits): i+=6;continue
                i+=2;continue
        elif c=='"': inside=True
        elif c in '{[': stack.append(c)
        elif c in '}]':
            if stack and ((stack[-1]=='{' and c=='}') or (stack[-1]=='[' and c==']')): stack.pop()
            else: mismatches+=1
        i+=1
    return {'unclosed_containers':len(stack),'unterminated_string':inside,
            'trailing_backslash':pending,'mismatched_closers':mismatches,
            'unicode_escape_count':u_count,'incomplete_unicode_escape_count':incomplete_u,
            'invalid_unicode_escape_count':invalid_u,
            'scope':'lexical_observation_not_truncation_proof','raw_values_exported':False}


def output_contract_diagnostics(text, schema: dict | None, fmt_type: str,
                                requested_limit=None, usage=None, finish_reason=None) -> dict:
    """Metadata-only evidence; NEVER used to replace the raw reply or pass gate.

    Revalidating the content inside exactly one whole Markdown fence is an
    observational counterfactual only. We do not extract an arbitrary JSON block,
    repair punctuation, map enum values, truncate text or call another model.
    Existing schema_validation, HTTP body and finish_reason stay authoritative.
    """
    d = {'status': 'recorded', 'scope': 'diagnostic_only_no_reply_or_gate_change',
         'raw_reply_rewritten': False, 'additional_model_calls': 0}
    if not isinstance(text, str):
        d.update(status='not_text', whole_markdown_fence=False)
        return d
    stripped = text.strip()
    d['layout'] = ('empty' if not stripped else
                   'markdown_fence' if stripped.startswith('```') else
                   'bom' if stripped.startswith('\ufeff') else
                   'object_or_array' if stripped[:1] in ('{', '[') else 'other')
    d['raw'] = _inspect_json_candidate(text, schema, fmt_type)
    d['whole_markdown_fence'] = False
    # Exact single envelope only. No prefix/suffix prose, nested fences, etc.
    lines = stripped.splitlines()
    if (len(lines) >= 3 and lines[0].strip().lower() in {'```json', '```'}
            and lines[-1].strip() == '```'
            and not any('```' in line for line in lines[1:-1])):
        d['whole_markdown_fence'] = True
        d['fence_payload_diagnostic'] = _inspect_json_candidate(
            '\n'.join(lines[1:-1]), schema, fmt_type)
        d['fence_payload_used_as_response'] = False
    completion = (usage or {}).get('completion_tokens') if isinstance(usage, dict) else None
    def nonbool_int(v): return isinstance(v, int) and not isinstance(v, bool)
    d['reported_completion_reached_requested_limit'] = (
        completion >= requested_limit
        if nonbool_int(completion) and nonbool_int(requested_limit) and requested_limit > 0 else None)
    d['token_count_is_not_proof_of_truncation'] = True
    reached=d['reported_completion_reached_requested_limit']
    d['completion_limit_signal']=('provider_reported_length' if finish_reason=='length' else
        'stop_reported_at_or_above_limit' if finish_reason=='stop' and reached is True else
        'below_requested_limit' if reached is False else 'UNKNOWN')
    d['lexical_summary']=json_lexical_summary(text)
    return d


class Server(ThreadingHTTPServer):
    daemon_threads=True; allow_reuse_address=True

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,data):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw))); self.end_headers()
        try: self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError): pass
    def do_GET(self):
        if self.path=='/health': return self.send(200,{'status':'ok','kind':'local_text_compat'})
        if self.path=='/v1/models': return self.send(200,{'object':'list','data':[{'id':'taide-12b','object':'model'}]})
        self.send(404,{'error':{'message':'Unknown endpoint'}})
    def do_POST(self):
        rid=self.headers.get('X-Request-ID','')[:128]; start=time.perf_counter(); status=500; meta={}; error=None
        call_id='call-'+uuid.uuid4().hex
        try:
            if self.path not in {'/v1/chat/completions','/v1/responses'}: raise Unsupported('Endpoint not in text-only bridge')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=4*1024*1024: raise Unsupported('Invalid body length')
            p=json.loads(self.rfile.read(length)); out,meta=convert(p,self.path,schema_mode=getattr(self.server,'schema_mode','prompt_only'),
                intent_output_token_floor=getattr(self.server,'intent_output_token_floor',None))
            provider_request_id='shadow-'+uuid.uuid4().hex
            status,body,headers=http_json(self.server.upstream+'/v1/chat/completions',out,
                          timeout=self.server.request_timeout,headers={'Authorization':'Bearer '+self.server.local_key,
                          'X-Request-ID':provider_request_id, 'X-Blink-Shadow-Parent-Request-ID':rid,
                          'X-Blink-Shadow-Call-ID':call_id})
            if status==200:
                schema=meta.pop('schema',None)
                bound_schema=meta.pop('_native_schema',None)
                legacy_bound=meta.pop('_legacy_bound_schema',None)
                text=(body.get('choices') or [{}])[0].get('message',{}).get('content','')
                meta['output_chars']=len(text) if isinstance(text,str) else 0
                meta['finish_reason']=(body.get('choices') or [{}])[0].get('finish_reason')
                meta['usage']=body.get('usage')
                if meta.get('output_format_type')=='json_object':
                    # Validation/observability only: keep the exact same prompt
                    # and raw model reply. Never repair JSON or add model calls.
                    try:
                        parsed=json.loads(text)
                        meta['schema_validation']='pass' if isinstance(parsed,dict) else 'fail'
                        if not isinstance(parsed,dict): meta['validation_error_kind']='expected_json_object'
                    except (TypeError,ValueError):
                        meta['schema_validation']='fail'; meta['validation_error_kind']='invalid_json'
                    meta['validation_kind']='json_object'
                elif schema is not None:
                    try:
                        import jsonschema
                        jsonschema.validate(json.loads(text),schema); meta['schema_validation']='pass'
                    except ImportError: meta['schema_validation']='validator_unavailable'
                    except json.JSONDecodeError:
                        meta['schema_validation']='fail'; meta['validation_error_kind']='invalid_json'
                    except Exception as exc:
                        meta['schema_validation']='fail'; meta['validation_error_kind']='schema_validation'
                        keyword=getattr(exc,'validator',None)
                        if isinstance(keyword,str) and keyword in {'required','type','enum','const','additionalProperties','anyOf','oneOf','allOf','maxLength','minLength','pattern'}:
                            meta['validation_keyword']=keyword
                if bound_schema is not None:
                    # Verify raw original text against BOTH schemas. No recovery,
                    # coercion, default filling or replacement of model content.
                    try:
                        import jsonschema
                        jsonschema.validate(json.loads(text),bound_schema)
                        meta['native_bound_schema_validation']='pass'
                    except Exception:
                        meta['native_bound_schema_validation']='fail'
                        meta['schema_validation']='fail'
                if meta.get('legacy_contract_requested'):
                    # Validate the untouched text, including the input echo.
                    # The narrowed schema never replaces the original validator.
                    try:
                        if legacy_bound is not None:
                            import jsonschema
                            jsonschema.validate(json.loads(text),legacy_bound)
                        elif meta.get('schema_validation') != 'pass':
                            raise ValueError('JSON object contract failed')
                        meta['legacy_bound_schema_validation']='pass'
                    except Exception:
                        meta['legacy_bound_schema_validation']='fail'
                        meta['schema_validation']='fail'
                # Evidence only. A fence-only candidate never changes the raw
                # response or the existing strict schema_validation verdict.
                if meta.get('output_format_type') in {'json_object', 'json_schema'}:
                    try:
                        meta['output_contract_diagnostics'] = output_contract_diagnostics(
                            text, schema, meta['output_format_type'],
                            out.get('max_tokens'), meta.get('usage'), meta.get('finish_reason'))
                    except Exception:
                        meta['output_contract_diagnostics'] = {
                            'status': 'unavailable',
                            'scope': 'diagnostic_only_no_reply_or_gate_change',
                            'raw_reply_rewritten': False, 'additional_model_calls': 0}
                if self.path=='/v1/responses': body=responses_envelope(body)
            meta.pop('schema',None); meta.pop('_native_schema',None); meta.pop('_legacy_bound_schema',None)
            self.send(status,body)
        except Unsupported as exc:
            status=501; error=str(exc); self.send(status,{'error':{'type':'unsupported_local_adapter_feature','message':error}})
        except Exception as exc:
            status=502; error=type(exc).__name__; self.send(status,{'error':{'type':'local_gateway_failure','message':error}})
        finally:
            meta.pop('schema',None); meta.pop('_native_schema',None); meta.pop('_legacy_bound_schema',None)
            event(self.server.audit_log,'model_call',request_id=rid,provider_request_id=locals().get('provider_request_id'),gateway_call_id=call_id,provider_port=self.server.provider_port,
                  proxy_port=self.server.proxy_port,status=status,error=error,duration_ms=(time.perf_counter()-start)*1000,**meta)

def serve(port,proxy_port,provider_port,local_key,audit_log,timeout,schema_mode='prompt_only',intent_output_token_floor=None):
    from generation_budget import validate_floor
    validate_floor(intent_output_token_floor)
    if intent_output_token_floor is not None and (provider_port not in {8000,8200} or schema_mode not in {'legacy_schema_fields','vllm_native_raw_query'}):
        raise Unsupported('Intent budget is restricted to the reviewed paired Shadow treatments')
    if schema_mode not in {'prompt_only','vllm_native_raw_query','legacy_explicit_json','legacy_schema_fields'}:
        raise Unsupported('Unknown schema experiment mode')
    if schema_mode == 'vllm_native_raw_query' and provider_port in {8000,8001,8002,8003,8010,6379}:
        raise Unsupported('Native schema experiment cannot target a legacy/production endpoint')
    if schema_mode in {'legacy_explicit_json', 'legacy_schema_fields'} and provider_port != 8000:
        raise Unsupported('Explicit legacy contract must target shared :8000 only')
    with Server(('127.0.0.1',port),Handler) as server:
        server.schema_mode=schema_mode
        server.intent_output_token_floor=intent_output_token_floor
        server.upstream=f'http://127.0.0.1:{proxy_port}'; server.provider_port=provider_port
        server.proxy_port=proxy_port; server.local_key=local_key; server.audit_log=audit_log; server.request_timeout=timeout
        server.serve_forever(poll_interval=.2)

def main():
    a=argparse.ArgumentParser(); a.add_argument('--spec',required=True); args=a.parse_args()
    spec=json.loads(Path(args.spec).read_text())
    serve(**spec)
if __name__=='__main__': main()

"""Process-local import, HTTP and Python-socket restrictions + metadata-only timing.
This is NOT a kernel firewall/sandbox for arbitrary native code. It prevents
accidental Python/SDK cloud-model fallback in the tested processes; no global
security settings, installed packages or production clients are modified.
"""
from __future__ import annotations
import contextvars
import functools
import importlib.abc
import importlib.machinery
import ipaddress
import os
import hashlib
import platform
import re
import shutil
import stat
import sysconfig
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlparse
from common import Refuse, event

TRACE_ID=contextvars.ContextVar('shadow_trace_id',default='')
_POLICY=None
class EgressBlocked(Refuse): pass

class Policy:
    def __init__(self, cfg: dict):
        self.cfg=cfg; self.role=cfg['role']; self.log=Path(cfg['audit_log'])
        self.local=set(map(int,cfg.get('local_ports',[])))
        self.bind_port=cfg.get('bind_port')
        self.https=set(cfg.get('https_hosts',[]))
        self.host_ports={(str(x[0]).lower(),int(x[1])) for x in cfg.get('host_ports',[])}
        self.allowed_ips=set()
        self.prod=Path(cfg['production_root']).resolve()
        self.production_roots={self.prod}|{Path(p).resolve() for p in cfg.get('production_roots',[])}
        self.shadow=Path(cfg.get('shadow_root',cfg['private_root'])).resolve()
        self.private=Path(cfg['private_root']).resolve()
        self._orig_resolve=socket.getaddrinfo
        # Narrow, read-only stdlib metadata operation. This is not a general
        # subprocess allowlist, shell permission or a network bypass.
        self._platform_code = None
        stdlib = Path(sysconfig.get_path('stdlib')).resolve()
        processor = getattr(platform, '_Processor', None)
        func = getattr(processor, 'from_subprocess', None)
        code = getattr(func, '__code__', None)
        if code is not None and Path(code.co_filename).resolve() == stdlib / 'platform.py':
            self._platform_code = code
        self._uname_paths = set()
        for candidate in ('/usr/bin/uname', '/bin/uname'):
            try:
                resolved = Path(candidate).resolve(strict=True)
                st = resolved.stat()
                if stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022 and os.access(resolved, os.X_OK):
                    self._uname_paths.add(str(resolved))
            except OSError:
                pass
    def block(self,reason,host='',port=None,**metadata):
        event(self.log,'egress_blocked',role=self.role,reason=reason,host=str(host),port=port,
              request_id=current_request_id(),**metadata)
        raise EgressBlocked(reason)
    def host_allowed(self,host,port):
        host=str(host).strip('[]').lower()
        try: loop=ipaddress.ip_address(host).is_loopback
        except ValueError: loop=(host=='localhost')
        if loop: return port in self.local
        if self.role!='backend': return False
        if (host,port) in self.host_ports: return True
        if port==443 and host in self.https: return True
        # Neo4j routing / MongoDB SRV hosts are server-discovered dependency nodes.
        if port == 7687 and host.endswith('.databases.neo4j.io'): return True
        if port == 27017 and host.endswith('.mongodb.net'): return True
        return False
    def resolve(self,host,port,*a,**kw):
        # getaddrinfo for local binds; connect is checked independently.
        if host is None: return self._orig_resolve(host,port,*a,**kw)
        try: pn=int(port)
        except (TypeError,ValueError): pn={'http':80,'https':443}.get(port,0)
        hs=host.decode() if isinstance(host,bytes) else str(host)
        if hs in {'127.0.0.1','::1','localhost'} and pn==self.bind_port:
            # Allow resolving our listen address, but never add it to outbound destinations.
            return self._orig_resolve(host,port,*a,**kw)
        if not self.host_allowed(hs,pn): self.block('DNS destination not permitted',hs,pn)
        result=self._orig_resolve(host,port,*a,**kw)
        for row in result:
            if row[4] and isinstance(row[4],tuple): self.allowed_ips.add((str(row[4][0]),pn))
        return result
    def check_url(self,url):
        u=urlparse(str(url)); port=u.port or (443 if u.scheme=='https' else 80)
        if u.scheme not in {'http','https'} or not self.host_allowed(u.hostname,port):
            self.block('HTTP destination not permitted',u.hostname,port)
        return u.hostname,port,u.path
    def _caller_stack(self):
        # Do not inspect locals or source text: either may contain credentials.
        records = []
        frame = sys._getframe(1)
        try:
            for _ in range(24):
                if frame is None:
                    break
                code = frame.f_code
                if code.co_filename != __file__:
                    records.append({'file': Path(code.co_filename).name,
                                    'function': code.co_name, 'line': frame.f_lineno})
                frame = frame.f_back
        finally:
            del frame
        return records

    def _from_stdlib_processor(self):
        if self._platform_code is None:
            return False
        frame = sys._getframe(1)
        try:
            for _ in range(24):
                if frame is None:
                    break
                if frame.f_code is self._platform_code:
                    return True
                frame = frame.f_back
        finally:
            del frame
        return False

    def _subprocess_details(self,name,args):
        executable = args[0] if args else ''
        argv = args[1] if name in {'subprocess.Popen','os.exec','os.posix_spawn'} and len(args)>1 else []
        if not isinstance(argv,(list,tuple)):
            argv = []
        # Deliberately omit argument values, environment and working directory.
        exe_text = os.fsdecode(executable) if isinstance(executable,(str,bytes,os.PathLike)) else '<unknown>'
        base = Path(exe_text).name
        safe_base = base if re.fullmatch(r'[A-Za-z0-9_.+-]{1,64}',base) and not base.startswith(('sk-','eyJ')) else '<redacted>'
        options = []
        for value in argv[1:]:
            if not isinstance(value,str):
                continue
            flag = value.split('=',1)[0]
            if re.fullmatch(r'--?[A-Za-z][A-Za-z-]{0,32}',flag):
                options.append(flag)
        return {'audit_event':name,'executable_basename':safe_base,
                'argument_count':len(argv),'option_names':options[:16],
                'cwd_supplied':len(args)>2 and args[2] is not None,
                'environment_supplied':len(args)>3 and args[3] is not None,
                'caller_stack':self._caller_stack()}

    def _allow_platform_uname(self,name,args):
        if self.role not in {'backend','proxy'} or name != 'subprocess.Popen' or len(args)!=4:
            return False
        executable,argv,cwd,env = args
        if not isinstance(argv,(list,tuple)) or len(argv)!=2 or argv[1]!='-p' or cwd is not None or env is not None:
            return False
        if not isinstance(executable,str) or argv[0]!=executable or not self._from_stdlib_processor():
            return False
        # No shell; only the two literal arguments used by CPython platform.
        if executable not in {'uname','/usr/bin/uname','/bin/uname'}:
            return False
        resolved = shutil.which(executable)
        if resolved is None:
            return False
        try:
            path = Path(resolved).resolve(strict=True)
            st = path.stat()
            if str(path) not in self._uname_paths or st.st_uid != 0 or st.st_mode & 0o022 or not stat.S_ISREG(st.st_mode):
                return False
        except OSError:
            return False
        event(self.log,'subprocess_allowed',role=self.role,request_id=current_request_id(),
              classification='stdlib_platform_uname_p',executable=str(path),
              reviewed_arguments=['-p'],caller_stack=self._caller_stack(),
              scope='read-only host metadata; no general subprocess permission')
        return True

    def audit(self,name,args):
        if name=='socket.connect':
            sock,address=args
            if isinstance(address,tuple):
                host,port=str(address[0]),int(address[1])
                if (host,port) in self.allowed_ips or self.host_allowed(host,port): return
                # DNS transport used by SRV lookup is not an HTTP/model endpoint.
                if port==53: return
                self.block('TCP destination not permitted',host,port)
            elif isinstance(address,(str,bytes)):
                path=Path(os.fsdecode(address)).resolve()
                if not path.is_relative_to(self.private): self.block('Unix socket outside private state')
        elif name=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)):
            try: path=Path(os.fsdecode(args[0])).absolute().resolve()
            except (OSError,ValueError): return
            if any(path.is_relative_to(root) for root in self.production_roots):
                mode=args[1]; flags=args[2]
                writing=(isinstance(mode,str) and any(x in mode for x in 'wa+')) or bool(isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
                if writing or path.suffix in {'.py','.pyc','.pyo'} or path.name.startswith('.env'):
                    event(self.log,'production_file_access_blocked',role=self.role,path=str(path),write=writing)
                    raise Refuse('Production source/config access prohibited in shadow process')
        elif name in {'subprocess.Popen','os.system','os.exec','os.posix_spawn'}:
            if self._allow_platform_uname(name,args):
                return
            self.block('Unreviewed subprocess launch prohibited in shadow service',
                       **self._subprocess_details(name,args))

class SourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self,root,log):
        self.root=Path(root).resolve(); self.log=log
        self.prefixes={'api','app','agents','core','tools','database','schemas','models','chatgpt'}
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] not in self.prefixes: return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec is None: return None
        paths=[]
        if spec.origin and spec.origin not in {'built-in','frozen'}: paths.append(spec.origin)
        paths.extend(spec.submodule_search_locations or [])
        if not paths or any(not Path(p).resolve().is_relative_to(self.root) for p in paths):
            event(self.log,'import_origin_blocked',module=fullname)
            raise ImportError('Shadow module resolved outside copied source: '+fullname)
        return spec

def current_request_id():
    mod=sys.modules.get('core.observability')
    if mod:
        try:
            value=mod.REQUEST_ID.get()
            if value and value!='-': return str(value)
        except Exception: pass
    value=TRACE_ID.get()
    if value: return value
    from proxy_trace import current_request_id as proxy_request_id
    return proxy_request_id()

def install(cfg: dict):
    global _POLICY
    if _POLICY is not None: raise Refuse('Guard already installed')
    policy=Policy(cfg); _POLICY=policy
    if cfg['role']=='proxy':
        from proxy_trace import install as install_proxy_observer
        install_proxy_observer(cfg['audit_log'])
    socket.getaddrinfo=policy.resolve
    sys.addaudithook(policy.audit)
    if cfg['role']=='backend': sys.meta_path.insert(0,SourceFinder(cfg['shadow_root'],cfg['audit_log']))
    # Import HTTP packages before BLINK imports; wrappers stay inside this child.
    import httpx
    sync_send=httpx.Client.send; async_send=httpx.AsyncClient.send
    def prepare(request):
        host,port,path=policy.check_url(request.url)
        rid=current_request_id()
        if port in cfg.get('llm_ports',[]) and rid: request.headers['X-Request-ID']=rid
        meta={'request_id':rid,'role':cfg['role'],'library':'httpx',
                'host':host,'port':port,'path':path.split('?')[0] if port in cfg.get('llm_ports',[]) else '<dependency>',
                'model_http':port in cfg.get('llm_ports',[])}
        if cfg['role']=='proxy' and meta['model_http'] and path=='/v1/chat/completions':
            from native_schema import wire_metadata
            try: meta['native_wire']=wire_metadata(request.content)
            except Exception: meta['native_wire']={'status':'unavailable'}
            from proxy_trace import context_metadata
            meta.update(context_metadata())
            from provider_strategy import outgoing_metadata
            try: meta['endpoint_wire']=outgoing_metadata(request.content)
            except Exception: meta['endpoint_wire']={'status':'unavailable'}
        return meta
    @functools.wraps(sync_send)
    def sync(self,request,*args,**kwargs):
        meta=prepare(request); start=time.perf_counter(); status=None; error=None
        try:
            r=sync_send(self,request,*args,**kwargs); status=r.status_code; return r
        except Exception as exc: error=type(exc).__name__; raise
        finally: event(policy.log,'dependency_http',**meta,duration_ms=(time.perf_counter()-start)*1000,status=status,error=error)
    @functools.wraps(async_send)
    async def async_(self,request,*args,**kwargs):
        meta=prepare(request); start=time.perf_counter(); status=None; error=None
        try:
            r=await async_send(self,request,*args,**kwargs); status=r.status_code; return r
        except Exception as exc: error=type(exc).__name__; raise
        finally: event(policy.log,'dependency_http',**meta,duration_ms=(time.perf_counter()-start)*1000,status=status,error=error)
    httpx.Client.send=sync; httpx.AsyncClient.send=async_
    try:
        import requests
        send=requests.Session.send
        @functools.wraps(send)
        def requests_send(self,request,*args,**kwargs):
            host,port,path=policy.check_url(request.url); rid=current_request_id()
            if port in cfg.get('llm_ports',[]) and rid: request.headers['X-Request-ID']=rid
            wire={}
            if cfg['role']=='proxy' and port in cfg.get('llm_ports',[]) and path=='/v1/chat/completions':
                from native_schema import wire_metadata
                try: wire['native_wire']=wire_metadata(request.body)
                except Exception: wire['native_wire']={'status':'unavailable'}
                from proxy_trace import context_metadata
                wire.update(context_metadata())
                from provider_strategy import outgoing_metadata
                try: wire['endpoint_wire']=outgoing_metadata(request.body)
                except Exception: wire['endpoint_wire']={'status':'unavailable'}
            start=time.perf_counter(); status=None; error=None
            try:
                r=send(self,request,*args,**kwargs); status=r.status_code; return r
            except Exception as exc: error=type(exc).__name__; raise
            finally:
                event(policy.log,'dependency_http',role=cfg['role'],library='requests',request_id=rid,host=host,port=port,
                      path=path if port in cfg.get('llm_ports',[]) else '<dependency>',model_http=port in cfg.get('llm_ports',[]),
                      duration_ms=(time.perf_counter()-start)*1000,status=status,error=error,**wire)
        requests.Session.send=requests_send
    except ImportError: pass
    if cfg['role']=='backend':
        import openai
        for cls in (openai.OpenAI,openai.AsyncOpenAI):
            original=cls.__init__
            def factory(init):
                @functools.wraps(init)
                def forced(self,*args,**kwargs):
                    # Also defeats hard-coded API keys/base_url passed by older modules.
                    kwargs['api_key']=cfg['local_key']; kwargs['base_url']=cfg['compat_base']
                    kwargs.pop('websocket_base_url',None)
                    init(self,*args,**kwargs)
                return forced
            cls.__init__=factory(original)
    event(policy.log,'guard_installed',role=cfg['role'],policy='loopback_llm_and_explicit_dependencies_only',
          native_code_sandbox=False)

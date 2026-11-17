"""Loopback-only, bounded photo inference; uploaded images stay in memory."""
from __future__ import annotations
import base64,hashlib,io,json,logging,os,subprocess,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from PIL import Image,UnidentifiedImageError
from skin_vlm import SkinVLM
from core import validate

ROOT=Path(__file__).resolve().parent
MODEL_PATH=Path(os.getenv('PHOTO_MODEL_PATH',str(ROOT/'model')))
SAMPLE_PATH=Path(os.getenv('PHOTO_PUBLIC_SAMPLE_PATH',str(ROOT/'data/public_acne_ccby4.jpg')))
ADAPTER_PATH=os.getenv('PHOTO_ADAPTER_PATH','').strip() or None
MAX_BYTES=5*1024*1024
ENGINE=None
STATE={'ready':False,'failed':False,'warmup_complete':False}
LOCK=threading.Lock()
EXPECTED_NFS='192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11'
PUBLIC_SAMPLE_SHA256='b938b0940de6722e34a9c7d13dfe2186d1b733900b692a6df9cb41a814096510'
DIAGNOSTIC_FIELDS=('generated_tokens','eos_seen','eos_at_end','token_cap_reached',
    'time_budget_reached','generate_seconds','synchronize_seconds','valid_output')
PUBLIC_RESULT_FIELDS={'status','source','requires_user_confirmation','eligible_for_auto_save',
    'severity','observation','quality','error','inference_seconds','input_source'}

def verified_public_sample():
    # This known CC-BY4 file is a startup workload, never evaluation gold.
    if SAMPLE_PATH.name!='public_acne_ccby4.jpg' or not SAMPLE_PATH.is_file():raise ValueError('Public warmup sample missing')
    metadata=json.loads((SAMPLE_PATH.parent/'public_image_attribution.json').read_text())
    if (metadata.get('file')!=SAMPLE_PATH.name or metadata.get('license')!='CC-BY-4.0'
            or metadata.get('license_url')!='https://creativecommons.org/licenses/by/4.0/'
            or metadata.get('source_url')!='https://commons.wikimedia.org/wiki/File:0601_Acne_Vulgaris.jpg'
            or metadata.get('author')!='Dr.Gandikota Raghurama Rao' or metadata.get('modified') is not False
            or SAMPLE_PATH.stat().st_size>MAX_BYTES or hashlib.sha256(SAMPLE_PATH.read_bytes()).hexdigest()!=PUBLIC_SAMPLE_SHA256):
        raise ValueError('Public warmup provenance or bytes invalid')
    return SAMPLE_PATH

def analyze_internal(engine,*,sample=None,data=None,note=''):
    try:
        return engine.analyze(sample,note) if sample is not None else engine.analyze_bytes(data,note)
    finally:
        diagnostics=getattr(engine,'last_diagnostics',None)
        if isinstance(diagnostics,dict):
            safe={key:diagnostics[key] for key in DIAGNOSTIC_FIELDS if type(diagnostics.get(key)) in {bool,int,float}}
            print('PHOTO_INFERENCE_DIAGNOSTICS '+json.dumps(safe,allow_nan=False),flush=True)

def successful_warmup(result,engine):
    if not isinstance(result,dict):return False
    try:observation=validate(result['observation'])
    except (KeyError,ValueError,TypeError):return False
    expected='needs_confirmation' if observation['image_scope'] in {'face','skin_closeup'} else 'needs_retake'
    return (result.get('status')==expected and result.get('requires_user_confirmation') is True
        and result.get('eligible_for_auto_save') is False
        and getattr(engine,'last_diagnostics',{}).get('generated_tokens',0)>0)

def require_persistent_mount():
    persistent=Path('/mnt/goodagents')
    mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(persistent)],text=True))['filesystems'][0]
    if (mount['target']!=str(persistent) or mount['fstype'] not in {'nfs','nfs4'}
            or mount.get('source')!=EXPECTED_NFS or 'rw' not in mount.get('options','').split(',')):
        raise SystemExit('Expected writable school NFS mount missing')
    return persistent

def warm():
    global ENGINE
    STATE.update(ready=False,failed=False,warmup_complete=False)
    try:
        sample=verified_public_sample()
        with LOCK:
            engine=SkinVLM(str(MODEL_PATH),ADAPTER_PATH)
            for _ in range(2):
                try:result=analyze_internal(engine,sample=sample)
                except Exception as exc:
                    logging.warning('Photo warmup inference failed (%s)',type(exc).__name__)
                    continue
                if successful_warmup(result,engine):break
            else:raise RuntimeError('Public warmup did not yield a valid model envelope')
            ENGINE=engine
            STATE.update(ready=True,warmup_complete=True)
        print('PHOTO_MODEL_READY',flush=True)
    except Exception as exc:
        ENGINE=None
        STATE.update(ready=False,failed=True,warmup_complete=False)
        logging.error('Photo model could not start (%s)',type(exc).__name__)

class DeadlineReader(io.RawIOBase):
    """A total read deadline, including clients that keep trickling bytes."""
    def __init__(self,connection,deadline):
        super().__init__()
        self.connection=connection;self.deadline=deadline
    def readable(self):return True
    def readinto(self,buffer):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Request read deadline exceeded')
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)

class PhotoHTTPServer(ThreadingHTTPServer):
    """Bound accepted sockets before parsing headers or creating worker threads."""
    daemon_threads=True
    request_queue_size=8

    def __init__(self,address,handler=None,*,max_connections=8,header_timeout=10,body_timeout=20):
        if max_connections<1 or header_timeout<=0 or body_timeout<=0:raise ValueError('Invalid HTTP resource limits')
        self._connections=threading.BoundedSemaphore(max_connections)
        self.header_timeout=header_timeout;self.body_timeout=body_timeout
        super().__init__(address,handler or Handler)

    def get_request(self):
        connection,address=super().get_request()
        connection.settimeout(self.header_timeout)
        return connection,address

    def process_request(self,connection,address):
        if not self._connections.acquire(blocking=False):
            try:
                connection.settimeout(.2)
                data=b'{"error":"connection_limit"}'
                connection.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data)
            except OSError:pass
            finally:self.shutdown_request(connection)
            return
        try:super().process_request(connection,address)
        except BaseException:
            self._connections.release();raise

    def process_request_thread(self,connection,address):
        try:super().process_request_thread(connection,address)
        finally:self._connections.release()

    def handle_error(self,request,address):
        # Tracebacks can include attacker-controlled request lines or content.
        logging.error('Photo HTTP request failed')

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.rfile.close()
        self.deadline_reader=DeadlineReader(self.connection,time.monotonic()+self.server.header_timeout)
        self.rfile=io.BufferedReader(self.deadline_reader)

    def log_message(self,*args):
        pass  # Do not put images, descriptions, or session information in access logs.

    def reply(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode()
        self.close_connection=True
        try:
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Connection','close')
            self.end_headers()
            self.wfile.write(data)
        except OSError:pass

    def do_GET(self):
        if self.path!='/health':return self.reply(404,{'error':'not_found'})
        return self.reply(200,{'ready':STATE['ready'],'failed':STATE['failed'],'busy':LOCK.locked(),
            'warmup_complete':STATE.get('warmup_complete',False)})

    def do_POST(self):
        if self.path!='/analyze':return self.reply(404,{'error':'not_found'})
        self.connection.settimeout(self.server.body_timeout)
        self.deadline_reader.deadline=time.monotonic()+self.server.body_timeout
        lengths=self.headers.get_all('Content-Length',[])
        if len(lengths)!=1 or self.headers.get_all('Transfer-Encoding'):
            return self.reply(400,{'error':'invalid_request_framing'})
        if not STATE['ready']:return self.reply(503,{'error':'model_starting'})
        if not LOCK.acquire(blocking=False):return self.reply(429,{'error':'busy'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_BYTES:return self.reply(413,{'error':'image_too_large'})
            raw=self.rfile.read(length)
            if len(raw)!=length:return self.reply(400,{'error':'incomplete_body'})
            body=json.loads(raw)
            if not isinstance(body,dict):return self.reply(400,{'error':'invalid_body'})
            note=body.get('note','')
            if not isinstance(note,str) or len(note)>500:return self.reply(400,{'error':'invalid_note'})
            if body.get('sample') is True:
                if not SAMPLE_PATH.is_file():return self.reply(503,{'error':'sample_unavailable'})
                result=analyze_internal(ENGINE,sample=SAMPLE_PATH,note=note)
                result['input_source']='public_sample'
            else:
                data=body.get('image_base64')
                if not isinstance(data,str):return self.reply(400,{'error':'missing_image'})
                decoded=base64.b64decode(data,validate=True)
                if not 0<len(decoded)<=3*1024*1024:return self.reply(413,{'error':'image_too_large'})
                result=analyze_internal(ENGINE,data=decoded,note=note)
                result['input_source']='user_photo'
            # Keep internal diagnostics/raw fields private even if a future
            # engine accidentally puts them directly into its result object.
            result={key:value for key,value in result.items() if key in PUBLIC_RESULT_FIELDS}
            return self.reply(200,result)
        except (ValueError,TypeError,UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning,OSError):
            return self.reply(400,{'error':'invalid_image'})
        except Exception as exc:
            logging.error('Photo inference failed (%s)',type(exc).__name__)
            return self.reply(503,{'error':'inference_unavailable'})
        finally:LOCK.release()

if __name__=='__main__':
    import resource
    # Native decoder/GPU failures must not dump uploaded photos into core files.
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    host=os.getenv('PHOTO_MODEL_BIND_HOST','127.0.0.1')
    if host!='127.0.0.1':raise SystemExit('Photo inference must bind to 127.0.0.1 and use SSH forwarding')
    persistent=require_persistent_mount()
    if not MODEL_PATH.resolve().is_relative_to(persistent):raise SystemExit('Model path is outside the persistent mount')
    from download_model import REPO,REVISION,WEIGHTS,file_hash
    metadata=json.loads((MODEL_PATH/'artifact-metadata.json').read_text())
    if metadata.get('repo_id')!=REPO or metadata.get('revision')!=REVISION or metadata.get('verified') is not True:
        raise SystemExit('Pinned official model verification metadata missing')
    for name,expected in WEIGHTS.items():
        if file_hash(MODEL_PATH/name)!=expected:raise SystemExit('Official model checksum mismatch: '+name)
    if ADAPTER_PATH:
        adapter=Path(ADAPTER_PATH)
        if not adapter.resolve().is_relative_to(persistent):raise SystemExit('Adapter is outside the persistent mount')
        if os.getenv('PHOTO_ALLOW_RESEARCH_ADAPTER')!='1':
            raise SystemExit('Research adapters require an explicit research-mode configuration; no automatic deployment')
    threading.Thread(target=warm,daemon=True).start()
    PhotoHTTPServer((
        host,
        int(os.getenv('PHOTO_MODEL_PORT','8025')),
    ),Handler).serve_forever()

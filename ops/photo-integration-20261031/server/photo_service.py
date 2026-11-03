"""Loopback-only, bounded, temporary-file photo inference service."""
from __future__ import annotations
import base64,json,logging,os,tempfile,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from PIL import UnidentifiedImageError
from skin_vlm import SkinVLM

ROOT=Path(__file__).resolve().parent
MAX_BYTES=5*1024*1024
ENGINE=None
STATE={'ready':False,'failed':False}
LOCK=threading.Lock()

def warm():
    global ENGINE
    try:
        ENGINE=SkinVLM(str(ROOT/'model'))
        STATE['ready']=True
        print('PHOTO_MODEL_READY',flush=True)
    except Exception:
        STATE['failed']=True
        logging.exception('Photo model could not start')

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass  # Do not put images, descriptions, or session information in access logs.

    def reply(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass

    def do_GET(self):
        if self.path!='/health':return self.reply(404,{'error':'not_found'})
        return self.reply(200,{'ready':STATE['ready'],'failed':STATE['failed'],'busy':LOCK.locked()})

    def do_POST(self):
        if self.path!='/analyze':return self.reply(404,{'error':'not_found'})
        self.connection.settimeout(20)
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
                result=ENGINE.analyze(ROOT/'data/public_acne_ccby4.jpg',note)
                result['input_source']='public_sample'
            else:
                data=body.get('image_base64')
                if not isinstance(data,str):return self.reply(400,{'error':'missing_image'})
                decoded=base64.b64decode(data,validate=True)
                if not 0<len(decoded)<=3*1024*1024:return self.reply(413,{'error':'image_too_large'})
                with tempfile.TemporaryDirectory(prefix='blink-photo-') as directory:
                    image=Path(directory)/'upload';image.write_bytes(decoded)
                    result=ENGINE.analyze(image,note)
                result['input_source']='user_photo'
            # Raw model output is an internal debugging artifact, not a product response.
            result.pop('raw_output',None)
            result.pop('image_sha256',None)
            return self.reply(200,result)
        except (ValueError,TypeError,UnidentifiedImageError,OSError):
            return self.reply(400,{'error':'invalid_image'})
        except Exception:
            logging.exception('Photo inference failed')
            return self.reply(503,{'error':'inference_unavailable'})
        finally:LOCK.release()

if __name__=='__main__':
    threading.Thread(target=warm,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1',8025),Handler).serve_forever()

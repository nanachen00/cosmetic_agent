"""Candidate BGE API: fixed local weights, FP32 CLS/Normalize and bounded IO.

Compatible single-input endpoint; unlike the historical service, a batch returns
every input. Candidate validation is required before changing the live 8002 API.
"""
import argparse,hashlib,json,logging,math,os,resource,subprocess,threading,time
from pathlib import Path
from bounded_http import BoundedHTTPServer,BoundedHandler,RequestError

ROOT=Path('/mnt/goodagents')
EXPECTED_NFS='192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11'
REVISION='5617a9f61b028005a4858fdac845db406aefb181'
WEIGHT_SHA256='b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38'
SNAPSHOT=ROOT/'hf-cache/hub/models--BAAI--bge-m3/snapshots'/REVISION
MAX_BODY=512*1024
MAX_BATCH=8
MAX_TEXT_CHARS=32768
MAX_TOTAL_CHARS=65536
MAX_PADDED_TOKENS=8192
DIMENSION=1024
REQUIRED_FILES={'config.json','config_sentence_transformers.json','modules.json','sentence_bert_config.json','1_Pooling/config.json',
    'tokenizer.json','tokenizer_config.json','special_tokens_map.json','sentencepiece.bpe.model','pytorch_model.bin'}


def file_hash(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        while chunk:=source.read(8*1024*1024):value.update(chunk)
    return value.hexdigest()


def require_mount():
    mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(ROOT)],text=True))['filesystems'][0]
    if (mount.get('target')!=str(ROOT) or mount.get('fstype') not in {'nfs','nfs4'}
            or mount.get('source')!=EXPECTED_NFS or 'rw' not in mount.get('options','').split(',')):
        raise RuntimeError('Expected writable school NFS mount missing')
    return mount


def verify_snapshot(snapshot,manifest):
    if not snapshot.resolve().is_relative_to(ROOT) or snapshot.name!=REVISION:raise ValueError('Pinned persistent snapshot required')
    if manifest.get('repo_id')!='BAAI/bge-m3' or manifest.get('revision')!=REVISION:raise ValueError('BGE revision differs')
    if set(manifest.get('files_sha256',{}))!=REQUIRED_FILES:raise ValueError('Incomplete BGE snapshot verification')
    if manifest['files_sha256'].get('pytorch_model.bin')!=WEIGHT_SHA256:raise ValueError('Unrecognized official BGE weights')
    for name,digest in manifest['files_sha256'].items():
        path=snapshot/name
        if not path.resolve().is_relative_to(ROOT) or file_hash(path)!=digest:raise ValueError('BGE snapshot checksum differs')
    # Auto-loaders otherwise prefer an unreviewed safetensors/adapter or token overlay.
    for name in ('model.safetensors','model.safetensors.index.json','pytorch_model.bin.index.json','adapter_config.json','added_tokens.json'):
        if (snapshot/name).exists():raise ValueError('Unreviewed BGE loader override present')
    modules=json.loads((snapshot/'modules.json').read_text())
    expected=[('', 'sentence_transformers.models.Transformer'),('1_Pooling','sentence_transformers.models.Pooling'),('2_Normalize','sentence_transformers.models.Normalize')]
    if [(row.get('path'),row.get('type')) for row in modules]!=expected:raise ValueError('Unapproved BGE module configuration')
    pooling=json.loads((snapshot/'1_Pooling/config.json').read_text())
    if pooling.get('pooling_mode_cls_token') is not True or any(value for key,value in pooling.items() if key.startswith('pooling_mode_') and key!='pooling_mode_cls_token'):
        raise ValueError('BGE CLS pooling differs')
    if json.loads((snapshot/'sentence_bert_config.json').read_text()).get('max_seq_length')!=8192:raise ValueError('BGE sequence length differs')


def texts_for(body):
    if not isinstance(body,dict):raise RequestError(400,'invalid_request')
    model=body.get('model','bge-m3')
    if not isinstance(model,str) or model not in {'bge-m3','BAAI/bge-m3'}:raise RequestError(400,'unsupported_model')
    if body.get('encoding_format','float')!='float':raise RequestError(400,'unsupported_encoding_format')
    if 'dimensions' in body and (type(body['dimensions']) is not int or body['dimensions']!=DIMENSION):raise RequestError(400,'unsupported_dimensions')
    values=body.get('input');values=[values] if isinstance(values,str) else values
    if not isinstance(values,list) or not 1<=len(values)<=MAX_BATCH:raise RequestError(400,'invalid_input_batch')
    if any(not isinstance(value,str) or not value.strip() for value in values):raise RequestError(400,'input_must_contain_text')
    if any(len(value)>MAX_TEXT_CHARS for value in values) or sum(map(len,values))>MAX_TOTAL_CHARS:raise RequestError(413,'input_too_long')
    return [value.strip() for value in values]


class BGEModel:
    def __init__(self,snapshot):
        import torch
        from sentence_transformers import SentenceTransformer
        if not torch.version.hip or not torch.cuda.is_available() or 'MI300X' not in torch.cuda.get_device_name(0):raise RuntimeError('MI300X required')
        self.torch=torch
        self.model=SentenceTransformer(str(snapshot),device='cuda:0',local_files_only=True,trust_remote_code=False,
            model_kwargs={'dtype':torch.float32,'use_safetensors':False},tokenizer_kwargs={'local_files_only':True,'trust_remote_code':False})
        self.model.float().eval();self.model.max_seq_length=8192
        if self.model.get_sentence_embedding_dimension()!=DIMENSION or next(self.model.parameters()).dtype!=torch.float32:raise RuntimeError('BGE dtype/dimension differs')
    def encode(self,texts):
        encoded=self.model.tokenizer(texts,add_special_tokens=True,truncation=False,padding=False,return_attention_mask=False,return_token_type_ids=False)
        lengths=[len(values) for values in encoded['input_ids']]
        if max(lengths)>8192 or max(lengths)*len(texts)>MAX_PADDED_TOKENS:raise RequestError(413,'input_token_budget_exceeded')
        with self.torch.inference_mode():
            vectors=self.model.encode(texts,batch_size=len(texts),normalize_embeddings=True,show_progress_bar=False,precision='float32',convert_to_numpy=True).tolist()
        return vectors,sum(lengths)


class Handler(BoundedHandler):
    def do_GET(self):
        if self.path=='/healthz':return self.reply(200,{'ready':True,'status':'ready','model':'BAAI/bge-m3','revision':REVISION,'dimension':DIMENSION,'busy':self.server.busy})
        if self.path=='/v1/models':return self.reply(200,{'object':'list','data':[{'id':'bge-m3','object':'model'}]})
        return self.reply(404,{'error':'not_found'})
    def do_POST(self):
        if self.path!='/v1/embeddings':return self.reply(404,{'error':'not_found'})
        acquired=False
        try:
            texts=texts_for(self.read_json(MAX_BODY))
            acquired=self.server.encode_slot.acquire(timeout=self.server.queue_timeout)
            if not acquired:return self.reply(429,{'error':'embedding_service_busy'},{'Retry-After':'1'})
            self.server.busy=True;start=time.perf_counter()
            vectors,tokens=self.server.engine.encode(texts)
            if len(vectors)!=len(texts) or any(len(row)!=DIMENSION or not all(math.isfinite(v) for v in row) or abs(math.sqrt(sum(v*v for v in row))-1)>1e-5 for row in vectors):
                raise RuntimeError('Invalid embedding vectors')
            return self.reply(200,{'object':'list','data':[{'object':'embedding','embedding':row,'index':i} for i,row in enumerate(vectors)],
                'model':'bge-m3','usage':{'prompt_tokens':tokens,'total_tokens':tokens},'performance':{'duration_ms':round((time.perf_counter()-start)*1000,2)}})
        except RequestError as error:return self.reply(error.status,{'error':error.code})
        except Exception as error:
            logging.error('BGE request failed (%s)',type(error).__name__)
            return self.reply(503,{'error':'embedding_unavailable'})
        finally:
            if acquired:self.server.busy=False;self.server.encode_slot.release()


def server_for(address,engine,**kwargs):
    server=BoundedHTTPServer(address,Handler,**kwargs)
    server.engine=engine;server.encode_slot=threading.BoundedSemaphore(1);server.queue_timeout=.5;server.busy=False
    return server


def warmup(engine):
    # Finish a real GPU forward before accepting any health/inference request.
    vectors,_=engine.encode(['服務啟動預熱：保濕與乾燥。'])
    if len(vectors)!=1 or len(vectors[0])!=DIMENSION or not all(math.isfinite(value) for value in vectors[0]) or abs(math.sqrt(sum(value*value for value in vectors[0]))-1)>1e-5:
        raise RuntimeError('BGE startup warmup did not produce a finite normalized vector')
    print('BGE_WARMUP_OK',flush=True)


def main():
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8027);args=parser.parse_args()
    if not 1<=args.port<=65535:raise SystemExit('Invalid port')
    require_mount()
    for name in ('HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE'):os.environ[name]='1'
    manifest=json.loads(Path(__file__).with_name('bge-m3.secure.manifest.json').read_text());verify_snapshot(SNAPSHOT,manifest)
    engine=BGEModel(SNAPSHOT)
    warmup(engine)
    server=server_for(('127.0.0.1',args.port),engine)
    print('BGE_READY',flush=True);server.serve_forever()


if __name__=='__main__':main()

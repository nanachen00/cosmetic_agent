"""Real socket tests without a GPU or private photos."""
import base64,contextlib,hashlib,http.client,io,json,socket,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import photo_service as service
import install_photo_gpu_runtime as installer
from skin_vlm import open_image_bytes


class FakeEngine:
    def __init__(self):
        self.entered=threading.Event();self.release=threading.Event();self.fail=False;self.last_bytes=None;self.extra_result={}
    def analyze_bytes(self,data,note=''):
        self.last_bytes=data;self.entered.set();self.release.wait(2)
        if self.fail:raise RuntimeError('sensitive photo details must not escape')
        return dict(status='needs_confirmation',raw_output='private model output',image_sha256='private digest',**self.extra_result)


class PhotoHTTP(unittest.TestCase):
    def setUp(self):
        self.engine=FakeEngine();self.engine.release.set()
        self.patches=[patch.object(service,'ENGINE',self.engine),patch.object(service,'STATE',{'ready':True,'failed':False}),patch.object(service,'LOCK',threading.Lock())]
        for p in self.patches:p.start()
        self.server=service.PhotoHTTPServer(('127.0.0.1',0),max_connections=2,header_timeout=.15,body_timeout=.15)
        self.server.daemon_threads=False  # Join this test's handlers before restoring module state.
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start()
    def tearDown(self):
        self.engine.release.set();self.server.shutdown();self.server.server_close();self.thread.join(2)
        for p in reversed(self.patches):p.stop()
    def request(self,body=None,path='/analyze'):
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=2)
        try:
            if body is None:connection.request('GET',path)
            else:connection.request('POST',path,json.dumps(body),{'Content-Type':'application/json'})
            response=connection.getresponse();return response.status,json.loads(response.read())
        finally:connection.close()
    def upload(self):return {'image_base64':base64.b64encode(b'public synthetic bytes').decode()}
    def assert_unlocked(self):
        for _ in range(100):
            if not service.LOCK.locked():break
            time.sleep(.01)
        self.assertFalse(service.LOCK.locked());self.assertEqual(self.request(self.upload())[0],200)

    def test_upload_stays_in_memory_and_response_excludes_debug_data(self):
        self.engine.last_diagnostics=dict(generated_tokens=3,valid_output=True,private_text='must not escape')
        self.engine.extra_result=dict(diagnostics={'raw_text':'PRIVATE'},_diagnostics={'image':'PRIVATE'},
            last_diagnostics={'user':'PRIVATE'},generated_tokens=3,raw_text='PRIVATE')
        with patch('tempfile.TemporaryDirectory',side_effect=AssertionError('No disk image allowed')):
            status,result=self.request(self.upload())
        self.assertEqual(status,200);self.assertEqual(self.engine.last_bytes,b'public synthetic bytes')
        self.assertNotIn('raw_output',result);self.assertNotIn('image_sha256',result)
        self.assertNotIn('generated_tokens',result);self.assertNotIn('last_diagnostics',result)
        self.assertEqual(result,{'status':'needs_confirmation','input_source':'user_photo'})

    def test_invalid_body_and_inference_failure_release_gpu_slot(self):
        self.assertEqual(self.request({'image_base64':'!invalid!'})[0],400);self.assert_unlocked()
        self.engine.fail=True
        status,result=self.request(self.upload());self.assertEqual(status,503);self.assertNotIn('sensitive',json.dumps(result))
        self.engine.fail=False;self.assert_unlocked()

    def test_duplicate_length_and_chunked_framing_are_rejected(self):
        for headers in [b'Content-Length: 2\r\nContent-Length: 2',b'Content-Length: 2\r\nTransfer-Encoding: chunked']:
            with self.subTest(headers=headers):
                with socket.create_connection(self.server.server_address,timeout=2) as connection:
                    connection.sendall(b'POST /analyze HTTP/1.1\r\nHost: localhost\r\n'+headers+b'\r\n\r\n{}')
                    self.assertIn(b' 400 ',connection.recv(1024))
                self.assert_unlocked()

    def test_slow_headers_hit_connection_cap_then_expire(self):
        connections=[socket.create_connection(self.server.server_address,timeout=2) for _ in range(2)]
        try:
            for connection in connections:connection.sendall(b'GET /health HTTP/1.1\r\nHost:')
            self.assertEqual(self.request(path='/health')[0],503)
            time.sleep(.25)
            self.assertEqual(self.request(path='/health')[0],200)
        finally:
            for connection in connections:connection.close()

    def test_stalled_body_releases_inference_slot(self):
        with socket.create_connection(self.server.server_address,timeout=2) as connection:
            connection.sendall(b'POST /analyze HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{')
            self.assertIn(b' 400 ',connection.recv(1024))
        self.assert_unlocked()

    def test_trickling_headers_cannot_extend_total_deadline(self):
        connection=socket.create_connection(self.server.server_address,timeout=2)
        try:
            connection.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\nX-Slow: ')
            def trickle():
                try:
                    for _ in range(20):connection.sendall(b'a');time.sleep(.03)
                except OSError:pass
            sender=threading.Thread(target=trickle);sender.start()
            start=time.monotonic();response=connection.recv(1024)
            self.assertEqual(response,b'');self.assertLess(time.monotonic()-start,.4)
            sender.join(1)
            self.assertEqual(self.request(path='/health')[0],200)
        finally:connection.close()

    def test_trickling_body_cannot_extend_total_deadline(self):
        with socket.create_connection(self.server.server_address,timeout=2) as connection:
            connection.sendall(b'POST /analyze HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{')
            def trickle():
                try:
                    for _ in range(20):connection.sendall(b' ');time.sleep(.03)
                except OSError:pass
            sender=threading.Thread(target=trickle);sender.start()
            start=time.monotonic();response=connection.recv(1024)
            self.assertIn(b' 400 ',response);self.assertLess(time.monotonic()-start,.4)
            sender.join(1)
        self.assert_unlocked()

    def test_busy_inference_and_client_disconnect_do_not_leak_admission(self):
        self.engine.release.clear()
        with socket.create_connection(self.server.server_address,timeout=2) as connection:
            data=json.dumps(self.upload()).encode()
            connection.sendall(b'POST /analyze HTTP/1.1\r\nHost: localhost\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data)
            self.assertTrue(self.engine.entered.wait(1))
            self.assertEqual(self.request(self.upload())[0],429)
        self.engine.release.set()
        for _ in range(100):
            if not service.LOCK.locked():break
            time.sleep(.01)
        self.assert_unlocked()


class ImageDecoder(unittest.TestCase):
    def encoded(self,image,format,**kwargs):
        data=io.BytesIO();image.save(data,format=format,**kwargs);return data.getvalue()
    def test_supported_images_are_fully_decoded(self):
        for format in ['JPEG','PNG','WEBP']:
            image,quality=open_image_bytes(self.encoded(Image.new('RGB',(240,280),(140,120,100)),format))
            self.assertEqual(image.mode,'RGB');self.assertEqual(image.size,(240,280))
    def test_animated_and_transparent_images_are_rejected(self):
        transparent=Image.new('RGBA',(240,240),(1,2,3,0))
        with self.assertRaisesRegex(ValueError,'Transparent'):open_image_bytes(self.encoded(transparent,'PNG'))
        animated=self.encoded(Image.new('RGB',(240,240),'red'),'WEBP',save_all=True,append_images=[Image.new('RGB',(240,240),'blue')],duration=20)
        with self.assertRaisesRegex(ValueError,'Animated'):open_image_bytes(animated)
    def test_other_formats_and_truncated_images_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'format'):open_image_bytes(self.encoded(Image.new('RGB',(240,240)),'TIFF'))
        data=self.encoded(Image.new('RGB',(240,240)),'PNG')
        with self.assertRaises((ValueError,OSError)):open_image_bytes(data[:len(data)//2])


class PersistentMount(unittest.TestCase):
    def test_photo_and_installer_reject_wrong_volume_or_read_only_mount(self):
        valid=dict(target='/mnt/goodagents',fstype='nfs4',source=service.EXPECTED_NFS,options='rw,relatime')
        for module in [service,installer]:
            for changes in [{'source':'other:/volume'},{'options':'ro,relatime'},{'fstype':'ext4'},{'target':'/'}]:
                mount=dict(valid,**changes)
                with self.subTest(module=module.__name__,changes=changes),patch.object(module.subprocess,'check_output',return_value=json.dumps({'filesystems':[mount]})):
                    with self.assertRaisesRegex((SystemExit,RuntimeError),'writable school NFS'):
                        module.require_persistent_mount()


class StartupWarmup(unittest.TestCase):
    def valid(self):
        return dict(status='needs_confirmation',requires_user_confirmation=True,eligible_for_auto_save=False,
            observation=dict(image_scope='skin_closeup',observations=[],limitations=['光線會影響觀察。'],follow_up_question=''))

    def test_public_fixture_requires_bytes_and_attribution_not_a_private_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);sample=root/'public_acne_ccby4.jpg';sample.write_bytes(b'synthetic public fixture')
            metadata=root/'public_image_attribution.json'
            value=dict(file=sample.name,license='CC-BY-4.0',license_url='https://creativecommons.org/licenses/by/4.0/',
                source_url='https://commons.wikimedia.org/wiki/File:0601_Acne_Vulgaris.jpg',
                author='Dr.Gandikota Raghurama Rao',modified=False)
            metadata.write_text(json.dumps(value))
            with patch.object(service,'SAMPLE_PATH',sample),patch.object(service,'PUBLIC_SAMPLE_SHA256',hashlib.sha256(sample.read_bytes()).hexdigest()):
                self.assertEqual(service.verified_public_sample(),sample)
                value['license']='unknown';metadata.write_text(json.dumps(value))
                with self.assertRaises(ValueError):service.verified_public_sample()
                value['license']='CC-BY-4.0';metadata.write_text(json.dumps(value));sample.write_bytes(b'private replacement')
                with self.assertRaises(ValueError):service.verified_public_sample()
                sample.unlink()
                with self.assertRaises(ValueError):service.verified_public_sample()

    def test_ready_only_after_real_valid_forward_and_at_most_two_attempts(self):
        owner=self
        class Engine:
            last_diagnostics=None
            def __init__(self,*args):self.calls=0
            def analyze(self,*args):
                owner.assertFalse(service.STATE['ready']);owner.assertTrue(service.LOCK.locked());self.calls+=1
                self.last_diagnostics={'generated_tokens':3,'valid_output':self.calls==2}
                return {'status':'invalid_model_output'} if self.calls==1 else owner.valid()
        state={'ready':False,'failed':False}
        with patch.object(service,'verified_public_sample',return_value=Path('/public/fixture')), \
                patch.object(service,'SkinVLM',Engine),patch.object(service,'STATE',state), \
                patch.object(service,'LOCK',threading.Lock()),patch.object(service,'ENGINE',None), \
                contextlib.redirect_stdout(io.StringIO()):
            service.warm();self.assertTrue(state['ready']);self.assertTrue(state['warmup_complete']);self.assertFalse(state['failed'])
            self.assertEqual(service.ENGINE.calls,2)

    def test_missing_fixture_or_two_invalid_generations_never_become_ready(self):
        class Engine:
            last_diagnostics={'generated_tokens':400,'valid_output':False}
            def __init__(self,*args):self.calls=0
            def analyze(self,*args):self.calls+=1;return {'status':'invalid_model_output'}
        engine=Engine()
        with patch.object(service,'verified_public_sample',return_value=Path('/public/fixture')), \
                patch.object(service,'SkinVLM',return_value=engine),patch.object(service,'STATE',{}), \
                patch.object(service,'LOCK',threading.Lock()),patch.object(service,'ENGINE',None), \
                contextlib.redirect_stdout(io.StringIO()):
            service.warm();self.assertEqual(engine.calls,2);self.assertFalse(service.STATE['ready']);self.assertTrue(service.STATE['failed']);self.assertIsNone(service.ENGINE)
        with patch.object(service,'verified_public_sample',side_effect=ValueError('missing')), \
                patch.object(service,'SkinVLM',side_effect=AssertionError('no GPU load')),patch.object(service,'STATE',{}), \
                patch.object(service,'ENGINE',None):
            service.warm();self.assertFalse(service.STATE['ready']);self.assertTrue(service.STATE['failed'])

    def test_quality_retake_without_generation_is_not_a_real_warmup(self):
        result=self.valid();engine=type('Engine',(),{'last_diagnostics':{'generated_tokens':0}})()
        self.assertFalse(service.successful_warmup(result,engine))

    def test_internal_diagnostics_log_only_numeric_allowlist(self):
        engine=FakeEngine();engine.release.set()
        engine.last_diagnostics={'generated_tokens':3,'valid_output':True,'raw_output':'PRIVATE','image_sha256':'HASH','note':'USER'}
        output=io.StringIO()
        with contextlib.redirect_stdout(output):service.analyze_internal(engine,data=b'public fixture')
        self.assertIn('generated_tokens',output.getvalue())
        for private in ('PRIVATE','HASH','USER','raw_output','image_sha256','note'):self.assertNotIn(private,output.getvalue())

    def test_photo_and_installer_accept_only_confirmed_writable_volume(self):
        valid=dict(target='/mnt/goodagents',fstype='nfs4',source=service.EXPECTED_NFS,options='rw,relatime')
        for module in [service,installer]:
            with self.subTest(module=module.__name__),patch.object(module.subprocess,'check_output',return_value=json.dumps({'filesystems':[valid]})):
                self.assertTrue(module.require_persistent_mount())


if __name__=='__main__':unittest.main()

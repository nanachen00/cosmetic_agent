"""CPU and real-socket checks for the isolated BGE service; no GPU imports."""
import contextlib
import http.client
import importlib.util
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import bge_secure_service as service


class Engine:
    def __init__(self):
        self.calls = []
        self.failure = False
        self.invalid = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def encode(self, texts):
        self.calls.append(texts)
        self.entered.set()
        if self.block:
            self.release.wait(2)
        if self.failure:
            raise RuntimeError('private test input must not appear in response')
        vectors = [[0.0] * service.DIMENSION for _ in texts]
        for index, vector in enumerate(vectors):
            vector[index] = float('nan') if self.invalid else 1.0
        return vectors, len(texts)


class ServiceHTTP(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.server = service.server_for(('127.0.0.1', 0), self.engine,
                                        max_connections=2, header_timeout=.15, body_timeout=.15)
        self.server.queue_timeout = .04
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.address = self.server.server_address

    def tearDown(self):
        self.engine.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def post(self, body):
        connection = http.client.HTTPConnection(*self.address, timeout=2)
        try:
            connection.request('POST', '/v1/embeddings', json.dumps(body), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw(self, request):
        with socket.create_connection(self.address, timeout=2) as connection:
            connection.sendall(request)
            response = b''
            while chunk := connection.recv(65536):
                response += chunk
            return response

    def test_single_and_batch_return_every_input_in_order(self):
        self.assertEqual(self.post({'input': ' first '})[0], 200)
        status, payload = self.post({'model': 'bge-m3', 'input': [' first ', 'second', 'third']})
        self.assertEqual(status, 200)
        self.assertEqual([row['index'] for row in payload['data']], [0, 1, 2])
        self.assertEqual([row['embedding'][i] for i, row in enumerate(payload['data'])], [1, 1, 1])
        self.assertEqual(self.engine.calls, [['first'], ['first', 'second', 'third']])

    @unittest.skipUnless(importlib.util.find_spec('requests'), 'Run with the locked backend Python for existing-client integration')
    def test_existing_embedding_client_uses_candidate_api_unchanged(self):
        client_file = Path(__file__).resolve().parents[2] / 'cosmetic-agent/app/clients/embedding_client.py'
        spec = importlib.util.spec_from_file_location('existing_bge_client', client_file)
        client_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client_module)
        client = client_module.EmbeddingClient(base_url=f'http://127.0.0.1:{self.address[1]}/v1',
                                              model='bge-m3', timeout_seconds=1)
        try:
            vector = client.embed_query(' public comparison sentence ')
            self.assertEqual(len(vector), 1024)
            self.assertEqual(vector[0], 1.0)
            self.assertEqual(self.engine.calls, [['public comparison sentence']])
        finally:
            client._session.close()

    def test_invalid_inputs_do_not_reach_model(self):
        for body in ({'input': []}, {'input': ['']}, {'input': [[1]]}, {'input': ['a'] * 9},
                     {'input': 'a', 'model': []}, {'input': 'a', 'dimensions': True},
                     {'input': 'a', 'encoding_format': 'base64'}, ['not', 'an', 'object']):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0], 400)
        self.assertFalse(self.engine.calls)

    def test_text_and_aggregate_limits(self):
        self.assertEqual(self.post({'input': 'x' * 32769})[0], 413)
        self.assertEqual(self.post({'input': ['x' * 32768] * 3})[0], 413)
        self.assertFalse(self.engine.calls)

    def test_duplicate_content_length_and_transfer_encoding_are_rejected(self):
        for framing in (b'Content-Length: 2\r\nContent-Length: 2',
                        b'Content-Length: 2\r\nTransfer-Encoding: chunked',
                        b'Content-Length: +2'):
            result = self.raw(b'POST /v1/embeddings HTTP/1.1\r\nHost: local\r\n' + framing + b'\r\n\r\n{}')
            self.assertIn(b'400 Bad Request', result)
        self.assertFalse(self.engine.calls)

    def test_body_size_is_rejected_before_body_is_read(self):
        result = self.raw(b'POST /v1/embeddings HTTP/1.1\r\nHost: local\r\nContent-Length: 524289\r\n\r\n')
        self.assertIn(b'413', result)
        self.assertFalse(self.engine.calls)

    def test_body_timeout_releases_connection_slot(self):
        result = self.raw(b'POST /v1/embeddings HTTP/1.1\r\nHost: local\r\nContent-Length: 40\r\n\r\n{')
        self.assertIn(b'400', result)
        self.assertEqual(self.post({'input': 'after timeout'})[0], 200)

    def test_slow_header_and_connection_overload_are_bounded(self):
        with contextlib.ExitStack() as stack:
            connections = [stack.enter_context(socket.create_connection(self.address, timeout=2)) for _ in range(2)]
            for connection in connections:
                connection.sendall(b'POST /v1/embeddings HTTP/1.1\r\nHo')
            # Wait for both accepted sockets, without depending on thread ordering.
            deadline = time.monotonic() + .1
            while self.server.slots._value != 0 and time.monotonic() < deadline:
                time.sleep(.001)
            with socket.create_connection(self.address, timeout=2) as third:
                self.assertIn(b'503 Service Unavailable', third.recv(4096))
            time.sleep(.18)
            for connection in connections:
                self.assertEqual(connection.recv(4096), b'')
        self.assertEqual(self.post({'input': 'after timeout'})[0], 200)

    def test_gpu_admission_cleanup_after_failure_and_invalid_vectors(self):
        for attribute in ('failure', 'invalid'):
            setattr(self.engine, attribute, True)
            status, payload = self.post({'input': 'sensitive text'})
            self.assertEqual((status, payload), (503, {'error': 'embedding_unavailable'}))
            setattr(self.engine, attribute, False)
            self.assertEqual(self.post({'input': 'recovery'})[0], 200)
            # A complete HTTP response may arrive just before the handler's
            # finally executes; wait on the actual released admission slot.
            self.assertTrue(self.server.encode_slot.acquire(timeout=1))
            try:
                self.assertFalse(self.server.busy)
            finally:
                self.server.encode_slot.release()

    def test_gpu_overload_fails_fast_and_disconnection_releases(self):
        self.engine.block = True
        first = socket.create_connection(self.address, timeout=2)
        payload = json.dumps({'input': 'first'}).encode()
        first.sendall(b'POST /v1/embeddings HTTP/1.1\r\nHost: local\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        self.assertTrue(self.engine.entered.wait(1))
        self.assertEqual(self.post({'input': 'second'})[0], 429)
        first.close()
        self.engine.release.set()
        deadline = time.monotonic() + 1
        while self.server.busy and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(self.server.busy)
        self.assertEqual(self.post({'input': 'recovered'})[0], 200)


class TokenBudget(unittest.TestCase):
    def test_padding_cost_is_bounded_before_gpu_encode(self):
        model = service.BGEModel.__new__(service.BGEModel)
        class TokenizerModel:
            def tokenizer(self, texts, **kwargs):
                return {'input_ids': [list(range(4097)), [1]]}
            def encode(self, *args, **kwargs):
                raise AssertionError('Over-budget batch must not enter GPU')
        model.model = TokenizerModel()
        with self.assertRaises(service.RequestError) as result:
            model.encode(['long', 'short'])
        self.assertEqual(result.exception.status, 413)


class StartupReadiness(unittest.TestCase):
    def test_first_gpu_encode_finishes_before_socket_bind_or_ready(self):
        events=[]
        def encode(texts):
            self.assertEqual(len(texts),1)
            events.append('encode')
            return [[1.0]+[0.0]*1023],8
        def listen(*args,**kwargs):
            events.append('listen')
            return SimpleNamespace(serve_forever=lambda:events.append('serve'))
        with patch.object(service.resource,'setrlimit'),patch.object(service,'require_mount'),patch.object(service,'verify_snapshot'),patch.object(service,'BGEModel',return_value=SimpleNamespace(encode=encode)),patch.object(service,'server_for',side_effect=listen),patch('sys.argv',['bge_secure_service.py']),patch.dict(service.os.environ,{}),contextlib.redirect_stdout(io.StringIO()) as output:
            service.main()
        self.assertEqual(events,['encode','listen','serve'])
        self.assertLess(output.getvalue().index('BGE_WARMUP_OK'),output.getvalue().index('BGE_READY'))

    def test_invalid_warmup_never_opens_socket_or_reports_ready(self):
        for row in ([0.0]*1024,[float('nan')]+[0.0]*1023,[1.0]*1023):
            with self.subTest(length=len(row)),patch.object(service.resource,'setrlimit'),patch.object(service,'require_mount'),patch.object(service,'verify_snapshot'),patch.object(service,'BGEModel',return_value=SimpleNamespace(encode=lambda texts:([row],8))),patch.object(service,'server_for') as listen,patch('sys.argv',['bge_secure_service.py']),patch.dict(service.os.environ,{}),contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaisesRegex(RuntimeError,'startup warmup'):
                    service.main()
                listen.assert_not_called()
                self.assertNotIn('BGE_READY',output.getvalue())


class SnapshotIntegrity(unittest.TestCase):
    def test_mount_requires_exact_writable_school_nfs_before_model_load(self):
        valid = dict(target='/mnt/goodagents', source=service.EXPECTED_NFS, fstype='nfs4', options='rw,relatime')
        for changes in ({'target': '/'}, {'source': 'another-nfs'}, {'fstype': 'overlay'}, {'options': 'ro,relatime'}):
            with self.subTest(changes=changes), patch.object(service.subprocess, 'check_output', return_value=json.dumps({'filesystems': [dict(valid, **changes)]})):
                with self.assertRaisesRegex(RuntimeError, 'NFS mount missing'):
                    service.require_mount()
        with patch.object(service.subprocess, 'check_output', return_value=json.dumps({'filesystems': [valid]})):
            self.assertEqual(service.require_mount(), valid)

    def test_required_configuration_and_weight_hashes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            snapshot = root / service.REVISION
            snapshot.mkdir()
            for name in service.REQUIRED_FILES:
                target = snapshot / name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(b'fixture')
            (snapshot / 'modules.json').write_text(json.dumps([
                {'path': '', 'type': 'sentence_transformers.models.Transformer'},
                {'path': '1_Pooling', 'type': 'sentence_transformers.models.Pooling'},
                {'path': '2_Normalize', 'type': 'sentence_transformers.models.Normalize'}]))
            (snapshot / '1_Pooling/config.json').write_text(json.dumps({'pooling_mode_cls_token': True}))
            (snapshot / 'sentence_bert_config.json').write_text(json.dumps({'max_seq_length': 8192}))
            manifest = {'repo_id': 'BAAI/bge-m3', 'revision': service.REVISION,
                        'files_sha256': {name: service.file_hash(snapshot / name) for name in service.REQUIRED_FILES}}
            with patch.object(service, 'ROOT', root), patch.object(service, 'WEIGHT_SHA256', manifest['files_sha256']['pytorch_model.bin']):
                service.verify_snapshot(snapshot, manifest)
                (snapshot / 'model.safetensors').write_bytes(b'unreviewed alternate weights')
                with self.assertRaisesRegex(ValueError, 'loader override'):
                    service.verify_snapshot(snapshot, manifest)
                (snapshot / 'model.safetensors').unlink()
                (snapshot / 'config.json').write_bytes(b'modified')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    service.verify_snapshot(snapshot, manifest)
                del manifest['files_sha256']['config.json']
                with self.assertRaisesRegex(ValueError, 'Incomplete'):
                    service.verify_snapshot(snapshot, manifest)


if __name__ == '__main__':
    unittest.main()

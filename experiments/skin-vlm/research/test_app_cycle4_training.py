"""CPU checks for loss semantics, module policy, and production backpressure."""
import json
import io
import os
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import torch
from torch import nn

from research.train_app_cycle4 import (encode_json, sliced_loss, HealthMonitor, ServicePause,
    initialize_report, analyze_cooperatively, evaluate)


class Tokenizer:
    eos_token_id = 9

    def convert_tokens_to_ids(self, token):
        return {'<|image_pad|>': 7, '<|vision_start|>': 6, '<|vision_end|>': 8, '<|video_pad|>': 5}[token]


class Processor:
    tokenizer = Tokenizer()

    def apply_chat_template(self, messages, **kwargs):
        tokens = [1, 6, 7, 8, 2] + ([3, 4, 9, 10] if len(messages) == 3 else [])
        return {'input_ids': torch.tensor([tokens]), 'attention_mask': torch.ones(1, len(tokens), dtype=torch.long)}


class ToyModel(nn.Module):
    """Ordinary causal LM projection; slicing must preserve loss and gradients."""
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(16, 7)
        self.projection = nn.Linear(7, 16)
        self.device = torch.device('cpu')

    def forward(self, input_ids, attention_mask, logits_to_keep=None):
        from types import SimpleNamespace
        hidden = self.embedding(input_ids)
        if logits_to_keep is not None:
            hidden = hidden[:, logits_to_keep]
        return SimpleNamespace(logits=self.projection(hidden))


class TrainingTests(unittest.TestCase):
    def row(self):
        return {'id': 'fixture', 'image': '/fixture.png', 'target': {
            'image_scope': 'non_skin', 'observations': [], 'limitations': [], 'follow_up_question': ''}}

    def test_every_assistant_token_and_eos_supervised_without_image_or_prompt(self):
        with patch('research.train_app_cycle4.open_image', return_value=(object(), {'reasons': []})):
            batch, positions, targets = encode_json(Processor(), self.row())
        self.assertEqual(positions.tolist(), [5, 6, 7, 8])
        self.assertEqual(targets.tolist(), [3, 4, 9, 10])
        self.assertEqual(batch['input_ids'][0, positions - 1].tolist(), [2, 3, 4, 9])

    def test_missing_eos_is_rejected(self):
        processor = Processor()
        processor.tokenizer = Tokenizer()
        processor.tokenizer.eos_token_id = 15
        with patch('research.train_app_cycle4.open_image', return_value=(object(), {'reasons': []})):
            with self.assertRaisesRegex(ValueError, 'EOS'):
                encode_json(processor, self.row())

    def test_sliced_full_ce_and_parameter_gradients_equal(self):
        torch.manual_seed(17)
        model = ToyModel()
        batch = Processor().apply_chat_template([{}, {}, {}])
        positions = torch.tensor([5, 6, 7, 8])
        targets = batch['input_ids'][0, positions]
        reference = torch.nn.functional.cross_entropy(model(**batch).logits[0, positions - 1], targets)
        reference.backward()
        reference_grads = [parameter.grad.clone() for parameter in model.parameters()]
        model.zero_grad(set_to_none=True)
        sliced = sliced_loss(model, (batch, positions, targets)).mean()
        sliced.backward()
        torch.testing.assert_close(reference, sliced)
        for parameter, expected in zip(model.parameters(), reference_grads):
            torch.testing.assert_close(parameter.grad, expected)

    def test_two_consecutive_probe_failures_pause_but_success_resets_count(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = HealthMonitor(Path(folder) / 'probes.jsonl')
            with patch('urllib.request.urlopen', side_effect=TimeoutError):
                self.assertFalse(monitor.probe()['ok'])
                monitor.check()
                self.assertFalse(monitor.probe()['ok'])
                with self.assertRaises(ServicePause):
                    monitor.check()
            records = [json.loads(line) for line in monitor.path.read_text().splitlines()]
            self.assertEqual([record['consecutive_failures'] for record in records], [1, 2])
            self.assertTrue(all('embedding' not in record for record in records))

    def test_success_uses_actual_photo_health_and_embedding_routes(self):
        requests = []
        def response(request, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            requests.append(url)
            if url == 'http://127.0.0.1:8025/health':
                return io.BytesIO(b'{"ready":true}')
            if url == 'http://127.0.0.1:8002/v1/embeddings':
                return io.BytesIO(json.dumps({'data': [{'embedding': [1.] + [0.] * 1023}]}).encode())
            raise AssertionError('Unknown production route')
        with tempfile.TemporaryDirectory() as folder:
            monitor = HealthMonitor(Path(folder) / 'probes.jsonl')
            monitor.count = 1
            with patch('urllib.request.urlopen', side_effect=response):
                self.assertTrue(monitor.probe()['ok'])
            self.assertEqual(monitor.count, 0)
            monitor.check()
            self.assertEqual(requests, ['http://127.0.0.1:8025/health', 'http://127.0.0.1:8002/v1/embeddings'])

    def test_current_launcher_bootstrap_is_fresh_but_old_or_prior_results_are_not(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder)
            (report / 'worker.log').touch()
            job = report / 'job.json'
            job.write_text(json.dumps({'pid': os.getpid(), 'trainer_sha256': 'fixture'}))
            with patch('research.train_app_cycle4.file_hash', return_value='fixture'):
                initialize_report(report, 'a' * 64, False)
                (report / 'old-results.json').write_text('{}')
                with self.assertRaises(FileExistsError):
                    initialize_report(report, 'a' * 64, False)
                (report / 'old-results.json').unlink()
                job.write_text(json.dumps({'pid': os.getpid() + 1, 'trainer_sha256': 'fixture'}))
                with self.assertRaises(FileExistsError):
                    initialize_report(report, 'a' * 64, False)

    def test_interrupted_generation_is_discarded_and_same_image_retried(self):
        class Batch(dict):
            def to(self, device): return self
        class FakeProcessor:
            def apply_chat_template(self, *args, **kwargs):
                return Batch(input_ids=torch.tensor([[1, 2]]))
            def batch_decode(self, *args, **kwargs):
                return [json.dumps({'image_scope': 'non_skin', 'observations': [], 'limitations': [], 'follow_up_question': ''})]
        calls = []
        def generate(**kwargs):
            criterion = kwargs['stopping_criteria'][0]
            criterion.triggered = not calls
            calls.append(True)
            return torch.tensor([[1, 2, 3]])
        engine = SimpleNamespace(processor=FakeProcessor(), model=SimpleNamespace(device='cpu', generate=generate))
        admission = SimpleNamespace(wait_idle=lambda **kwargs: None, make_stopping_criterion=lambda: SimpleNamespace(triggered=False))
        monitor = SimpleNamespace(check=lambda: None)
        with patch('research.train_app_cycle4.open_image', return_value=(object(), {'reasons': []})), \
                patch('torch.cuda.synchronize'), patch.dict('sys.modules', {'transformers': SimpleNamespace(StoppingCriteriaList=list)}):
            result = analyze_cooperatively(engine, dict(self.row(), image_sha256='fixture'), monitor, admission)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['photo_priority_interruptions'], 1)
        self.assertEqual(result['status'], 'needs_retake')

    def test_explicit_resume_uses_only_matching_validation_prefix(self):
        rows = [dict(self.row(), id='first', image_sha256='sha1'), dict(self.row(), id='second', image_sha256='sha2')]
        prediction = dict(status='needs_retake', observation={'image_scope': 'non_skin', 'observations': []})
        engine = SimpleNamespace(model=SimpleNamespace(eval=lambda: None, config=SimpleNamespace()))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'val.json'
            partial = path.with_suffix('.partial-predictions.json')
            partial.write_text(json.dumps({'fingerprint': 'a' * 64,
                'predictions': [dict(prediction, id='first', image_sha256='sha1')]}))
            with patch('research.train_app_cycle4.analyze_cooperatively', return_value=dict(prediction, image_sha256='sha2')) as generate:
                evaluate(engine, rows, path, 'a' * 64, SimpleNamespace(check=lambda: None), None, resume=True)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(generate.call_args[0][1]['id'], 'second')
            self.assertEqual([row['id'] for row in json.loads(path.read_text())['predictions']], ['first', 'second'])


if __name__ == '__main__':
    unittest.main()

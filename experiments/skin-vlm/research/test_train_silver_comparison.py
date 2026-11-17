"""Offline checks: sample-weight gradients, frozen references and sealed inputs."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import torch
from torch import nn

from research import train_app_cycle4 as cycle4
from research import train_silver_comparison as silver
from research.launch_app_cycle4 import trainer_command
from research import launch_app_cycle4 as launcher


def row(identity, skin=True):
    return dict(id=identity, subject_id=identity, image='/mnt/goodagents/data/' + identity + '.png',
        image_sha256=silver.digest(identity), source='synthetic offline fixture', license='test fixture',
        usage_permitted=True, label_origin='source_expert_derived', subject_id_verified=False,
        target=dict(image_scope='skin_closeup' if skin else 'non_skin',
            observations=[dict(tag=tag, region='unknown', evidence='可見外觀') for tag in sorted(cycle4.TAGS)] if skin else [],
            limitations=['影像品質會影響觀察。'], follow_up_question=''))


class ToyModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.weight = nn.Parameter(torch.tensor(1.))
        self.device = torch.device('cpu')
        self.config = SimpleNamespace(use_cache=False)

    def gradient_checkpointing_disable(self):
        pass

    def save_pretrained(self, folder, safe_serialization):
        assert safe_serialization
        (Path(folder) / 'adapter_model.safetensors').write_bytes(b'locally generated test adapter')


class Processor:
    def save_pretrained(self, folder, **kwargs):
        (Path(folder) / 'processor.json').write_text('{}')


class SilverTests(unittest.TestCase):
    def gold(self):
        return [row('gold-positive'), row('gold-negative', False)]

    def silver(self):
        value = row('silver')
        value.update(label_origin='ai_silver', gold_eligible=False, silver_only_train=True,
            loss_weight=.25, reviewer_type='codex_visual_assistant', model_version='unavailable',
            review_protocol_sha256='a' * 64, decisions_sha256='b' * 64)
        return value

    def audit(self, value):
        return dict(manifest_sha256='c' * 64, silver_only_train=True, accepted_ids=[value['id']],
            exclusion_index_sha256='d' * 64, reserved_categories=list(silver.RESERVED_CATEGORIES),
            reserved_coverage={key: dict(status='checked', records=1) for key in silver.RESERVED_CATEGORIES},
            cleaning_checks=dict.fromkeys(silver.CLEANING_CHECKS, True),
            review_protocol_sha256='a' * 64, decisions_sha256='b' * 64)

    def load(self, value=None, mutate_audit=None, mutate_review=None):
        value = self.silver() if value is None else value
        audit = self.audit(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'silver_only_train.jsonl'
            audit_path = Path(directory) / 'cleaning-audit.json'
            review_path = Path(directory) / 'final-visual-review.json'
            decision = dict(id=value['id'], image_sha256=value['image_sha256'],
                image_scope=value['target']['image_scope'], silver_candidate=True,
                features={tag: tag in {item['tag'] for item in value['target']['observations']} for tag in cycle4.TAGS},
                quality_flags=[], reviewer_type='codex_visual_assistant', model_version='unavailable',
                human_expert_verified=False)
            if mutate_review:
                mutate_review(decision)
            review_path.write_text(json.dumps({'reviews': [decision]}))
            review_sha = cycle4.file_hash(review_path)
            audit['final_visual_review'] = dict(path=review_path.name, sha256=review_sha)
            if mutate_audit:
                mutate_audit(audit)
            audit_path.write_text(json.dumps(audit))
            audit_sha = cycle4.file_hash(audit_path)
            def file_hash(path):
                return 'c' * 64 if Path(path).name == 'silver_only_train.jsonl' else value['image_sha256']
            with patch.object(silver, 'load_rows', return_value=[value]), \
                    patch.object(cycle4, 'file_hash', side_effect=lambda p: audit_sha if Path(p).resolve() == audit_path.resolve() else
                        review_sha if Path(p).resolve() == review_path.resolve() else file_hash(p)), \
                    patch.object(cycle4, 'open_image', return_value=(object(), {'reasons': []})):
                return silver.load_silver(path, 'c' * 64, audit_path, audit_sha, self.gold(), [row('val')])

    def test_silver_provenance_is_explicit_and_captions_do_not_enter_prompt(self):
        value = self.silver()
        value['note'] = 'source hint must not enter the training prompt'
        rows, info = self.load(value)
        self.assertEqual(rows[0]['note'], '')
        self.assertEqual(rows[0]['label_origin'], 'ai_silver')
        self.assertFalse(rows[0]['gold_eligible'])
        self.assertEqual(info['teacher_model_version'], 'unavailable')
        self.assertFalse(info['sealed_test_inputs_opened'])

    def test_missing_cleaning_or_holdout_group_fails_closed(self):
        for change in (lambda value: value['cleaning_checks'].pop('ocr'),
                       lambda value: value['reserved_categories'].remove('sealed_test'),
                       lambda value: value['reserved_coverage']['external_phone'].update(status='missing'),
                       lambda value: value.update(accepted_ids=[])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(mutate_audit=change)

    def test_actual_older_reviewer_and_honest_missing_md5_are_not_relabelled(self):
        value = self.silver()
        value['reviewer_type'] = 'conversation_assistant'
        rows, info = self.load(value, mutate_audit=lambda audit: (
            audit['cleaning_checks'].update(md5='unavailable'), audit.update(md5_status_reason='Publisher did not supply MD5'),
            audit['reserved_coverage']['scin_gold'].update(status='not_applicable', records=0, reason='No SCIN gold exists')))
        self.assertEqual(rows[0]['reviewer_type'], 'conversation_assistant')
        self.assertEqual(info['upstream_cleaning_checks']['md5'], 'unavailable')

    def test_final_visual_unknown_inconsistency_quality_or_missing_evidence_refuses_training(self):
        changes = [lambda value: value['features'].update(flaking=None),
            lambda value: value['features'].update(dark_marks=False),
            lambda value: value.update(quality_flags=['extensive_black_redaction']),
            lambda value: value.update(silver_candidate=False),
            lambda value: value.update(image_sha256='e' * 64),
            lambda value: value.update(image_scope='face'),
            lambda value: value.update(human_expert_verified=True)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(mutate_review=change)
        for change in (lambda audit: audit.pop('final_visual_review'),
                       lambda audit: audit['final_visual_review'].update(sha256='f' * 64),
                       lambda audit: audit['final_visual_review'].update(path='../outside-review.json')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(mutate_audit=change)

    def test_gold_overlap_invented_teacher_wrong_weight_or_gold_origin_rejected(self):
        changes = [dict(image_sha256=self.gold()[0]['image_sha256']), dict(subject_id='val'),
            dict(id='gold-positive'), dict(model_version='invented-version'), dict(loss_weight=1.),
            dict(loss_weight=True), dict(label_origin='source_expert_derived'), dict(gold_eligible=True)]
        for change in changes:
            value = self.silver()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(value)

    def test_sealed_input_rejected_before_any_read(self):
        with patch.object(cycle4, 'file_hash', side_effect=AssertionError('must not open a test input')):
            with self.assertRaisesRegex(ValueError, 'Sealed'):
                silver.read_verified(Path('/unused/test.json'), 'a' * 64)

    def test_original_default_weighting_is_unchanged_and_optional_values_are_locked(self):
        values, details = cycle4.loss_weighting(self.gold())
        self.assertEqual(values, [1., 1.])
        self.assertIsNone(details)
        rows = self.gold() + [self.silver()]
        values, details = cycle4.loss_weighting(rows, 'loss_weight', self.gold())
        self.assertEqual(values, [1., 1., .25])
        rows[-1]['loss_weight'] = .5
        self.assertNotEqual(cycle4.loss_weighting(rows, 'loss_weight', self.gold())[1], details)
        for value in (0, -1, 2, True, float('nan')):
            rows[-1]['loss_weight'] = value
            with self.assertRaises(ValueError):
                cycle4.loss_weighting(rows, 'loss_weight', self.gold())

    def train_cpu(self, root, value, *, weighted, resume=False):
        model = ToyModel()
        gradients = []
        model.weight.register_hook(lambda gradient: gradients.append(float(gradient)))
        engine = SimpleNamespace(model=model, processor=Processor())
        config = dict(silver.RECIPE, name='cpu-pilot', seed=20261116, gradient_clip=100.)
        lineage = dict(runtime={'torch': torch.__version__}, fixture=True)
        def sliced_loss(model, encoded):
            return model.weight * torch.ones(5 if encoded == 'aux' else 1) * (3 if encoded == 'aux' else 2)
        monitor = SimpleNamespace(check=lambda: None, failed=SimpleNamespace(is_set=lambda: False))
        admission = SimpleNamespace(wait_idle=lambda **kwargs: None)
        with patch.object(cycle4, 'configure_adapter', return_value=(model, {'language': [model.weight], 'visual': []})), \
                patch.object(cycle4, 'verify_causal_projection', return_value={'checked': True}), \
                patch.object(cycle4, 'encode_json', return_value='json'), \
                patch.object(cycle4, 'encode_aux', return_value='aux'), \
                patch.object(cycle4, 'sliced_loss', side_effect=sliced_loss), \
                patch.object(cycle4, 'class_weights', return_value={key: {'true': 1., 'false': 1.} for key in cycle4.KEYS}) as balance, \
                patch('torch.cuda.manual_seed_all'), patch('torch.cuda.get_rng_state_all', return_value=[]), \
                patch('torch.cuda.set_rng_state_all'), patch('torch.cuda.reset_peak_memory_stats'), \
                patch('torch.cuda.max_memory_allocated', return_value=0):
            summary = cycle4.train(engine, [value], config, root / 'cpu-pilot', root / 'backup', lineage,
                monitor, admission, resume, pilot=True, **({'sample_weight_key': 'loss_weight', 'class_weight_rows': self.gold()} if weighted else {}))
        return summary, gradients, balance.call_args

    def test_actual_json_and_aux_backward_both_scale_and_gold_class_weights_are_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ordinary, full_gradients, _ = self.train_cpu(root / 'ordinary', self.silver(), weighted=False)
            weighted, quarter_gradients, balance = self.train_cpu(root / 'weighted', self.silver(), weighted=True)
            self.assertEqual(len(full_gradients), 2)
            self.assertEqual(len(quarter_gradients), 2)
            for full, quarter in zip(full_gradients, quarter_gradients):
                self.assertAlmostEqual(quarter, full * .25, places=7)
            self.assertEqual(balance.args[0], self.gold())
            self.assertNotIn('loss_weighting', ordinary)
            self.assertEqual(ordinary['fingerprint'], silver.protocol_fingerprint(dict(
                lineage=ordinary['lineage'], config=ordinary['config'], pilot=True, runtime=ordinary['lineage']['runtime'])))
            self.assertEqual(weighted['loss_weighting']['class_weight_images'], 2)
            self.assertEqual(weighted['steps'], 1)
            self.assertTrue(Path(weighted['checkpoint'], 'files.sha256.json').is_file())
            changed = self.silver()
            changed['loss_weight'] = .5
            with self.assertRaisesRegex(ValueError, 'changed|fingerprint|Fingerprint'):
                self.train_cpu(root / 'weighted', changed, weighted=True, resume=True)

    def test_replication_requires_real_original_gates_and_historical_gain(self):
        metrics = dict(valid_json_rate=1., non_skin_correct_rejection_rate=1., macro_f1=.9,
            by_tag={tag: dict(precision=.9, recall=.9, f1=.9) for tag in cycle4.TAGS})
        self.assertTrue(silver.replication_gate(metrics, {'macro_f1': .63})['allowed'])
        self.assertFalse(silver.replication_gate(metrics, {'macro_f1': .895})['allowed'])
        failed = copy.deepcopy(metrics)
        failed['by_tag']['flaking']['recall'] = .5
        self.assertFalse(silver.replication_gate(failed, {'macro_f1': .63})['allowed'])
        results = [dict(seed=seed, validation=metrics) for seed in silver.SEEDS]
        historical = {str(seed): {'macro_f1': .63} for seed in silver.SEEDS}
        self.assertTrue(silver.matched_stability(results, historical)['eligible'])
        self.assertFalse(silver.matched_stability(results[:1], historical)['eligible'])

    def test_historical_controls_recompute_predictions_and_reject_drift(self):
        validation = [row('val-positive'), row('val-object', False)]
        protocol = dict(data_manifest_sha256=cycle4.FROZEN, runtime={'torch': 'cpu-test'},
            model={'pinned': True}, generation={'do_sample': False})
        fingerprint = silver.protocol_fingerprint(protocol)
        predictions = [dict(id=value['id'], image_sha256=value['image_sha256'], observation=value['target'],
            status='needs_confirmation' if index == 0 else 'needs_retake') for index, value in enumerate(validation)]
        metrics = cycle4.score(validation, predictions)
        metrics['macro_f1'] = 1.
        with tempfile.TemporaryDirectory() as directory, patch.object(silver, 'HISTORICAL_FINGERPRINT', fingerprint):
            root = Path(directory)
            (root / 'protocol.json').write_text(json.dumps(dict(protocol, fingerprint=fingerprint)))
            candidates = []
            for seed in silver.SEEDS:
                name = 'B-json-aux-r8' + ('' if seed == silver.SEEDS[0] else '-seed' + str(seed))
                candidates.append(dict(name=name, config=dict(silver.RECIPE, name=name, seed=seed), validation=metrics))
                (root / (name + '-val.json')).write_text(json.dumps(dict(fingerprint=fingerprint,
                    predictions=predictions, metrics=metrics)))
            (root / 'selection.json').write_text(json.dumps(dict(fingerprint=fingerprint,
                test_evaluated=False, candidates=candidates)))
            hashes = {path.name: cycle4.file_hash(path) for path in root.iterdir()}
            with patch.object(silver, 'HISTORICAL_FILES', hashes):
                historical = silver.historical_controls(root, validation, protocol)
                self.assertEqual(historical['kind'], 'historical_control_not_fresh_paired')
                self.assertEqual(len(historical['seed_metrics']), 3)
                changed = copy.deepcopy(validation)
                changed[0]['image_sha256'] = 'f' * 64
                with self.assertRaisesRegex(ValueError, 'IDs/image'):
                    silver.historical_controls(root, changed, protocol)
                changed_protocol = dict(protocol, runtime={'torch': 'different'})
                with self.assertRaisesRegex(ValueError, 'comparable'):
                    silver.historical_controls(root, validation, changed_protocol)
                path = root / 'B-json-aux-r8-val.json'
                content = json.loads(path.read_text())
                content['metrics']['macro_f1'] = .9
                path.write_text(json.dumps(content))
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    silver.historical_controls(root, validation, protocol)
                hashes[path.name] = cycle4.file_hash(path)
                with self.assertRaisesRegex(ValueError, 'metrics'):
                    silver.historical_controls(root, validation, protocol)

    def test_launcher_keeps_original_command_and_separates_silver_arguments(self):
        args = SimpleNamespace(trainer='app-cycle4', python=Path('/fixture/python'),
            data=Path('/fixture/data'), model=Path('/fixture/model'), output=Path('/fixture/output'),
            report=Path('/fixture/report'), backup_root=Path('/fixture/backup'),
            silver_train=None, silver_audit=None, silver_sha256=None, silver_audit_sha256=None,
            historical_report=None, resume=False, pilot_only=True, replicate=False)
        trainer, command = trainer_command(args)
        self.assertEqual(trainer.name, 'train_app_cycle4.py')
        self.assertEqual(command[-1], '--pilot-only')
        self.assertNotIn('--silver-train', command)
        args.trainer = 'silver-comparison'
        with self.assertRaises(ValueError):
            trainer_command(args)
        args.silver_train = Path('/fixture/silver_only_train.jsonl')
        args.silver_audit = Path('/fixture/audit.json')
        args.silver_sha256 = 'a' * 64
        args.silver_audit_sha256 = 'b' * 64
        args.historical_report = Path('/fixture/historical')
        trainer, command = trainer_command(args)
        self.assertEqual(trainer.name, 'train_silver_comparison.py')
        self.assertIn('--silver-sha256', command)
        self.assertIn('--pilot-only', command)
        args.replicate = True
        with self.assertRaises(ValueError):
            trainer_command(args)

    def test_quality_stop_cannot_resume_before_preflight_or_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = []
            for name in ('data', 'model', 'output', 'report', 'backup-root', 'silver-train', 'silver-audit', 'historical-report'):
                argv += ['--' + name, str(root)]
            argv += ['--silver-sha256', 'a' * 64, '--silver-audit-sha256', 'b' * 64, '--resume']
            for sentinel in ('data-quality-stop.json', 'DO_NOT_RESUME'):
                path = root / sentinel
                path.write_text('{}')
                with patch.object(silver, 'preflight', side_effect=AssertionError('must not reach preflight')):
                    with self.assertRaisesRegex(ValueError, 'cannot resume'):
                        silver.main(argv)
                with patch('sys.argv', ['launcher', '--python', '/fixture/python', '--data', '/fixture/data',
                        '--model', '/fixture/model', '--output', '/fixture/out', '--report', str(root),
                        '--backup-root', '/fixture/backup', '--resume']), \
                        patch.object(launcher.subprocess, 'Popen', side_effect=AssertionError('must not launch')), \
                        patch.object(launcher.subprocess, 'check_output', side_effect=AssertionError('must not preflight')):
                    with self.assertRaisesRegex(ValueError, 'cannot resume'):
                        launcher.main()
                path.unlink()
            launcher.refuse_invalidated_resume(root, True)


if __name__ == '__main__':
    unittest.main()

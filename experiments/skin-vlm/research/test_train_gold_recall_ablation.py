"""CPU only: true-only gradients, frozen gold boundary and bounded launch."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import math
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import torch

from research import train_app_cycle4 as cycle4
from research import train_gold_recall_ablation as gold
from research.launch_app_cycle4 import trainer_command


def row(identity, tags=()):
    return dict(id=identity, label_origin='source_expert_derived', subject_id_verified=False,
        target=dict(image_scope='skin_closeup', observations=[{'tag': tag, 'region': 'unknown', 'evidence': '可見外觀'} for tag in tags],
            limitations=['影像品質會影響觀察。'], follow_up_question=''))


class GoldRecallTests(unittest.TestCase):
    def test_predeclared_recipe_changes_only_positive_auxiliary_boost(self):
        original = dict(gold.CONFIG)
        original.pop('aux_positive_boost')
        original['name'] = gold.controls.RECIPE['name']
        original.pop('seed')
        self.assertEqual(original, gold.controls.RECIPE)
        self.assertEqual(gold.CONFIG['seed'], 20261116)
        self.assertEqual(gold.CONFIG['aux_positive_boost'], {'dark_marks': 2., 'flaking': 2.})
        self.assertEqual((559 + 7) // 8 * gold.CONFIG['epochs'], 140)

    def test_only_positive_rare_feature_gradients_double_and_negatives_are_unchanged(self):
        features = dict(skin_visible=1., acne_like_bumps=1., redness=1., dark_marks=2., flaking=2.)
        weights = {key: {'true': 1.5, 'false': .75} for key in cycle4.KEYS}
        for flags in ((True, True), (True, False), (False, True), (False, False)):
            truth = dict.fromkeys(cycle4.KEYS, True)
            truth.update(dark_marks=flags[0], flaking=flags[1])
            base = cycle4.auxiliary_multipliers(truth, weights, features, 'cpu')
            boosted = cycle4.auxiliary_multipliers(truth, weights, features, 'cpu', gold.CONFIG['aux_positive_boost'])
            primary = torch.tensor(2., requires_grad=True)
            auxiliary = torch.arange(1., 6., requires_grad=True)
            ((primary + .25 * (auxiliary * base).sum() / 7) / 1.25).backward()
            base_gradient, primary_gradient = auxiliary.grad.clone(), primary.grad.clone()
            auxiliary.grad.zero_();primary.grad.zero_()
            ((primary + .25 * (auxiliary * boosted).sum() / 7) / 1.25).backward()
            expected = base_gradient.clone()
            for index, key in enumerate(cycle4.KEYS):
                if key in {'dark_marks', 'flaking'} and truth[key]:
                    expected[index] *= 2
            torch.testing.assert_close(auxiliary.grad, expected)
            torch.testing.assert_close(primary.grad, primary_gradient)
            # The existing defaults construct exactly the old multiplier vector.
            torch.testing.assert_close(base, torch.tensor([features[key] * weights[key]['true' if truth[key] else 'false']
                for key in cycle4.KEYS]), rtol=0, atol=0)

    def test_positive_boost_cannot_affect_other_tags_or_use_invalid_values(self):
        self.assertEqual(cycle4.positive_auxiliary_boost({}), {})
        for invalid in (None, [], {'redness': 2}, {'dark_marks': True}, {'flaking': 0},
                        {'dark_marks': 4.1}, {'flaking': float('nan')}, {'dark_marks': float('inf')}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cycle4.positive_auxiliary_boost({'aux_positive_boost': invalid})

    def test_config_boost_is_part_of_the_existing_checkpoint_fingerprint(self):
        payload = dict(runtime={'torch': 'cpu'}, config=gold.CONFIG)
        current = gold.protocol_fingerprint(payload)
        changed = copy.deepcopy(payload)
        changed['config']['aux_positive_boost']['flaking'] = 3.
        self.assertNotEqual(current, gold.protocol_fingerprint(changed))

    def test_gold_boundary_rejects_silver_or_changed_counts(self):
        train = [row('train-' + str(index)) for index in range(559)]
        validation = [row('val-' + str(index)) for index in range(80)]
        gold.assert_gold_only(train, validation)
        for updates in ({'label_origin': 'ai_silver'}, {'silver_only_train': True},
                        {'teacher': {'reviewer_type': 'assistant'}}, {'loss_weight': .25}, {'loss_weight': True},
                        {'subject_id_verified': True}, {'human_expert_verified': True}, {'label_origin': 'human_verified'},
                        {'label_origin': None}):
            changed = copy.deepcopy(train)
            changed[0].update(updates)
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                gold.assert_gold_only(changed, validation)
        with self.assertRaises(ValueError):
            gold.assert_gold_only(train[:-1], validation)
        invalid = copy.deepcopy(train)
        invalid[0]['target']['position'] = 'left-face'
        with self.assertRaises(ValueError):
            gold.assert_gold_only(invalid, validation)

    def test_pilot_selection_uses_only_training_positives_deterministically(self):
        rows = [row('first'), row('z', ['flaking']), row('b', ['flaking', 'dark_marks']), row('a', ['dark_marks', 'flaking'])]
        self.assertEqual(gold.select_pilot_row(rows)['id'], 'a')
        self.assertEqual(gold.select_pilot_row(list(reversed(rows)))['id'], 'a')
        with self.assertRaises(ValueError):
            gold.select_pilot_row([row('no-positive')])

    def test_gold_launcher_has_no_silver_or_replication_arguments(self):
        args = SimpleNamespace(trainer='gold-recall-ablation', python=Path('/fixture/python'),
            data=Path('/fixture/data'), model=Path('/fixture/model'), output=Path('/fixture/output'),
            report=Path('/fixture/report'), backup_root=Path('/fixture/backup'),
            historical_report=Path('/fixture/history'), silver_train=None, silver_audit=None,
            silver_sha256=None, silver_audit_sha256=None, resume=False, pilot_only=True, replicate=False)
        trainer, command = trainer_command(args)
        self.assertEqual(trainer.name, 'train_gold_recall_ablation.py')
        self.assertIn('--historical-report', command)
        self.assertIn('--pilot-only', command)
        self.assertNotIn('--silver-train', command)
        args.silver_train = Path('/fixture/forbidden-silver.jsonl')
        with self.assertRaises(ValueError):
            trainer_command(args)
        args.silver_train = None;args.replicate = True
        with self.assertRaises(ValueError):
            trainer_command(args)

    def test_quality_stop_and_silver_arguments_fail_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = []
            for name in ('data', 'model', 'output', 'report', 'backup-root', 'historical-report'):
                argv += ['--' + name, str(root)]
            (root / 'DO_NOT_RESUME').touch()
            with patch.object(gold, 'preflight', side_effect=AssertionError('must not load')):
                with self.assertRaisesRegex(ValueError, 'cannot resume'):
                    gold.main(argv + ['--resume'])
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    gold.main(argv + ['--silver-train', 'forbidden.jsonl'])

    def test_new_launcher_bootstrap_checks_this_trainer_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'worker.log').touch()
            (root / 'job.json').write_text(json.dumps(dict(pid=os.getpid(), trainer_sha256='gold-trainer')))
            with patch.object(cycle4, 'file_hash', return_value='gold-trainer'):
                gold.initialize_report(root, 'a' * 64, False)
            (root / 'unrelated-result.json').write_text('{}')
            with self.assertRaises(FileExistsError):
                gold.initialize_report(root, 'a' * 64, False)

    def test_historical_protocol_is_stable_across_hash_seeds_but_real_drift_refuses_resume(self):
        # These confusion counts exercise the actual nonassociative four-tag
        # F1 sum, without importing or depending on any real held-out dataset.
        counts = {'acne_like_bumps': (30, 13, 7), 'dark_marks': (5, 2, 11),
                  'flaking': (3, 1, 11), 'redness': (41, 13, 3)}
        validation, predictions = [], []
        for index in range(80):
            actual = [tag for tag, (tp, fp, fn) in counts.items() if index < tp + fn]
            predicted = [tag for tag, (tp, fp, fn) in counts.items() if index < tp or tp + fn <= index < tp + fn + fp]
            sample = row('synthetic-' + str(index), actual)
            sample['image_sha256'] = gold.controls.digest(sample['id'])
            if index >= 64:
                sample['target']['image_scope'] = 'non_skin'
            validation.append(sample)
            target = copy.deepcopy(sample['target'])
            target['observations'] = [{'tag': tag, 'region': 'unknown', 'evidence': '合成測試'} for tag in predicted]
            predictions.append(dict(id=sample['id'], image_sha256=sample['image_sha256'], observation=target,
                status='needs_confirmation' if index < 64 else 'needs_retake'))
        measured = cycle4.score(validation, predictions)
        measured['macro_f1'] = math.fsum(measured['by_tag'][tag]['f1'] for tag in sorted(cycle4.TAGS)) / 4
        protocol = dict(runtime={'torch': 'cpu-fixture'}, data_manifest_sha256=cycle4.FROZEN,
            model={'id': 'fixture'}, generation={'do_sample': False}, source_sha256={'trainer.py': 'a' * 64})
        historical_fp = gold.protocol_fingerprint(protocol)
        script = r'''
import json,sys
from pathlib import Path
from research import train_silver_comparison as controls
root=Path(sys.argv[1]);fixture=json.loads((root/'fixture.json').read_text())
controls.HISTORICAL_FINGERPRINT=fixture['historical_fingerprint']
controls.HISTORICAL_FILES=fixture['file_sha256']
protocol=fixture['protocol'];mode=sys.argv[2]
if mode=='changed_source':protocol['source_sha256']['trainer.py']='b'*64
if mode=='changed_metric':
 path=root/'B-json-aux-r8-val.json';data=json.loads(path.read_text())
 data['metrics']['by_tag']['flaking']['recall']+=.01
 path.write_text(json.dumps(data))
try:
 protocol['historical_control']=controls.historical_controls(root,fixture['validation'],protocol)
 fingerprint=controls.protocol_fingerprint(protocol)
 if len(sys.argv)>3:controls.assert_fingerprint_matches(sys.argv[3],fingerprint)
 print(json.dumps({'fingerprint':fingerprint,'resume_accepted':True}))
except ValueError:
 print(json.dumps({'resume_accepted':False}))
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'protocol.json').write_text(json.dumps(dict(protocol, fingerprint=historical_fp)))
            candidates = []
            for seed in gold.controls.SEEDS:
                name = 'B-json-aux-r8' + ('' if seed == gold.controls.SEEDS[0] else '-seed' + str(seed))
                candidates.append(dict(name=name, config=dict(gold.controls.RECIPE, name=name, seed=seed), validation=measured))
                (root / (name + '-val.json')).write_text(json.dumps(dict(fingerprint=historical_fp, metrics=measured, predictions=predictions)))
            (root / 'selection.json').write_text(json.dumps(dict(fingerprint=historical_fp, test_evaluated=False, candidates=candidates)))
            hashes = {path.name: cycle4.file_hash(path) for path in root.iterdir()}
            (root / 'fixture.json').write_text(json.dumps(dict(protocol=protocol, validation=validation,
                historical_fingerprint=historical_fp, file_sha256=hashes)))
            expected = None
            for seed in range(1, 9):
                env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=str(gold.ROOT))
                command = [sys.executable, '-c', script, str(root), 'unchanged'] + ([expected] if expected else [])
                response = json.loads(subprocess.run(command, env=env, text=True, capture_output=True, check=True, timeout=20).stdout)
                self.assertTrue(response['resume_accepted'], 'unchanged protocol must resume across Python hash seeds')
                expected = response['fingerprint']
            for mode in ('changed_source', 'changed_metric'):
                response = json.loads(subprocess.run([sys.executable, '-c', script, str(root), mode, expected],
                    env=env, text=True, capture_output=True, check=True, timeout=20).stdout)
                self.assertFalse(response['resume_accepted'], 'real code/metric changes must still refuse reuse')


if __name__ == '__main__':
    unittest.main()

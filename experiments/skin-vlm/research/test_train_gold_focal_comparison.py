"""CPU-only focal math, exact resume boundary and bounded fresh-pair workflow."""
import contextlib
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

import torch
from torch import nn
from torch.nn import functional as F

from research import train_app_cycle4 as cycle4
from research import train_gold_focal_comparison as focal
from research.launch_app_cycle4 import trainer_command


def row(identity, tags=('dark_marks', 'flaking')):
    return dict(id=identity, image_sha256=focal.controls.digest(identity), subject_id=identity,
        subject_id_verified=False, label_origin='source_expert_derived', target=dict(image_scope='skin_closeup',
            observations=[dict(tag=tag, region='unknown', evidence='合成測試') for tag in tags],
            limitations=['合成測試資料。'], follow_up_question=''))


def metrics(value):
    return dict(images=80, valid_json_rate=1., non_skin_correct_rejection_rate=1., macro_f1=value,
        by_tag={tag: dict(precision=value, recall=value, f1=value) for tag in sorted(cycle4.TAGS)})


class TinyModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.logits = nn.Parameter(torch.tensor([[.3, .8, -.4, .2, .5, -.1, .7, -.2]] * 5))
        self.device = torch.device('cpu')
        self.config = SimpleNamespace(use_cache=False)

    def gradient_checkpointing_disable(self):
        pass

    def save_pretrained(self, folder, safe_serialization):
        assert safe_serialization
        (Path(folder) / 'adapter_model.safetensors').write_bytes(b'locally generated CPU test adapter')


class TinyProcessor:
    def save_pretrained(self, folder, **kwargs):
        (Path(folder) / 'processor.json').write_text('{}')


class FocalTests(unittest.TestCase):
    def test_predeclared_fresh_pair_changes_only_focal_gammas_and_name(self):
        control, focused = copy.deepcopy(focal.CONFIGS)
        focused.pop('focal_gammas');focused['name'] = control['name']
        self.assertEqual(control, focused)
        self.assertNotIn('focal_gammas', control)
        self.assertTrue(all('aux_positive_boost' not in config for config in focal.CONFIGS))
        self.assertEqual(focal.GAMMAS, {'true': 1., 'false': 2.})
        self.assertEqual((control['rank'], control['lr'], control['epochs'], control['seed'], control['aux_weight']),
            (8, 3e-5, 2, 20261116, .25))
        self.assertEqual(focal.IMAGE_BUDGET, {'shortest_edge': 256 * 32 * 32, 'longest_edge': 768 * 32 * 32})

    def test_gamma0_is_bitwise_original_ce_loss_and_full_gradient(self):
        torch.manual_seed(13)
        logits = torch.randn(5, 11, requires_grad=True)
        primary_logits = torch.randn(7, 11, requires_grad=True)
        targets = torch.tensor([1, 2, 2, 1, 1])
        truth = cycle4.expected(row('fixture'))
        multipliers = torch.tensor([1., 1., 1., 2., 2.])
        def objective(gammas):
            primary = F.cross_entropy(primary_logits, torch.arange(7))
            ce = F.cross_entropy(logits, targets, reduction='none')
            transformed = cycle4.focal_auxiliary_losses(ce, truth, gammas)
            if gammas is None or gammas == {'true': 0., 'false': 0.}:
                self.assertIs(transformed, ce)
            return (primary + .25 * (transformed * multipliers).sum() / 7) / 1.25
        baseline = objective(None);baseline.backward()
        gradient, primary_gradient = logits.grad.clone(), primary_logits.grad.clone()
        logits.grad.zero_();primary_logits.grad.zero_()
        identity = objective({'true': 0., 'false': 0.});identity.backward()
        torch.testing.assert_close(identity, baseline, rtol=0, atol=0)
        torch.testing.assert_close(logits.grad, gradient, rtol=0, atol=0)
        torch.testing.assert_close(primary_logits.grad, primary_gradient, rtol=0, atol=0)

    def test_true_false_focal_gradients_include_the_probability_factor_derivative(self):
        logits = torch.tensor([[1., -.2, .4, -.7]] * 5, requires_grad=True)
        truth = cycle4.expected(row('fixture'))
        targets = torch.tensor([1 if truth[key] else 2 for key in cycle4.KEYS])
        ce = F.cross_entropy(logits, targets, reduction='none')
        weighted = cycle4.focal_auxiliary_losses(ce, truth, focal.GAMMAS)
        weighted.sum().backward()
        with torch.no_grad():
            gamma = torch.tensor([1. if truth[key] else 2. for key in cycle4.KEYS])
            pt = torch.exp(-ce);q = -torch.expm1(-ce)
            derivative = q.pow(gamma) + ce * gamma * q.pow(gamma - 1) * pt
            ordinary = torch.softmax(logits, dim=-1) - F.one_hot(targets, 4)
            expected = ordinary * derivative[:, None]
            detached_factor_gradient = ordinary * q.pow(gamma)[:, None]
        torch.testing.assert_close(logits.grad, expected, rtol=1e-6, atol=1e-7)
        self.assertGreater(float((logits.grad - detached_factor_gradient).abs().max()), .01)
        self.assertTrue(torch.isfinite(logits.grad).all())

    def test_probability_uses_full_vocab_ce_not_binary_renormalization(self):
        logits = torch.tensor([[8., 2., 1., -.5]] * 5, requires_grad=True)
        truth = cycle4.expected(row('fixture'))
        targets = torch.tensor([1 if truth[key] else 2 for key in cycle4.KEYS])
        ce = F.cross_entropy(logits, targets, reduction='none')
        result = cycle4.focal_auxiliary_losses(ce, truth, focal.GAMMAS)
        gamma = torch.tensor([1. if truth[key] else 2. for key in cycle4.KEYS])
        expected = ce * (1 - torch.exp(-ce)).pow(gamma)
        binary_pt = torch.softmax(logits[:, 1:3], dim=-1).gather(1, (targets - 1)[:, None]).flatten()
        binary_substitution = ce * (1 - binary_pt).pow(gamma)
        torch.testing.assert_close(result, expected)
        self.assertGreater(float((result - binary_substitution).abs().max().detach()), 1.)

    def test_zero_extreme_and_fractional_ce_boundaries_have_finite_gradients_without_clipping(self):
        ce = torch.tensor([0., 1e-12, .5, 100., 1000.], requires_grad=True)
        result = cycle4.focal_auxiliary_losses(ce, cycle4.expected(row('fixture')), {'true': .5, 'false': 2.})
        result.sum().backward()
        self.assertTrue(torch.isfinite(result).all())
        self.assertTrue(torch.isfinite(ce.grad).all())
        self.assertEqual(float(ce.grad[0]), 0.)
        self.assertEqual(float(result[-1].detach()), 1000.)

    def test_invalid_gamma_configuration_is_not_silently_accepted(self):
        self.assertIsNone(cycle4.validated_focal_gammas({}))
        for value in (None, {}, {'true': 1}, {'true': 1, 'false': True}, {'true': -1, 'false': 2},
                      {'true': 1, 'false': 5}, {'true': float('nan'), 'false': 2}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cycle4.validated_focal_gammas({'focal_gammas': value})

    def cpu_train(self, root, gammas, resume=False):
        model = TinyModel();engine = SimpleNamespace(model=model, processor=TinyProcessor())
        reference = [row('gold-positive', tuple(cycle4.TAGS)), row('gold-control', ())]
        reference[1]['target']['image_scope'] = 'non_skin'
        config = dict(focal.FOCAL, name='cpu-focal-pilot', focal_gammas=gammas)
        targets = torch.tensor([1, 2, 2, 1, 1])
        def loss(model, encoded):
            return F.cross_entropy(model.logits * (1.1 if encoded == 'json' else 1.), targets, reduction='none')
        monitor = SimpleNamespace(check=lambda: None, failed=threading.Event())
        admission = SimpleNamespace(wait_idle=lambda **kwargs: None)
        with patch.object(cycle4, 'configure_adapter', return_value=(model, {'language': [model.logits], 'visual': []})), \
                patch.object(cycle4, 'verify_causal_projection', return_value={'checked': True}), \
                patch.object(cycle4, 'encode_json', return_value='json'), patch.object(cycle4, 'encode_aux', return_value='aux'), \
                patch.object(cycle4, 'sliced_loss', side_effect=loss), patch('torch.cuda.manual_seed_all'), \
                patch('torch.cuda.get_rng_state_all', return_value=[]), patch('torch.cuda.set_rng_state_all'), \
                patch('torch.cuda.reset_peak_memory_stats'), patch('torch.cuda.max_memory_allocated', return_value=0):
            return cycle4.train(engine, [row('pilot')], config, root / 'cpu-focal-pilot', root / 'backup',
                {'runtime': {'torch': str(torch.__version__)}, 'gold_only': True}, monitor, admission,
                resume, pilot=True, class_weight_rows=reference)

    def test_actual_optimizer_checkpoint_records_formula_and_refuses_different_formula_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = self.cpu_train(root, focal.GAMMAS)
            self.assertEqual(result['steps'], 1)
            self.assertFalse(result['auxiliary_focal_variant']['probability_clipping'])
            self.assertFalse(result['auxiliary_focal_variant']['focal_factor_gradient_detached'])
            self.assertTrue((Path(result['checkpoint']) / 'files.sha256.json').is_file())
            with self.assertRaisesRegex(ValueError, 'changed|fingerprint|Fingerprint'):
                self.cpu_train(root, {'true': 1., 'false': 3.}, resume=True)

    def test_pilot_uses_training_rare_positives_and_both_boolean_labels(self):
        selected = focal.select_pilot_row([row('nonrare', ()), row('rare')])
        self.assertEqual(selected['id'], 'rare')
        with self.assertRaises(ValueError):
            focal.select_pilot_row([row('alltrue', tuple(cycle4.TAGS))])

    def test_pair_summary_is_fresh_single_seed_and_preserves_failed_gates(self):
        results = [dict(name=config['name'], parameters=10, validation=metrics(value))
            for config, value in zip(focal.CONFIGS, (.63, .65))]
        summary = focal.paired_summary(results)
        self.assertAlmostEqual(summary['macro_f1_difference'], .02)
        self.assertFalse(summary['focal_gate']['eligible'])
        self.assertFalse(summary['additional_seeds_started'])
        self.assertFalse(summary['automatic_deployment'])
        with self.assertRaises(ValueError):
            focal.paired_summary(list(reversed(results)))

    def test_main_runs_pilot_then_fresh_ce_then_fresh_focal_with_unmerged_base(self):
        class Monitor:
            def __init__(self, *args):self.stop=threading.Event();self.thread=SimpleNamespace(start=lambda: None)
            def probe(self):return {'ok': True}
            def check(self):pass
        class Admission:
            def __init__(self, *args):pass
            def start(self):pass
            def close(self):pass
            def wait_idle(self, **kwargs):pass
        engine = SimpleNamespace(processor=SimpleNamespace(image_processor=SimpleNamespace(size=focal.IMAGE_BUDGET)), active_adapter=None)
        calls=[];detach=[]
        def train(engine, rows, config, output, *args, **kwargs):
            self.assertIsNone(engine.active_adapter, 'candidate must start from the unmerged base')
            engine.active_adapter=config['name'];calls.append((config, kwargs, len(rows)))
            return dict(steps=1 if kwargs.get('pilot') else 140, training_images=len(rows),
                visual_trainable_parameters=0, trainable_parameters=16515072, checkpoint=str(output/'step-final'), elapsed_seconds=0.)
        def unload(engine):detach.append(engine.active_adapter);engine.active_adapter=None
        rows=[row('gold-'+str(index)) for index in range(559)];validation=[row('val-'+str(index)) for index in range(80)]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);argv=[]
            for key in ('data','model','output','report','backup-root','historical-report'):argv += ['--'+key,str(root/key)]
            with patch.object(focal,'preflight',return_value=({'runtime':{'torch':'cpu-fixture'}},rows,validation)), \
                    patch.object(cycle4,'HealthMonitor',Monitor),patch.object(cycle4,'PhotoAdmission',Admission), \
                    patch.object(cycle4,'SkinVLM',return_value=engine) as load, \
                    patch.object(focal,'verify_focal_pilot_formula',return_value={'checked':True}), \
                    patch.object(cycle4,'train',side_effect=train),patch.object(cycle4,'detach_adapter',side_effect=unload), \
                    patch.object(cycle4,'evaluate',side_effect=[metrics(.63),metrics(.65)]):
                focal.main(argv)
            self.assertEqual([call[0]['name'] for call in calls], ['pilot-'+focal.FOCAL['name'],focal.CE['name'],focal.FOCAL['name']])
            self.assertEqual([call[0]['seed'] for call in calls], [20261116]*3)
            self.assertEqual([call[2] for call in calls], [1,559,559])
            self.assertEqual(len(detach),3);self.assertEqual(load.call_count,1)
            result=json.loads((root/'report/selection.json').read_text())
            self.assertFalse(result['additional_seeds_started']);self.assertIsNone(result['selected_research_adapter'])
            self.assertFalse(result['test_evaluated']);self.assertFalse(result['automatic_deployment'])

    def test_launcher_choice_preserves_gold_only_flags(self):
        args=SimpleNamespace(trainer='gold-focal-comparison',python=Path('/fixture/python'),data=Path('/fixture/data'),
            model=Path('/fixture/model'),output=Path('/fixture/output'),report=Path('/fixture/report'),backup_root=Path('/fixture/backup'),
            historical_report=Path('/fixture/history'),silver_train=None,silver_audit=None,silver_sha256=None,
            silver_audit_sha256=None,resume=False,pilot_only=True,replicate=False)
        trainer,command=trainer_command(args)
        self.assertEqual(trainer.name,'train_gold_focal_comparison.py');self.assertIn('--pilot-only',command)
        args.silver_train=Path('/forbidden/silver.jsonl')
        with self.assertRaises(ValueError):trainer_command(args)

    def test_quality_stop_and_silver_flags_fail_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);argv=[]
            for key in ('data','model','output','report','backup-root','historical-report'):argv += ['--'+key,str(root)]
            (root/'DO_NOT_RESUME').touch()
            with patch.object(focal,'preflight',side_effect=AssertionError('must not load')):
                with self.assertRaisesRegex(ValueError,'cannot resume'):focal.main(argv+['--resume'])
                with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                    focal.main(argv+['--silver-train','forbidden.jsonl'])


if __name__ == '__main__':
    unittest.main()

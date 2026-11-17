"""One gold-only rare-feature recall ablation; no silver, test reads or deployment.

The fixed B recipe changes only positive auxiliary weights for dark_marks and
flaking. Historical same-seed controls are not fresh paired training runs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import validate
from research import train_app_cycle4 as cycle4
from research import train_silver_comparison as controls
from research.app_cycle4_policy import gate, protocol_fingerprint, assert_fingerprint_matches
from research.launch_app_cycle4 import refuse_invalidated_resume

NAME = 'B-json-aux-r8-gold-recall-boost2'
CONFIG = dict(controls.RECIPE, name=NAME, seed=20261116, aux_positive_boost={'dark_marks': 2., 'flaking': 2.})
LIMITATIONS = [
    'Historical same-seed B is not a fresh paired control; validation has already been used for recipe selection.',
    'Positive auxiliary reweighting can trade precision for recall; every original validation gate remains unchanged.',
    'Source-image clusters are not verified patients. This is not clinical or phone-photo generalization evidence.',
    'Only one seed is run. No sealed test is opened, and no adapter is automatically deployed.',
]


def assert_gold_only(gold, validation):
    if len(gold) != 559 or len(validation) != 80:
        raise ValueError('Only the frozen 559 gold train and 80 validation images are allowed')
    for row in gold + validation:
        if (row.get('label_origin') not in {'source_expert_derived', 'source_object_derived'}
                or row.get('subject_id_verified') is not False or row.get('silver_only_train') is True
                or row.get('human_expert_verified') is True or row.get('teacher') is not None
                or type(row.get('loss_weight', 1.)) not in {int, float} or row.get('loss_weight', 1.) != 1.):
            raise ValueError('AI/silver/new weighted inputs cannot enter the gold-only ablation')
        validate(row['target'])


def select_pilot_row(gold):
    """Deterministic training-only selector; never inspect validation outcomes."""
    def positives(row):
        return {item['tag'] for item in row['target']['observations']} & {'dark_marks', 'flaking'}
    eligible = [row for row in gold if positives(row)]
    if not eligible:
        raise ValueError('A rare-feature positive gold training image is required for the pilot')
    return min(eligible, key=lambda row: (-len(positives(row)), row['id']))


def initialize_report(report, fingerprint, resume):
    # The shared helper checks its own __file__; recognize only this launcher's
    # exact current-process bootstrap before delegating all ordinary cases.
    entries = {path.name for path in report.iterdir()} if report.exists() else set()
    if not resume and entries and entries <= {'worker.log', 'job.json'} and 'job.json' in entries:
        job = json.loads((report / 'job.json').read_text())
        if job.get('pid') == os.getpid() and job.get('trainer_sha256') == cycle4.file_hash(Path(__file__)):
            return
    controls.initialize_report(report, fingerprint, resume)


def preflight(args):
    for key in ('PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD'):
        if os.environ.get(key):
            raise RuntimeError('Unsanitized training environment: ' + key)
    if any(os.environ.get(key) != '1' for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE')):
        raise RuntimeError('Explicit offline model mode is required')
    for path, parent in ((args.output, '/mnt/goodagents/checkpoints/skin-appearance-decision'),
                         (args.report, '/mnt/goodagents/outputs/skin-vlm'),
                         (args.backup_root, '/mnt/goodagents/checkpoint-backups')):
        if not path.resolve().is_relative_to(Path(parent)) or path.resolve() == Path(parent):
            raise ValueError('Choose a fresh research child directory under ' + parent)
    if not args.historical_report.resolve().is_relative_to(Path('/mnt/goodagents')):
        raise ValueError('Historical controls must stay on persistent storage')
    protocol, gold, validation = cycle4.preflight(args)
    assert_gold_only(gold, validation)
    historical = controls.historical_controls(args.historical_report, validation, protocol)
    cycle4.positive_auxiliary_boost(CONFIG)
    steps = math.ceil(len(gold) / CONFIG['accumulation']) * CONFIG['epochs']
    if steps != 140:
        raise ValueError('The fixed gold-only recipe must have exactly 140 optimizer steps')
    protocol.update(experiment='gold_only_rare_positive_auxiliary_recall_ablation', grid=[CONFIG],
        replication_seeds=[CONFIG['seed']], train_images=559, validation_images=80,
        gold_only=True, silver_input_paths_allowed=False, AI_silver_used_for_training=False,
        new_training_sources_used=False, historical_control=historical, optimizer_steps_per_seed=steps,
        objective='Original B full-JSON plus .25 auxiliary CE; dark_marks/flaking positive auxiliary multiplier x2; negatives unchanged',
        aux_weights='Original train-only balanced class weights and fixed feature weight sum7; extra x2 only for true dark_marks/flaking',
        pilot_selection='Maximum rare-positive tags then ID, gold train only; no validation outcomes used',
        validation_selection='One fixed seed, all 80 frozen gold validation images, unchanged original gates; no automatic additional seeds',
        rough_timing_reference=dict(historical_training_steps=140, historical_training_seconds=controls.REFERENCE_TRAINING_SECONDS,
            historical_validation_inference_seconds=controls.REFERENCE_VALIDATION_SECONDS,
            excludes='SHA/model loading, checkpoint IO and production-priority pauses; not a guarantee'),
        pending_or_AI_gold_used=False, test_evaluated=False, automatic_deployment=False, limitations=LIMITATIONS)
    return protocol, gold, validation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'model', 'output', 'report', 'backup-root', 'historical-report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--pilot-only', action='store_true')
    args = parser.parse_args(argv)
    refuse_invalidated_resume(args.report, args.resume)
    protocol, gold, validation = preflight(args)
    fingerprint = protocol_fingerprint(protocol)
    initialize_report(args.report, fingerprint, args.resume)
    if not (args.report / 'protocol.json').exists() and args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError('Existing outputs without a frozen protocol cannot be reused')
    args.report.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    cycle4.write(args.report / 'protocol.json', dict(protocol, fingerprint=fingerprint))
    monitor = cycle4.HealthMonitor(args.report / 'service-concurrency.jsonl')
    if not monitor.probe()['ok']:
        raise RuntimeError('Production service probe failed before model load')
    monitor.thread.start()
    admission = cycle4.PhotoAdmission(args.report / 'photo-priority-events.jsonl')
    lineage = dict(protocol_fingerprint=fingerprint, runtime=protocol['runtime'],
        manifest_sha256=cycle4.FROZEN, base_model=cycle4.REPO, revision=cycle4.REVISION,
        gold_only=True, aux_positive_boost=CONFIG['aux_positive_boost'])
    def status(value, **extra):
        cycle4.write(args.report / 'status.json', dict(status=value, fingerprint=fingerprint,
            gold_only=True, test_evaluated=False, automatic_deployment=False, **extra))
    try:
        admission.start()
        status('loading_base')
        cycle4.yield_to_photo(admission, monitor)
        engine = cycle4.SkinVLM(str(args.model))
        pilot_path = args.report / 'pilot.json'
        if not pilot_path.exists():
            row = select_pilot_row(gold)
            pilot_config = dict(CONFIG, name='pilot-' + NAME)
            status('one_step_gold_positive_pilot', optimizer_steps=1,
                training_progress_path=str(args.output / (pilot_config['name'] + '-progress.json')))
            summary = cycle4.train(engine, [row], pilot_config, args.output / pilot_config['name'], args.backup_root,
                lineage, monitor, admission, args.resume, pilot=True, class_weight_rows=gold)
            cycle4.write(pilot_path, dict(fingerprint=fingerprint, training=summary, gold_only=True,
                positive_tags_exercised=sorted({item['tag'] for item in row['target']['observations']} & {'dark_marks', 'flaking'})))
            cycle4.detach_adapter(engine)
        else:
            if not args.resume:
                raise FileExistsError('Pilot result exists without --resume')
            assert_fingerprint_matches(json.loads(pilot_path.read_text())['fingerprint'], fingerprint)
        if args.pilot_only:
            status('pilot_complete', optimizer_steps=1, validation_evaluated=False)
            return
        status('training_gold_recall', candidate=NAME, optimizer_steps=140,
            training_progress_path=str(args.output / (NAME + '-progress.json')),
            estimated_training_seconds=controls.REFERENCE_TRAINING_SECONDS)
        summary = cycle4.train(engine, gold, CONFIG, args.output / NAME, args.backup_root,
            lineage, monitor, admission, args.resume)
        status('gold_validation_only', candidate=NAME, validation_images=80,
            validation_progress_path=str(args.report / (NAME + '-val.progress.json')),
            estimated_validation_inference_seconds=controls.REFERENCE_VALIDATION_SECONDS)
        metrics = cycle4.evaluate(engine, validation, args.report / (NAME + '-val.json'),
            fingerprint, monitor, admission, args.resume)
        historical = protocol['historical_control']['seed_metrics'][str(CONFIG['seed'])]
        check = gate(metrics)
        result = dict(name=NAME, config=CONFIG, seed=CONFIG['seed'], validation=metrics, gate=check,
            historical_macro_f1=historical['macro_f1'], historical_macro_f1_gain=metrics['macro_f1'] - historical['macro_f1'],
            per_tag_historical_changes={tag: {key: metrics['by_tag'][tag][key] - historical['by_tag'][tag][key]
                if metrics['by_tag'][tag][key] is not None and historical['by_tag'][tag][key] is not None else None
                for key in ('precision', 'recall', 'f1')} for tag in sorted(cycle4.TAGS)},
            parameters=summary['trainable_parameters'], adapter=summary['checkpoint'])
        cycle4.write(args.report / 'selection.json', dict(status='one_seed_complete', fingerprint=fingerprint,
            candidates=[result], historical_control=protocol['historical_control'], selected_research_adapter=None,
            additional_seeds_started=False, additional_seed_review_permitted=check['eligible'], gold_only=True,
            test_evaluated=False, automatic_deployment=False, research_only=True,
            clinical_validation=False, phone_photo_evaluation=False, limitations=LIMITATIONS))
        cycle4.write(args.report / 'progress.json', dict(fingerprint=fingerprint, completed=1, total=1, results=[result]))
        status('complete', completed_seeds=1, validation_gate_eligible=check['eligible'])
    except cycle4.ServicePause as exc:
        status('suspended_for_service_health', error_type=type(exc).__name__)
        raise SystemExit(75) from exc
    except Exception as exc:
        status('failed', error_type=type(exc).__name__)
        raise
    finally:
        monitor.stop.set()
        admission.close()


if __name__ == '__main__':
    main()

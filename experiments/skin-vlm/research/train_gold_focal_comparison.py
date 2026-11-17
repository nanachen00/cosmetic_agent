"""Fresh gold-only CE versus asymmetric decoder-boolean focal auxiliary loss.

One predeclared gamma setting is an exploratory hypothesis, not a medical
optimum. Both trials start from the same unmerged official base with a reset
seed and optimizer. No silver/new data, sealed test or automatic deployment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research import train_app_cycle4 as cycle4
from research import train_gold_recall_ablation as gold
from research import train_silver_comparison as controls
from research.app_cycle4_policy import gate, protocol_fingerprint, assert_fingerprint_matches
from research.launch_app_cycle4 import refuse_invalidated_resume

GAMMAS = {'true': 1., 'false': 2.}
CE = dict(controls.RECIPE, name='B-fresh-ce-r8', seed=20261116)
FOCAL = dict(controls.RECIPE, name='F-fresh-asym-focal-r8', seed=20261116, focal_gammas=GAMMAS)
CONFIGS = [CE, FOCAL]
IMAGE_BUDGET = {'shortest_edge': 256 * 32 * 32, 'longest_edge': 768 * 32 * 32}
REFERENCES = {
    'original_focal_paper': 'https://arxiv.org/abs/1708.02002v2',
    'official_torchvision_focal_implementation': 'https://docs.pytorch.org/vision/main/_modules/torchvision/ops/focal_loss.html',
    'asymmetric_multilabel_paper': 'https://arxiv.org/abs/2009.14119v4',
    'official_ASL_implementation': 'https://github.com/Alibaba-MIIL/ASL/blob/main/src/loss_functions/losses.py',
}
LIMITATIONS = [
    'A fresh same-seed CE control improves the comparison; this remains a single-seed sequential shared-GPU exploration.',
    'Gamma(true)=1, gamma(false)=2 is one predeclared hypothesis, not a proven optimum or clinical theorem.',
    'This full-vocabulary decoder-boolean focal variant is not the binary-head/clipped ASL implementation.',
    'The validation set has already been used for recipe selection; no independent test or phone-photo accuracy claim.',
    'Source-image clusters are not verified patients. No automatic further seeds, selected adapter or deployment.',
]


def select_pilot_row(rows):
    eligible = [row for row in rows if 0 < sum(cycle4.expected(row).values()) < len(cycle4.KEYS)]
    return gold.select_pilot_row(eligible)


def initialize_report(report, fingerprint, resume):
    entries = {path.name for path in report.iterdir()} if report.exists() else set()
    if not resume and entries and entries <= {'worker.log', 'job.json'} and 'job.json' in entries:
        job = json.loads((report / 'job.json').read_text())
        if job.get('pid') == os.getpid() and job.get('trainer_sha256') == cycle4.file_hash(Path(__file__)):
            return
    controls.initialize_report(report, fingerprint, resume)


def preflight(args):
    # Reuse exact NFS, official shard, runtime and expert/COCO provenance guards.
    # This function does not train the older recall-ablation configuration.
    protocol, rows, validation = gold.preflight(args)
    for config in CONFIGS:
        if 'aux_positive_boost' in config:
            raise ValueError('Do not combine focal focusing with the failed positive-boost ablation')
        cycle4.validated_focal_gammas(config)
    protocol.update(experiment='fresh_gold_CE_vs_asymmetric_decoder_boolean_focal', grid=CONFIGS,
        primary_comparison='Fresh same-model/same-data/same-seed CE then focal; both reset LoRA/optimizer/RNG from unmerged official base',
        objective='(Full assistant JSON CE + .25 * auxiliary) / 1.25; focal auxiliary=sum(feature_i * gold_class_i * (1-exp(-CE_i))**gamma_i * CE_i)/7',
        aux_weights='Original gold-only class weights clipped .5..2 and feature weights1/1/1/2/2; fixed denominator7; no positive boost',
        focal_definition=dict(gammas=GAMMAS, probability='p_t=exp(-per-boolean-token full-vocabulary CE)',
            factor_computation='-expm1(-CE) equals 1-p_t without clipping',
            probability_clipping=False, binary_head=False, focal_factor_gradient_detached=False,
            applies_to='All five decoder boolean positions; true/false selects the exponent'),
        processor_image_budget=IMAGE_BUDGET, maximum_input_tokens=4096,
        augmentation='Unchanged EXIF/RGB and image budget; train-only same horizontal mirror for both objectives; no colour changes or label-removing crop',
        pilot_selection='Gold train only: rare-feature positive image containing both true and false boolean targets',
        validation_selection='Fixed CE then focal order, all80 validation images each, original gates, single seed; no automatic trial expansion',
        formula_references=REFERENCES, formula_claim='Research hypothesis, not an optimal medical formula',
        comparison_training_steps=280, candidate_training_steps=140,
        rough_pair_timing=dict(training_seconds=2 * controls.REFERENCE_TRAINING_SECONDS,
            validation_inference_seconds=2 * controls.REFERENCE_VALIDATION_SECONDS,
            excludes='Base loading, IO/checksums and production-priority waits; not a guarantee'),
        limitations=LIMITATIONS)
    return protocol, rows, validation


def verify_focal_pilot_formula(engine, row, monitor, admission):
    import torch
    truth = cycle4.expected(row)
    positive = sum(truth.values())
    if not 0 < positive < len(cycle4.KEYS):
        raise ValueError('Pilot must exercise both true and false focal exponents')
    cycle4.yield_to_photo(admission, monitor)
    with torch.no_grad():
        losses = cycle4.sliced_loss(engine.model, cycle4.encode_aux(engine.processor, row))
        identity = cycle4.focal_auxiliary_losses(losses, truth, {'true': 0., 'false': 0.})
        focused = cycle4.focal_auxiliary_losses(losses, truth, GAMMAS)
    cycle4.yield_to_photo(admission, monitor)
    if not torch.equal(identity, losses) or not torch.isfinite(focused).all():
        raise RuntimeError('Focal pilot gamma0 identity or finite-loss check failed')
    text_config = getattr(engine.model.config, 'text_config', engine.model.config)
    vocabulary_size = int(text_config.vocab_size)
    if vocabulary_size <= 2:
        raise ValueError('A binary auxiliary head is not this declared experiment')
    return dict(boolean_slots=len(losses), positive_slots=positive, negative_slots=len(truth) - positive,
        gamma_zero_bitwise_equals_original_CE=True, focused_losses_finite=True,
        full_vocabulary_size=vocabulary_size, gammas=GAMMAS, probability_clipping=False,
        full_model_or_adapter_updated_by_check=False)


def paired_summary(results):
    if len(results) != 2 or [row['name'] for row in results] != [CE['name'], FOCAL['name']]:
        raise ValueError('Exactly the predeclared fresh CE and focal results are required')
    control, focal = results
    if control['parameters'] != focal['parameters']:
        raise ValueError('Fresh comparison changed model capacity')
    return dict(kind='fresh_same_seed_CE_vs_focal', seed=20261116,
        control_macro_f1=control['validation']['macro_f1'], focal_macro_f1=focal['validation']['macro_f1'],
        macro_f1_difference=focal['validation']['macro_f1'] - control['validation']['macro_f1'],
        per_tag_changes={tag: {key: focal['validation']['by_tag'][tag][key] - control['validation']['by_tag'][tag][key]
            if focal['validation']['by_tag'][tag][key] is not None and control['validation']['by_tag'][tag][key] is not None else None
            for key in ('precision', 'recall', 'f1')} for tag in sorted(cycle4.TAGS)},
        control_gate=gate(control['validation']), focal_gate=gate(focal['validation']),
        single_seed=True, validation_reused_for_selection=True, independent_generalization_proven=False,
        additional_seeds_started=False, automatic_deployment=False, test_evaluated=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'model', 'output', 'report', 'backup-root', 'historical-report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--pilot-only', action='store_true')
    args = parser.parse_args(argv)
    refuse_invalidated_resume(args.report, args.resume)
    protocol, rows, validation = preflight(args)
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
        gold_only=True, formula='Fresh CE versus full-vocabulary asymmetric focal auxiliary')
    def status(value, **extra):
        cycle4.write(args.report / 'status.json', dict(status=value, fingerprint=fingerprint,
            gold_only=True, test_evaluated=False, automatic_deployment=False, **extra))
    try:
        admission.start()
        status('loading_base')
        cycle4.yield_to_photo(admission, monitor)
        engine = cycle4.SkinVLM(str(args.model))
        if engine.processor.image_processor.size != IMAGE_BUDGET:
            raise ValueError('Image budget changed; keep the declared existing processor limits')
        pilot_path = args.report / 'pilot.json'
        if not pilot_path.exists():
            row = select_pilot_row(rows)
            status('one_step_gold_focal_pilot', optimizer_steps=1)
            formula_check = verify_focal_pilot_formula(engine, row, monitor, admission)
            config = dict(FOCAL, name='pilot-' + FOCAL['name'])
            summary = cycle4.train(engine, [row], config, args.output / config['name'], args.backup_root,
                lineage, monitor, admission, args.resume, pilot=True, class_weight_rows=rows)
            cycle4.write(pilot_path, dict(fingerprint=fingerprint, formula_check=formula_check, training=summary))
            cycle4.detach_adapter(engine)
        else:
            if not args.resume:
                raise FileExistsError('Pilot result exists without --resume')
            assert_fingerprint_matches(json.loads(pilot_path.read_text())['fingerprint'], fingerprint)
        if args.pilot_only:
            status('pilot_complete', optimizer_steps=1, validation_evaluated=False)
            return
        results = []
        for config in CONFIGS:
            name = config['name']
            status('training_fresh_gold_candidate', candidate=name, candidate_index=len(results) + 1,
                candidate_total=2, optimizer_steps=140,
                training_progress_path=str(args.output / (name + '-progress.json')))
            summary = cycle4.train(engine, rows, config, args.output / name, args.backup_root,
                lineage, monitor, admission, args.resume)
            if summary['steps'] != 140 or summary['training_images'] != 559 or summary['visual_trainable_parameters'] != 0:
                raise RuntimeError('Fresh comparison changed declared steps, data or trainable modules')
            status('gold_validation_only', candidate=name, candidate_index=len(results) + 1,
                candidate_total=2, validation_images=80,
                validation_progress_path=str(args.report / (name + '-val.progress.json')))
            metrics = cycle4.evaluate(engine, validation, args.report / (name + '-val.json'),
                fingerprint, monitor, admission, args.resume)
            result = dict(name=name, config=config, seed=config['seed'], validation=metrics, gate=gate(metrics),
                parameters=summary['trainable_parameters'], adapter=summary['checkpoint'],
                training_elapsed_this_invocation_seconds=summary['elapsed_seconds'])
            results.append(result)
            cycle4.write(args.report / 'progress.json', dict(fingerprint=fingerprint, completed=len(results), total=2, results=results))
            cycle4.detach_adapter(engine)  # unload(), never merge CE weights into focal's base.
        comparison = paired_summary(results)
        cycle4.write(args.report / 'selection.json', dict(status='fresh_pair_complete', fingerprint=fingerprint,
            candidates=results, paired_comparison=comparison, selected_research_adapter=None,
            additional_seeds_started=False, gold_only=True, test_evaluated=False, automatic_deployment=False,
            research_only=True, clinical_validation=False, phone_photo_evaluation=False, limitations=LIMITATIONS))
        status('complete', completed_candidates=2, paired_macro_f1_difference=comparison['macro_f1_difference'])
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

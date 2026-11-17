"""Separate, low-weight AI-silver experiment; gold splits and test stay sealed.

Reuse the cycle4 optimizer, exact checkpoint resume, full JSON/auxiliary losses,
photo admission and cooperative validation. Historical controls are not fresh
paired runs. Nothing here deploys an adapter or starts a serving process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import load_rows
from research import train_app_cycle4 as cycle4
from research.app_cycle4_policy import CONFIGS, SEEDS, gate, protocol_fingerprint, assert_fingerprint_matches
from research.launch_app_cycle4 import refuse_invalidated_resume

HISTORICAL_FINGERPRINT = 'cef58b995aa17f4dc6255dc9f589b95acf7ee918eb1390380b1d707fbec92381'
HISTORICAL_FILES = {
    'protocol.json': 'a5c5ebc87a4166af62f484a9e2c34f04f7e2fbbc06de5ea5d7a1f9aac19d5ad3',
    'selection.json': 'b76368b2ba9d020bd99313cb964e5818ab717d4fb3c5f4d9a85c2e9691f5e897',
    'B-json-aux-r8-val.json': 'cef52512a839356ff1727bb794e68dfdb747a2ae166a1ee0d245dd4709e6ed93',
    'B-json-aux-r8-seed20261117-val.json': '31847690e24a371abfef4b142a0004fdb7632f0e6910331b1bddceb37805fc31',
    'B-json-aux-r8-seed20261118-val.json': '4cc091115d7c2e6bf3a7fd17130d2b4f662eed1a78a4f9f279ede70108508b00',
}
SILVER_WEIGHT = .25
CLEANING_CHECKS = ('sha256', 'md5', 'full_decode', 'quality', 'exact_duplicate',
                   'near_duplicate', 'mirrored_duplicate', 'ocr', 'holdout_exclusion')
RESERVED_CATEGORIES = ('frozen_train', 'frozen_val', 'sealed_test', 'acne_parent', 'external_phone', 'scin_gold')
RECIPE = next(item for item in CONFIGS if item['name'] == 'B-json-aux-r8')
REFERENCE_TRAINING_SECONDS = 1038.722  # r3 B summary: 140 steps, 559 images, no resume.
REFERENCE_VALIDATION_SECONDS = 576.096  # Sum of the 80 r3 B validation inference times.
LIMITATIONS = [
    'AI-only silver targets are weak labels, never gold or independently verified appearance.',
    'Historical same-seed B controls were selected using this validation set; comparisons are exploratory.',
    'Adding silver changes optimizer step count, warmup and shuffle; historical controls do not isolate the data effect.',
    'Source-image clusters are not verified patient identities; no clinical or phone-person generalization claim.',
    'No sealed-test input is opened, even if validation gates pass; no automatic deployment.',
]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(char in '0123456789abcdef' for char in value)


def metrics_equal(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(metrics_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(metrics_equal(a, b) for a, b in zip(left, right))
    if type(left) in {float, int} and type(right) in {float, int}:
        return math.isclose(left, right, rel_tol=0., abs_tol=1e-12)
    return left == right


def read_verified(path, expected):
    path = Path(path)
    if 'test' in path.name.lower():
        raise ValueError('Sealed/test input basename is forbidden')
    if not sha256(expected) or cycle4.file_hash(path) != expected:
        raise ValueError('Input checksum mismatch: ' + path.name)
    return json.loads(path.read_text())


def final_visual_consistency(rows, audit, audit_path):
    """Bind every accepted target to a distinct, hash-verified final AI review.

    Preserve unknowns in raw review evidence. An unknown is never converted to
    false, and a first-pass silver_candidate flag cannot approve this gate.
    """
    reference = audit.get('final_visual_review')
    if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
        raise ValueError('A final per-image visual consistency review is required')
    relative = Path(reference['path'])
    if relative.is_absolute() or not relative.name or '..' in relative.parts:
        raise ValueError('Final review must be a local relative audit artifact')
    directory = Path(audit_path).parent.resolve()
    artifact = directory / relative
    if artifact.is_symlink() or not artifact.resolve().is_relative_to(directory):
        raise ValueError('Final review artifact escapes the audit directory')
    document = read_verified(artifact, reference.get('sha256'))
    decisions = document.get('reviews') if isinstance(document, dict) else None
    if not isinstance(decisions, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in decisions):
        raise ValueError('Final visual review must contain identified decisions')
    by_id = {item['id']: item for item in decisions}
    if len(by_id) != len(decisions):
        raise ValueError('Duplicate IDs in final visual review')
    for row in rows:
        decision = by_id.get(row['id'])
        if decision is None or decision.get('image_sha256') != row['image_sha256']:
            raise ValueError('Final visual review does not bind every accepted image')
        features = decision.get('features')
        if (not isinstance(features, dict) or set(features) != set(cycle4.TAGS)
                or any(type(value) is not bool for value in features.values())):
            raise ValueError('Final visual review has an unknown or missing appearance decision')
        if (decision.get('silver_candidate') is not True or decision.get('quality_flags') != []
                or decision.get('image_scope') not in {'face', 'skin_closeup'}
                or decision['image_scope'] != row['target']['image_scope']
                or {tag for tag, value in features.items() if value} != {item['tag'] for item in row['target']['observations']}):
            raise ValueError('Final visual review disagrees with target or has unresolved quality flags')
        initial_teacher = row.get('teacher', row)
        if (not isinstance(initial_teacher, dict) or decision.get('reviewer_type') != 'codex_visual_assistant'
                or decision.get('model_version') != 'unavailable' or decision.get('human_expert_verified') is not False
                or reference['sha256'] == initial_teacher.get('decisions_sha256')):
            raise ValueError('A separate final AI review with honest non-gold provenance is required')
    return dict(sha256=reference['sha256'], path=relative.as_posix(), accepted_images_checked=len(rows),
        four_tag_decisions_all_boolean=True, unresolved_quality_flags=False,
        target_consistency=True, human_expert_verified=False, model_version='unavailable')


def load_silver(path, manifest_sha256, audit_path, audit_sha256, gold, validation):
    """Require a frozen upstream cleaning audit; independently check bytes/quality.

    Near-duplicate/mirror/OCR/holdout checks are upstream audit evidence, not a
    claim that this trainer reruns OCR or reads sealed test images/labels.
    """
    path = Path(path)
    if path.name != 'silver_only_train.jsonl':
        raise ValueError('A separate silver_only_train.jsonl manifest is required')
    audit = read_verified(audit_path, audit_sha256)
    if not sha256(manifest_sha256) or cycle4.file_hash(path) != manifest_sha256:
        raise ValueError('Silver manifest checksum mismatch')
    if audit.get('manifest_sha256') != manifest_sha256 or audit.get('silver_only_train') is not True:
        raise ValueError('Cleaning audit does not bind this silver-only manifest')
    checks = audit.get('cleaning_checks', {})
    if any(checks.get(key) is not True for key in CLEANING_CHECKS if key != 'md5'):
        raise ValueError('All cleaning and holdout-exclusion checks must pass')
    if checks.get('md5') is not True and not (checks.get('md5') == 'unavailable' and audit.get('md5_status_reason')):
        raise ValueError('MD5 must be verified, or honestly marked unavailable with a reason')
    if not set(RESERVED_CATEGORIES).issubset(audit.get('reserved_categories', [])):
        raise ValueError('Cleaning audit must cover every reserved source group')
    if not sha256(audit.get('exclusion_index_sha256')):
        raise ValueError('Frozen metadata-only exclusion index checksum required')
    coverage = audit.get('reserved_coverage', {})
    for category in RESERVED_CATEGORIES:
        entry = coverage.get(category, {})
        if not isinstance(entry, dict) or type(entry.get('records')) is not int or entry['records'] < 0:
            raise ValueError('Explicit reserved source coverage required: ' + category)
        checked = entry.get('status') == 'checked' and entry['records'] > 0
        not_applicable = (category == 'scin_gold' and entry.get('status') == 'not_applicable'
            and entry['records'] == 0 and bool(entry.get('reason')))
        if not checked and not not_applicable:
            raise ValueError('Missing reserved source coverage: ' + category)
    reviews = audit.get('review_artifacts')
    if reviews is None:
        reviews = [{key: audit.get(key) for key in ('review_protocol_sha256', 'decisions_sha256')}]
    if not isinstance(reviews, list) or not reviews or any(not isinstance(item, dict) or
            not sha256(item.get('review_protocol_sha256')) or not sha256(item.get('decisions_sha256')) for item in reviews):
        raise ValueError('Review protocol and AI decision checksums required')
    review_pairs = {(item['review_protocol_sha256'], item['decisions_sha256']) for item in reviews}
    rows = load_rows(path)
    accepted = audit.get('accepted_ids')
    if not isinstance(accepted, list) or len(accepted) != len(set(accepted)) or set(accepted) != {row['id'] for row in rows}:
        raise ValueError('Cleaning audit accepted IDs differ from the manifest')
    final_review = final_visual_consistency(rows, audit, audit_path)
    reserved = gold + validation
    reserved_ids = {row['id'] for row in reserved}
    reserved_sha = {row['image_sha256'] for row in reserved}
    reserved_groups = {row['subject_id'] for row in reserved}
    seen_sha, seen_groups = set(), set()
    for row in rows:
        if (row.get('label_origin') != 'ai_silver' or row.get('silver_only_train') is not True
                or row.get('subject_id_verified') is not False or row.get('gold_eligible') is not False
                or type(row.get('loss_weight')) not in {int, float} or row['loss_weight'] != SILVER_WEIGHT):
            raise ValueError('Each AI-only silver row must explicitly retain weight .25 and non-gold origin')
        teacher = row.get('teacher', row)
        if (not isinstance(teacher, dict) or teacher.get('reviewer_type') not in {'codex_visual_assistant', 'conversation_assistant'}
                or teacher.get('model_version') != 'unavailable'
                or (teacher.get('review_protocol_sha256'), teacher.get('decisions_sha256')) not in review_pairs):
            raise ValueError('Actual AI reviewer provenance required; do not invent a teacher revision')
        image = Path(row['image'])
        if not image.resolve().is_relative_to(Path('/mnt/goodagents')):
            raise ValueError('Silver images must remain on persistent storage')
        if not sha256(row.get('image_sha256')) or cycle4.file_hash(image) != row['image_sha256']:
            raise ValueError('Silver image bytes changed')
        if (row['id'] in reserved_ids or row['image_sha256'] in reserved_sha or row['subject_id'] in reserved_groups
                or row['image_sha256'] in seen_sha):
            raise ValueError('Gold/validation overlap or duplicate silver image')
        # Multiple images in one declared capture group are allowed only if the
        # upstream cleaning audit has excluded all reserved groups as a unit.
        seen_sha.add(row['image_sha256'])
        seen_groups.add(row['subject_id'])
        _, quality = cycle4.open_image(image)
        if quality['reasons'] or row['target']['image_scope'] == 'uncertain':
            raise ValueError('Uncertain/poor-quality silver cannot train as a negative')
        row['note'] = ''  # Captions/source hints must not substitute for image evidence.
    return rows, dict(manifest_sha256=manifest_sha256, audit_sha256=audit_sha256,
        exclusion_index_sha256=audit['exclusion_index_sha256'], review_artifacts=reviews,
        sample_count=len(rows), source_image_clusters=len(seen_groups), loss_weight=SILVER_WEIGHT,
        label_origin='ai_silver', teacher_model_version='unavailable',
        upstream_cleaning_checks=checks, reserved_coverage=coverage, final_visual_review=final_review,
        sealed_test_inputs_opened=False)


def historical_controls(folder, validation, current_protocol):
    folder = Path(folder)
    if any(cycle4.file_hash(folder / name) != expected for name, expected in HISTORICAL_FILES.items()):
        raise ValueError('Independently audited historical control checksum differs')
    protocol = json.loads((folder / 'protocol.json').read_text())
    assert_fingerprint_matches(protocol, HISTORICAL_FINGERPRINT)
    if (protocol['data_manifest_sha256'] != cycle4.FROZEN or protocol['runtime'] != current_protocol['runtime']
            or protocol['model'] != current_protocol['model'] or protocol['generation'] != current_protocol['generation']):
        raise ValueError('Historical data/model/runtime/generation is not comparable')
    selection = json.loads((folder / 'selection.json').read_text())
    if selection.get('fingerprint') != HISTORICAL_FINGERPRINT or selection.get('test_evaluated') is not False:
        raise ValueError('Historical control lineage is invalid')
    hashes = dict(HISTORICAL_FILES)
    metrics = {}
    for seed in SEEDS:
        name = 'B-json-aux-r8' + ('' if seed == SEEDS[0] else '-seed' + str(seed))
        result = next((item for item in selection['candidates'] if item['name'] == name), None)
        if result is None or result['config'] != dict(RECIPE, name=name, seed=seed):
            raise ValueError('Historical B recipe/seed differs')
        path = folder / (name + '-val.json')
        report = json.loads(path.read_text())
        if report.get('fingerprint') != HISTORICAL_FINGERPRINT:
            raise ValueError('Historical validation fingerprint differs')
        predictions = report['predictions']
        if ([item['id'] for item in predictions] != [row['id'] for row in validation]
                or [item.get('image_sha256') for item in predictions] != [row['image_sha256'] for row in validation]):
            raise ValueError('Historical validation IDs/image bytes differ')
        measured = cycle4.score(validation, predictions)
        measured['macro_f1'] = math.fsum(measured['by_tag'][tag]['f1'] or 0 for tag in sorted(cycle4.TAGS)) / len(cycle4.TAGS)
        if not metrics_equal(measured, report['metrics']) or not metrics_equal(measured, result['validation']):
            raise ValueError('Historical metrics do not match the predictions')
        # Preserve hash-pinned report values in the protocol. Recomputed floating
        # sums are checks, not new lineage: set iteration can vary by hash seed.
        metrics[str(seed)] = report['metrics']
        hashes[path.name] = cycle4.file_hash(path)
    return dict(kind='historical_control_not_fresh_paired', fingerprint=HISTORICAL_FINGERPRINT,
        recipe=RECIPE, seed_metrics=metrics, file_sha256=hashes, limitations=LIMITATIONS)


def replication_gate(metrics, historical):
    result = gate(metrics)
    gain = result['macro_f1'] - historical['macro_f1'] if result['macro_f1'] is not None else None
    return dict(allowed=result['eligible'] and gain is not None and gain >= .02,
        validation_gate=result, historical_macro_f1_gain=gain,
        rule='First mixed seed must pass every unchanged cycle4 gate and gain at least .02 vs historical same-seed B',
        automatic_deployment=False, sealed_test_inputs_opened=False)


def matched_stability(results, historical):
    """Require all fixed seeds and compare each with its historical same seed."""
    seeds = [item['seed'] for item in results]
    complete = len(seeds) == len(SEEDS) and set(seeds) == set(SEEDS)
    gains = [item['validation']['macro_f1'] - historical[str(item['seed'])]['macro_f1'] for item in results]
    all_pass = complete and all(gate(item['validation'])['eligible'] for item in results)
    nonregressing = complete and min(gains) >= 0
    median_gain = statistics.median(gains) if gains else None
    eligible = all_pass and nonregressing and median_gain is not None and median_gain >= .02
    return dict(eligible=eligible, expected_seeds=list(SEEDS), observed_seeds=seeds,
        comparison='Historical same-seed B controls, not fresh paired training',
        all_three_seeds_pass=all_pass, all_seeds_nonregressing=nonregressing,
        matched_historical_macro_f1_gains=gains, median_validation_gain=median_gain,
        automatic_deployment=False, test_evaluated=False)


def initialize_report(report, fingerprint, resume):
    entries = {path.name for path in report.iterdir()} if report.exists() else set()
    if not entries:
        return
    if resume:
        assert_fingerprint_matches(json.loads((report / 'protocol.json').read_text()), fingerprint)
        return
    if entries <= {'worker.log', 'job.json'} and 'job.json' in entries:
        job = json.loads((report / 'job.json').read_text())
        if job.get('pid') == os.getpid() and job.get('trainer_sha256') == cycle4.file_hash(Path(__file__)):
            return
    raise FileExistsError('Fresh report required without explicit --resume')


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
            raise ValueError('Choose a distinct research child directory under ' + parent)
    for path in (args.silver_train, args.silver_audit, args.historical_report):
        if not path.resolve().is_relative_to(Path('/mnt/goodagents')):
            raise ValueError('Silver/control inputs must stay on the verified persistent mount')
    protocol, gold, validation = cycle4.preflight(args)
    silver, silver_info = load_silver(args.silver_train, args.silver_sha256,
        args.silver_audit, args.silver_audit_sha256, gold, validation)
    historical = historical_controls(args.historical_report, validation, protocol)
    gold = [dict(row, loss_weight=1.) for row in gold]
    rows = gold + silver
    protocol.update(experiment='gold559_plus_independent_AI_silver_only_train',
        silver=silver_info, historical_control=historical, gold_train_images=len(gold),
        train_images=len(rows), validation_images=len(validation), grid=[RECIPE],
        loss_weighting=cycle4.loss_weighting(rows, 'loss_weight', gold)[1],
        objective='Mean-example loss: w_i * (full assistant JSON CE + .25 * gold-balanced auxiliary CE) / 1.25; gold1, silver.25',
        sampling='Each original gold and silver source image once per epoch, same train-only mirror; no oversampling',
        optimizer_steps_per_seed=math.ceil(len(rows) / RECIPE['accumulation']) * RECIPE['epochs'],
        rough_timing_reference=dict(historical_training_seconds=REFERENCE_TRAINING_SECONDS,
            historical_training_steps=140, historical_validation_inference_seconds=REFERENCE_VALIDATION_SECONDS,
            mixed_training_seconds_by_step_ratio=REFERENCE_TRAINING_SECONDS *
                (math.ceil(len(rows) / RECIPE['accumulation']) * RECIPE['epochs']) / 140,
            excludes='Base loading, SHA verification, scheduler/sequence-length differences, checkpoint IO and production-priority pauses; not a latency guarantee'),
        validation_selection='One mixed seed first; unchanged gates and +.02 vs historical same-seed B before explicit --replicate',
        AI_silver_used_for_training=True, pending_or_AI_gold_used=False, test_evaluated=False,
        automatic_deployment=False, limitations=LIMITATIONS)
    return protocol, gold, silver, validation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'model', 'output', 'report', 'backup-root', 'silver-train', 'silver-audit', 'historical-report'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('silver-sha256', 'silver-audit-sha256'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--pilot-only', action='store_true')
    parser.add_argument('--replicate', action='store_true', help='Explicitly permit two more seeds, only after first-seed gates pass')
    args = parser.parse_args(argv)
    refuse_invalidated_resume(args.report, args.resume)
    if args.pilot_only and args.replicate:
        parser.error('--pilot-only and --replicate cannot be combined')
    protocol, gold, silver, validation = preflight(args)
    fingerprint = protocol_fingerprint(protocol)
    initialize_report(args.report, fingerprint, args.resume)
    if not (args.report / 'protocol.json').exists() and args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError('Existing outputs without frozen report cannot be reused')
    args.report.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    cycle4.write(args.report / 'protocol.json', dict(protocol, fingerprint=fingerprint))
    monitor = cycle4.HealthMonitor(args.report / 'service-concurrency.jsonl')
    if not monitor.probe()['ok']:
        raise RuntimeError('Production service probe failed before model load')
    monitor.thread.start()
    admission = cycle4.PhotoAdmission(args.report / 'photo-priority-events.jsonl')
    lineage = dict(protocol_fingerprint=fingerprint, runtime=protocol['runtime'],
        manifest_sha256=cycle4.FROZEN, silver=protocol['silver'], base_model=cycle4.REPO, revision=cycle4.REVISION)
    results = []
    def status(value, **extra):
        cycle4.write(args.report / 'status.json', dict(status=value, fingerprint=fingerprint,
            test_evaluated=False, automatic_deployment=False, **extra))
    try:
        admission.start()
        status('loading_base')
        cycle4.yield_to_photo(admission, monitor)
        engine = cycle4.SkinVLM(str(args.model))
        pilot_path = args.report / 'pilot.json'
        if not pilot_path.exists():
            config = dict(RECIPE, name='pilot-B-json-aux-r8-silver', seed=SEEDS[0])
            status('one_step_silver_pilot', optimizer_steps=1,
                training_progress_path=str(args.output / (config['name'] + '-progress.json')))
            # The one-step pilot actually exercises a silver image at weight .25.
            summary = cycle4.train(engine, silver[:1], config, args.output / config['name'], args.backup_root,
                lineage, monitor, admission, args.resume, pilot=True,
                sample_weight_key='loss_weight', class_weight_rows=gold)
            cycle4.write(pilot_path, dict(fingerprint=fingerprint, training=summary, silver_weight_exercised=SILVER_WEIGHT))
            cycle4.detach_adapter(engine)
        else:
            if not args.resume:
                raise FileExistsError('Pilot result exists without --resume')
            assert_fingerprint_matches(json.loads(pilot_path.read_text())['fingerprint'], fingerprint)
        if args.pilot_only:
            status('pilot_complete', optimizer_steps=1, validation_evaluated=False)
            return
        for seed in SEEDS:
            if seed != SEEDS[0] and (not args.replicate or not replication_gate(
                    results[0]['validation'], protocol['historical_control']['seed_metrics'][str(SEEDS[0])])['allowed']):
                break
            name = 'B-json-aux-r8-silver' + ('' if seed == SEEDS[0] else '-seed' + str(seed))
            config = dict(RECIPE, name=name, seed=seed)
            status('training_mixed', candidate=name, optimizer_steps=protocol['optimizer_steps_per_seed'],
                training_progress_path=str(args.output / (name + '-progress.json')),
                estimated_training_seconds=protocol['rough_timing_reference']['mixed_training_seconds_by_step_ratio'])
            summary = cycle4.train(engine, gold + silver, config, args.output / name, args.backup_root,
                lineage, monitor, admission, args.resume, sample_weight_key='loss_weight', class_weight_rows=gold)
            status('gold_validation_only', candidate=name, validation_images=len(validation),
                validation_progress_path=str(args.report / (name + '-val.progress.json')),
                estimated_validation_inference_seconds=REFERENCE_VALIDATION_SECONDS)
            metrics = cycle4.evaluate(engine, validation, args.report / (name + '-val.json'),
                fingerprint, monitor, admission, args.resume)
            historical = protocol['historical_control']['seed_metrics'][str(seed)]
            results.append(dict(name=name, config=config, seed=seed, validation=metrics, gate=gate(metrics),
                historical_macro_f1=historical['macro_f1'], historical_macro_f1_gain=metrics['macro_f1'] - historical['macro_f1'],
                parameters=summary['trainable_parameters'], adapter=summary['checkpoint']))
            cycle4.write(args.report / 'progress.json', dict(fingerprint=fingerprint, results=results))
            cycle4.detach_adapter(engine)
        replication = replication_gate(results[0]['validation'], protocol['historical_control']['seed_metrics'][str(SEEDS[0])])
        stability = matched_stability(results, protocol['historical_control']['seed_metrics'])
        complete = len(results) == len(SEEDS)
        cycle4.write(args.report / 'selection.json', dict(status='three_seed_complete' if complete else 'one_seed_complete',
            fingerprint=fingerprint, candidates=results, replication=replication, stability=stability,
            selected_research_adapter=results[0]['adapter'] if complete and stability['eligible'] else None,
            historical_control=protocol['historical_control'], test_evaluated=False, automatic_deployment=False,
            research_only=True, clinical_validation=False, phone_photo_evaluation=False, limitations=LIMITATIONS))
        status('complete', completed_seeds=len(results), replication_allowed=replication['allowed'])
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

"""Fixed full-JSON research policy. No model imports, test reads or deployment."""
from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal
from pathlib import Path
import statistics

SEEDS = (20261116, 20261117, 20261118)
TAGS = ('acne_like_bumps', 'redness', 'dark_marks', 'flaking')
AUX_TAG_WEIGHTS = {'skin_visible': 1., 'acne_like_bumps': 1., 'redness': 1., 'dark_marks': 2., 'flaking': 2.}
COMMON = dict(epochs=2, accumulation=8, dropout=.05, weight_decay=.01,
              warmup_ratio=.05, gradient_clip=1., lr=3e-5)
CONFIGS = [
    dict(COMMON, name='A-json-r8', variant='A', rank=8, alpha=16, aux_weight=0., visual_lr=0., modules='language_attention_mlp'),
    dict(COMMON, name='B-json-aux-r8', variant='B', rank=8, alpha=16, aux_weight=.25, visual_lr=0., modules='language_attention_mlp'),
    dict(COMMON, name='C-json-aux-visual-r8', variant='C', rank=8, alpha=16, aux_weight=.25, visual_lr=3e-6, modules='language_attention_mlp_visual_merger'),
    dict(COMMON, name='D-json-aux-visual-r16', variant='D', rank=16, alpha=32, aux_weight=.25, visual_lr=3e-6, modules='language_attention_mlp_visual_merger'),
]


def _rate(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def gate(metrics):
    """Gate actual website JSON metrics; missing controls never count as success.

    macro_f1 is derived from the four tag F1s. A supplied macro_f1 must agree.
    A failed baseline is still a useful baseline; this does not imply deployment.
    """
    if not isinstance(metrics, dict):
        raise ValueError('Metrics must be an object')
    failures = []
    value = metrics.get('valid_json_rate')
    if not _rate(value):
        failures.append('valid_json_missing_or_invalid')
    elif value < .98:
        failures.append('valid_json_below_0.98')
    by_tag = metrics.get('by_tag', {})
    f1s = []
    for tag in TAGS:
        values = by_tag.get(tag, {}) if isinstance(by_tag, dict) else {}
        if not isinstance(values, dict):
            values = {}
        for key in ('precision', 'recall', 'f1'):
            value = values.get(key)
            if not _rate(value):
                failures.append(tag + ':' + key + '_missing_or_invalid')
            elif key in {'precision', 'recall'} and value < .8:
                failures.append(tag + ':' + key + '_below_0.80')
        if _rate(values.get('f1')):
            f1s.append(float(values['f1']))
    macro = sum(f1s) / len(TAGS) if len(f1s) == len(TAGS) else None
    if macro is None:
        failures.append('macro_f1_missing_or_invalid')
    elif macro < .8:
        failures.append('macro_f1_below_0.80')
    if 'macro_f1' in metrics and (not _rate(metrics['macro_f1']) or macro is None or abs(metrics['macro_f1'] - macro) > 1e-12):
        failures.append('macro_f1_inconsistent_with_by_tag')
    value = metrics.get('non_skin_correct_rejection_rate')
    if not _rate(value):
        failures.append('object_rejection_missing_or_invalid')
    elif value < .9:
        failures.append('object_rejection_below_0.90')
    return dict(eligible=not failures, failures=failures, macro_f1=macro,
                automatic_deployment=False)


def _validation(result):
    if not isinstance(result, dict):
        raise ValueError('Candidate result must be an object')
    if 'validation' in result:
        return result['validation']
    if 'metrics' in result:
        return result['metrics']
    raise ValueError('Candidate requires validation metrics')


def _rank(result):
    macro = gate(_validation(result))['macro_f1']
    parameters = result.get('parameters', 0)
    if not isinstance(parameters, (int, float)) or isinstance(parameters, bool) or not math.isfinite(parameters) or parameters < 0:
        raise ValueError('Invalid trainable parameter count')
    return (macro if macro is not None else -1., -parameters)


def choose_recipe(results):
    """Choose only eligible results; retain a separate, non-deployable diagnostic."""
    rows = list(results)
    eligible = [row for row in rows if gate(_validation(row))['eligible']]
    selected = max(eligible, key=_rank) if eligible else None
    diagnostic = max(rows, key=_rank) if rows else None
    return dict(selected=selected, diagnostic=diagnostic, eligible=selected is not None,
                automatic_deployment=False, test_evaluated=False)


def stability_report(base_metrics, seed_results):
    """Require all three fixed seeds, all gates, no regressions and median +.02."""
    baseline = gate(base_metrics)['macro_f1']
    if baseline is None:
        raise ValueError('Fresh base requires all four valid F1 metrics')
    rows = list(seed_results)
    seeds = [row.get('seed', row.get('config', {}).get('seed')) for row in rows]
    complete = len(seeds) == len(SEEDS) and all(type(seed) is int for seed in seeds) and set(seeds) == set(SEEDS)
    checks = [gate(_validation(row)) for row in rows]
    valid_macro = all(row['macro_f1'] is not None for row in checks)
    gains = [Decimal(str(row['macro_f1'])) - Decimal(str(baseline)) for row in checks] if valid_macro else []
    median_gain = statistics.median(gains) if gains else None
    all_pass = complete and all(row['eligible'] for row in checks)
    nonregressing = complete and bool(gains) and min(gains) >= 0
    improved = complete and median_gain is not None and median_gain >= Decimal('0.02')
    failures = []
    if not complete: failures.append('three_fixed_replication_seeds_required')
    if not all_pass: failures.append('not_all_three_seeds_pass_gates')
    if not nonregressing: failures.append('seed_regresses_or_incomplete')
    if not improved: failures.append('median_validation_gain_below_0.02_or_incomplete')
    return dict(eligible=all_pass and nonregressing and improved, failures=failures,
        expected_seeds=list(SEEDS), observed_seeds=seeds, all_three_seeds_pass=all_pass,
        all_seeds_nonregressing=nonregressing, median_validation_gain=float(median_gain) if median_gain is not None else None,
        baseline_macro_f1=baseline, replication_macro_f1=[row['macro_f1'] for row in checks],
        seed_gates=checks, automatic_deployment=False, test_evaluated=False)


def protocol_fingerprint(protocol):
    if not isinstance(protocol, dict) or not isinstance(protocol.get('runtime'), dict) or not protocol['runtime']:
        raise ValueError('A nonempty runtime fingerprint is required')
    payload = {key: value for key, value in protocol.items() if key != 'fingerprint'}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def assert_fingerprint_matches(stored, current):
    """Reject reuse across runtime/code/data/model changes, or a tampered record."""
    if isinstance(stored, dict):
        expected = protocol_fingerprint(stored)
        if 'fingerprint' in stored:
            value = stored['fingerprint']
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value) or value.lower() != expected:
                raise ValueError('Stored protocol fingerprint is inconsistent')
    elif isinstance(stored, str):
        if not re.fullmatch(r'[0-9a-fA-F]{64}', stored):
            raise ValueError('Fingerprint must be strict 64hex SHA-256')
        expected = stored.lower()
    else:
        raise ValueError('Stored protocol or fingerprint required')
    if isinstance(current, dict):
        actual = protocol_fingerprint(current)
    elif isinstance(current, str) and re.fullmatch(r'[0-9a-fA-F]{64}', current):
        actual = current.lower()
    else:
        raise ValueError('Current protocol or strict 64hex SHA-256 required')
    if expected != actual:
        raise ValueError('Protocol/runtime changed; fresh run required, refusing reuse')
    return actual


def assert_no_sealed_inputs(train_path, val_path):
    """Check only the two supplied input basenames; never open/stat test data.

    Parent directories and output/report paths are intentionally not classified.
    Dataset provenance and train/validation disjointness belong to the trainer.
    """
    if train_path is None or val_path is None or not str(train_path).strip() or not str(val_path).strip():
        raise ValueError('Both train and validation input files are required')
    paths = [Path(train_path), Path(val_path)]
    if any(not path.name or 'test' in path.name.lower() for path in paths):
        raise ValueError('Sealed test input basename is forbidden')
    return dict(train=str(paths[0]), val=str(paths[1]), sealed_test_inputs=False)

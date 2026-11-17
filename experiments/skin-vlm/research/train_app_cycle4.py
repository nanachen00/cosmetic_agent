"""Fresh full-website-JSON ablations. This entry point never opens a test split.

Training state is saved on persistent NFS, independently from serving runtimes.
Only explicit --resume permits reuse, with code/data/runtime fingerprints checked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from importlib.metadata import version

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import TAGS, parse, envelope
from download_model import REPO, REVISION, WEIGHTS, file_hash
from install_training_from_cache import require_mount
from research.appearance_decisions import class_weights, encode as encode_aux, expected, KEYS
from research.app_cycle4_policy import (CONFIGS, SEEDS, gate, choose_recipe,
    stability_report, protocol_fingerprint, assert_fingerprint_matches, assert_no_sealed_inputs)
from research.app_cycle4_checkpoint import save_checkpoint, load_checkpoint
from research.app_cycle4_admission import PhotoAdmission, PhotoAdmissionTimeout
from research.train_clean_comparison import load_split, write
from research.v3.train_cycle3 import lora_targets
from skin_vlm import SkinVLM, messages_for, open_image
from evaluate import score

FROZEN = {
    'train': '08d6e9fa0ae581926abc3ab7372977cdb6e4c78011ddd0d299659d9d48f951ef',
    'val': '805710681e72f749165271c962bcd255eb9bba40cd88a71f0d1dad18b9decd6b',
}


class ServicePause(RuntimeError):
    """Pause our training at a safe boundary when production probes deteriorate."""


class HealthMonitor:
    def __init__(self, path, interval=30, timeout=2):
        self.path, self.interval, self.timeout = path, interval, timeout
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.count = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def probe(self):
        start = time.monotonic()
        result = {'time': time.time(), 'ok': False}
        try:
            with urllib.request.urlopen('http://127.0.0.1:8025/health', timeout=self.timeout) as response:
                photo = json.load(response)
            if not (photo.get('ready') is True or photo.get('status') == 'ready'):
                raise ValueError('Photo service is not ready')
            request = urllib.request.Request('http://127.0.0.1:8002/v1/embeddings',
                data=json.dumps({'input': ['training concurrency probe'], 'model': 'BAAI/bge-m3'}).encode(),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                embedding = json.load(response)['data'][0]['embedding']
            if len(embedding) != 1024 or any(not math.isfinite(value) for value in embedding):
                raise ValueError('Embedding probe invalid')
            norm = sum(value * value for value in embedding) ** .5
            if not .99 < norm < 1.01:
                raise ValueError('Embedding probe norm invalid')
            result['ok'] = True
            self.count = 0
        except Exception as exc:
            result['error_type'] = type(exc).__name__
            self.count += 1
            if self.count >= 2:
                self.failed.set()
        result['seconds'] = round(time.monotonic() - start, 4)
        result['consecutive_failures'] = self.count
        with self.path.open('a') as stream:
            stream.write(json.dumps(result) + '\n')
        return result

    def run(self):
        while not self.stop.wait(self.interval):
            self.probe()

    def check(self):
        if self.failed.is_set():
            raise ServicePause('Two concurrent service probes failed; training paused with state preserved')


def encode_json(processor, row, flip=False):
    """Supervise every assistant JSON token and EOS, with the causal shift explicit."""
    import torch
    from PIL import ImageOps
    image, quality = open_image(row['image'])
    if quality['reasons']:
        raise ValueError('Frozen clean image failed quality checks: ' + row['id'])
    if flip:
        image = ImageOps.mirror(image)
    prompt = messages_for(image, row.get('note', ''))
    full = prompt + [{'role': 'assistant', 'content': [{'type': 'text',
        'text': json.dumps(row['target'], ensure_ascii=False, separators=(',', ':'))}]}]
    prefix = processor.apply_chat_template(prompt, tokenize=True, add_generation_prompt=True,
        return_dict=True, return_tensors='pt')
    batch = processor.apply_chat_template(full, tokenize=True, add_generation_prompt=False,
        return_dict=True, return_tensors='pt')
    batch.pop('token_type_ids', None)
    length = prefix['input_ids'].shape[1]
    if not torch.equal(prefix['input_ids'], batch['input_ids'][:, :length]):
        raise ValueError('Assistant prompt prefix changed')
    if batch['input_ids'].shape[1] > 4096:
        raise ValueError('No silent truncation of image or assistant tokens')
    mask = batch['attention_mask'][0].bool().clone()
    mask[:length] = False
    for token in ('<|image_pad|>', '<|vision_start|>', '<|vision_end|>', '<|video_pad|>'):
        token_id = processor.tokenizer.convert_tokens_to_ids(token)
        if token_id is not None:
            mask &= batch['input_ids'][0] != token_id
    positions = mask.nonzero().flatten()
    if len(positions) < 2 or positions[0] < 1:
        raise ValueError('Missing assistant JSON or EOS supervision')
    eos_ids = processor.tokenizer.eos_token_id
    eos_ids = [eos_ids] if isinstance(eos_ids, int) else eos_ids
    if not eos_ids or not any((batch['input_ids'][0, positions] == token_id).any() for token_id in eos_ids):
        raise ValueError('Chat template omitted supervised assistant EOS')
    return batch, positions, batch['input_ids'][0, positions]


def sliced_loss(model, encoded):
    import torch
    from torch.nn import functional as F
    batch, positions, targets = encoded
    batch = {key: value.to(model.device) for key, value in batch.items()}
    positions, targets = positions.to(model.device), targets.to(model.device)
    logits = model(**batch, logits_to_keep=positions - 1).logits[0].float()
    if logits.shape[0] != len(targets):
        raise ValueError('Causal logits slicing changed')
    losses = F.cross_entropy(logits, targets, reduction='none')
    if not torch.isfinite(losses).all():
        raise RuntimeError('Nonfinite supervised loss')
    return losses


def verify_causal_projection(engine, row, admission, monitor):
    """A real GPU check against ordinary full-sequence assistant-token CE."""
    import torch
    from torch.nn import functional as F
    batch, positions, targets = encode_json(engine.processor, row)
    batch = {key: value.to(engine.model.device) for key, value in batch.items()}
    positions, targets = positions.to(engine.model.device), targets.to(engine.model.device)
    engine.model.eval()
    with torch.inference_mode():
        yield_to_photo(admission, monitor)
        full_logits = engine.model(**batch).logits[0].float()
        reference = F.cross_entropy(full_logits[positions - 1], targets)
        del full_logits
        yield_to_photo(admission, monitor)
        sliced = F.cross_entropy(engine.model(**batch, logits_to_keep=positions - 1).logits[0].float(), targets)
    difference = float((reference - sliced).abs())
    if not torch.isfinite(sliced) or difference > .01:
        raise RuntimeError('Sliced/full assistant CE failed GPU equivalence check')
    return dict(assistant_tokens=len(positions), input_tokens=batch['input_ids'].shape[1],
        full_ce=float(reference), sliced_ce=float(sliced), absolute_difference=difference,
        prompt_and_image_tokens_supervised=False, eos_supervised=True)


def backup_state(source, destination):
    def hashes(folder):
        return {str(path.relative_to(folder)): file_hash(path) for path in folder.rglob('*') if path.is_file()}
    expected_hashes = hashes(source)
    if destination.exists():
        if destination.is_symlink() or hashes(destination) != expected_hashes:
            raise ValueError('Existing independent checkpoint copy differs')
        return
    temporary = destination.with_name(destination.name + '.partial')
    if temporary.exists():
        raise FileExistsError('Incomplete backup requires inspection')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, temporary)
    if hashes(temporary) != expected_hashes:
        raise RuntimeError('Checkpoint backup checksum mismatch')
    temporary.rename(destination)


def configure_adapter(engine, config, resume_path=None):
    from peft import LoraConfig, PeftModel, get_peft_model
    if resume_path:
        model = PeftModel.from_pretrained(engine.model, str(resume_path), is_trainable=True, local_files_only=True)
    else:
        names = lora_targets([name for name, _ in engine.model.named_modules()], config['modules'])
        model = get_peft_model(engine.model, LoraConfig(r=config['rank'], lora_alpha=config['alpha'],
            lora_dropout=config['dropout'], target_modules=names, bias='none', task_type='CAUSAL_LM'))
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    model.enable_input_require_grads()
    groups = {'language': [], 'visual': []}
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad:
            continue
        if 'lora_' not in name:
            raise ValueError('Only approved LoRA parameters may train')
        kind = 'visual' if '.visual.' in name else 'language'
        if kind == 'visual' and ('merger' not in name or not config['modules'].endswith('visual_merger')):
            raise ValueError('Unapproved visual backbone gradient')
        groups[kind].append(parameter)
    if not groups['language'] or bool(groups['visual']) != config['modules'].endswith('visual_merger'):
        raise ValueError('Declared optimizer modules do not match actual trainable parameters')
    engine.model = model
    return model, groups


def detach_adapter(engine):
    import torch
    engine.model.gradient_checkpointing_disable()
    engine.model.disable_input_require_grads()
    engine.model = engine.model.unload()
    if hasattr(engine.model, 'peft_config'):
        delattr(engine.model, 'peft_config')
    engine.model.config.use_cache = True
    engine.model.eval()
    torch.cuda.empty_cache()


def yield_to_photo(admission, monitor):
    try:
        admission.wait_idle(check_callback=monitor.check)
    except PhotoAdmissionTimeout as exc:
        raise ServicePause(str(exc)) from exc


def loss_weighting(rows, sample_weight_key=None, class_weight_rows=None):
    """Optional explicit research weighting; ordinary cycle4 calls stay unchanged."""
    if sample_weight_key is not None and (not isinstance(sample_weight_key, str) or not sample_weight_key):
        raise ValueError('A nonempty sample weight key is required')
    values = [row.get(sample_weight_key, 1.) if sample_weight_key else 1. for row in rows]
    if any(type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 1 for value in values):
        raise ValueError('Sample loss weights must be finite in (0, 1]')
    if sample_weight_key is None and class_weight_rows is None:
        return values, None
    reference = rows if class_weight_rows is None else class_weight_rows
    if not reference:
        raise ValueError('Class weight reference cannot be empty')
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return values, dict(sample_weight_key=sample_weight_key,
        row_weights_sha256=digest([dict(id=row['id'], image_sha256=row['image_sha256'], weight=weight)
            for row, weight in zip(rows, values)]),
        class_weight_rows_sha256=digest([dict(id=row['id'], image_sha256=row['image_sha256'], target=row['target'])
            for row in reference]), class_weight_images=len(reference),
        normalization='Divide by accumulation example count, not sum of sample weights; weight both JSON and auxiliary losses')


def positive_auxiliary_boost(config):
    """Optional train-only positive recall ablation; absent means unchanged."""
    boost = config.get('aux_positive_boost', {})
    if (not isinstance(boost, dict) or set(boost) - {'dark_marks', 'flaking'}
            or any(type(value) not in {int, float} or not math.isfinite(value) or not 1 <= value <= 4
                for value in boost.values())):
        raise ValueError('Positive auxiliary boost permits only dark_marks/flaking values in [1, 4]')
    return boost


def auxiliary_multipliers(truth, weights, feature_weights, device, boost=None):
    import torch
    boost = {} if boost is None else boost
    return torch.tensor([feature_weights[key] * weights[key]['true' if truth[key] else 'false'] *
        (boost.get(key, 1.) if truth[key] else 1.) for key in KEYS], device=device)


def validated_focal_gammas(config):
    """Explicit decoder-boolean focal variant; absent preserves original CE."""
    if 'focal_gammas' not in config:
        return None
    gammas = config['focal_gammas']
    if (not isinstance(gammas, dict) or set(gammas) != {'true', 'false'}
            or any(type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 4
                for value in gammas.values())):
        raise ValueError('Focal gammas require exactly true/false finite values in [0, 4]')
    return gammas


def focal_auxiliary_losses(losses, truth, gammas=None):
    """CE_i * (1-exp(-CE_i))**gamma(label_i), retaining full-vocab gradients.

    expm1 computes 1-p_t without probability clipping. At exact zero CE the
    positive-gamma loss and its CE derivative are zero; gamma0 is identity.
    """
    if gammas is None or all(value == 0 for value in gammas.values()):
        return losses
    import torch
    if losses.ndim != 1 or len(losses) != len(KEYS) or not torch.isfinite(losses).all() or (losses < 0).any():
        raise ValueError('Focal auxiliary input must be five finite nonnegative token CE values')
    exponents = torch.tensor([gammas['true' if truth[key] else 'false'] for key in KEYS],
        dtype=losses.dtype, device=losses.device)
    active = exponents > 0
    positive = active & (losses > 0)
    result = losses.clone()
    result[active] = 0.
    result[positive] = losses[positive] * (-torch.expm1(-losses[positive])).pow(exponents[positive])
    return result


def train(engine, rows, config, out, backup_root, lineage, monitor, admission, resume=False, pilot=False,
          *, sample_weight_key=None, class_weight_rows=None):
    import torch
    positive_boost = positive_auxiliary_boost(config)
    focal_gammas = validated_focal_gammas(config)
    sample_weights, weighting = loss_weighting(rows, sample_weight_key, class_weight_rows)
    payload = {'lineage': lineage, 'config': config, 'pilot': pilot, 'runtime': lineage['runtime']}
    if weighting is not None:
        payload['loss_weighting'] = weighting
    fingerprint = protocol_fingerprint(payload)
    resume_path = None
    restored = None
    if out.exists() and any(out.iterdir()):
        if not resume:
            raise FileExistsError('Fresh output required without explicit --resume')
        completed = sorted(path for path in out.glob('step-[0-9][0-9][0-9][0-9][0-9][0-9]')
            if (path / 'files.sha256.json').is_file())
        if not completed:
            raise RuntimeError('Output has no complete, verifiable checkpoint')
        resume_path = completed[-1]
        restored = load_checkpoint(resume_path, fingerprint)
    torch.manual_seed(config['seed'])
    torch.cuda.manual_seed_all(config['seed'])
    rng = random.Random(config['seed'])
    yield_to_photo(admission, monitor)
    pilot_check = verify_causal_projection(engine, rows[0], admission, monitor) if pilot and not restored else None
    model, groups = configure_adapter(engine, config, resume_path)
    optimizer = torch.optim.AdamW([dict(params=values, lr=config['visual_lr'] if kind == 'visual' else config['lr'],
        name=kind) for kind, values in groups.items() if values], weight_decay=config['weight_decay'])
    parameters = groups['language'] + groups['visual']
    total = 1 if pilot else math.ceil(len(rows) / config['accumulation']) * config['epochs']
    warmup = max(1, math.ceil(config['warmup_ratio'] * total))
    schedule = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda n:
        min(1., (n + 1) / warmup) * (.5 + .5 * math.cos(math.pi * max(0, n - warmup) / max(1, total - warmup))))
    state = dict(global_step=0, epoch_index=0, next_offset=0, order=[], history=[], config=config)
    if restored:
        optimizer.load_state_dict(restored['optimizer'])
        schedule.load_state_dict(restored['scheduler'])
        rng.setstate(restored['python_random_state'])
        torch.set_rng_state(restored['torch_rng'])
        torch.cuda.set_rng_state_all(restored['cuda_rng'])
        state = restored['state']
        if state['config'] != config:
            raise ValueError('Checkpoint configuration differs from the declared trial')
    weights = class_weights(rows if class_weight_rows is None else class_weight_rows, True)
    feature_weights = {'skin_visible': 1., 'acne_like_bumps': 1., 'redness': 1., 'dark_marks': 2., 'flaking': 2.}
    torch.cuda.reset_peak_memory_stats()
    model.train()
    checkpoint = resume_path
    started = time.monotonic()
    step_in_progress = False
    boundary_rng = None
    try:
        while state['global_step'] < total:
            monitor.check()
            if not state['order'] or state['next_offset'] >= len(state['order']):
                if state['order']:
                    state['epoch_index'] += 1
                state['order'] = list(range(len(rows)))
                rng.shuffle(state['order'])
                state['next_offset'] = 0
            offset = state['next_offset']
            indices = state['order'][offset:offset + config['accumulation']]
            if pilot:
                indices = indices[:1]
            optimizer.zero_grad(set_to_none=True)
            boundary_rng = (rng.getstate(), torch.get_rng_state(), torch.cuda.get_rng_state_all())
            step_in_progress = True
            json_losses, aux_losses = [], []
            for index in indices:
                row, flip = rows[index], rng.random() < .5
                sample_weight = sample_weights[index]
                yield_to_photo(admission, monitor)
                primary = sliced_loss(model, encode_json(engine.processor, row, flip)).mean()
                yield_to_photo(admission, monitor)
                primary_objective = primary / ((1 + config['aux_weight']) * len(indices))
                if sample_weight != 1.:
                    primary_objective = primary_objective * sample_weight
                primary_objective.backward()
                json_losses.append(float(primary.detach()))
                if config['aux_weight']:
                    yield_to_photo(admission, monitor)
                    auxiliary = sliced_loss(model, encode_aux(engine.processor, row, flip))
                    truth = expected(row)
                    auxiliary = focal_auxiliary_losses(auxiliary, truth, focal_gammas)
                    multipliers = auxiliary_multipliers(truth, weights, feature_weights, model.device, positive_boost)
                    auxiliary = (auxiliary * multipliers).sum() / sum(feature_weights.values())
                    yield_to_photo(admission, monitor)
                    auxiliary_objective = auxiliary * config['aux_weight'] / ((1 + config['aux_weight']) * len(indices))
                    if sample_weight != 1.:
                        auxiliary_objective = auxiliary_objective * sample_weight
                    auxiliary_objective.backward()
                    aux_losses.append(float(auxiliary.detach()))
            yield_to_photo(admission, monitor)
            norm = torch.nn.utils.clip_grad_norm_(parameters, config['gradient_clip'])
            if not torch.isfinite(norm) or float(norm) <= 0:
                raise RuntimeError('Missing or nonfinite adapter gradient')
            group_gradient_norms = {kind: float(sum(parameter.grad.detach().float().square().sum()
                for parameter in values if parameter.grad is not None).sqrt()) for kind, values in groups.items() if values}
            if any(not math.isfinite(value) or value <= 0 for value in group_gradient_norms.values()):
                raise RuntimeError('Declared optimizer group has no finite nonzero gradient')
            optimizer.step()
            schedule.step()
            state['global_step'] += 1
            state['next_offset'] = offset + len(indices)
            entry = dict(candidate=config['name'], epoch=state['epoch_index'] + 1, step=state['global_step'],
                total_steps=total, json_loss=sum(json_losses) / len(json_losses),
                aux_loss=sum(aux_losses) / len(aux_losses) if aux_losses else None, gradient_norm=float(norm),
                group_gradient_norms=group_gradient_norms,
                lr={group['name']: group['lr'] for group in optimizer.param_groups},
                elapsed_seconds=round(time.monotonic() - started, 3))
            state['history'].append(entry)
            step_in_progress = False
            if state['global_step'] == 1 or state['global_step'] % 5 == 0 or state['global_step'] == total:
                print(json.dumps(entry), flush=True)
            write(out.parent / (out.name + '-progress.json'), entry)
            if state['global_step'] % 25 == 0 or state['global_step'] == total or monitor.failed.is_set():
                checkpoint = save_checkpoint(out, model, engine.processor, optimizer, schedule, state, fingerprint, rng)
            monitor.check()
    except ServicePause:
        # Replay an interrupted accumulation chunk from its pre-step RNG state.
        # No parameter update has occurred; partial gradients are not a checkpoint.
        if step_in_progress and boundary_rng is not None:
            optimizer.zero_grad(set_to_none=True)
            rng.setstate(boundary_rng[0])
            torch.set_rng_state(boundary_rng[1])
            torch.cuda.set_rng_state_all(boundary_rng[2])
        if checkpoint is None or int(checkpoint.name.split('-')[1]) != state['global_step']:
            checkpoint = save_checkpoint(out, model, engine.processor, optimizer, schedule, state, fingerprint, rng)
        raise
    summary = dict(config=config, lineage=lineage, fingerprint=fingerprint, steps=state['global_step'],
        training_images=len(rows), checkpoint=str(checkpoint),
        trainable_parameters=sum(value.numel() for value in parameters),
        visual_trainable_parameters=sum(value.numel() for value in groups['visual']),
        peak_gpu_allocated_bytes=torch.cuda.max_memory_allocated(), class_weights=weights,
        auxiliary_feature_weights=feature_weights, elapsed_seconds=round(time.monotonic() - started, 3),
        resumed=restored is not None, pilot_only=pilot, research_only=True, automatic_deployment=False)
    if pilot_check:
        summary['causal_projection_check'] = pilot_check
    if weighting is not None:
        summary['loss_weighting'] = weighting
    if focal_gammas is not None:
        summary['auxiliary_focal_variant'] = dict(gammas=focal_gammas,
            target_probability='exp(-full-vocabulary boolean-token CE)', probability_clipping=False,
            focal_factor_gradient_detached=False, normalization='Original fixed feature weight sum7')
    write(out / 'training-summary.json', summary)
    if not pilot:
        backup_state(checkpoint, backup_root / out.name / checkpoint.name)
    model.gradient_checkpointing_disable()
    model.config.use_cache = True
    model.eval()
    return summary


def analyze_cooperatively(engine, row, monitor, admission):
    """Same website generation, yielding and retrying when a photo request arrives."""
    import torch
    from transformers import StoppingCriteriaList
    image, quality = open_image(row['image'])
    if quality['reasons']:
        raise ValueError('Frozen validation image failed quality checks')
    interruptions = 0
    started = time.monotonic()
    while True:
        yield_to_photo(admission, monitor)
        batch = engine.processor.apply_chat_template(messages_for(image, row.get('note', '')),
            tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors='pt').to(engine.model.device)
        batch.pop('token_type_ids', None)
        criterion = admission.make_stopping_criterion()
        with torch.inference_mode():
            ids = engine.model.generate(**batch, max_new_tokens=400, max_time=60, do_sample=False,
                repetition_penalty=1.05, stopping_criteria=StoppingCriteriaList([criterion]))
        torch.cuda.synchronize()
        if criterion.triggered:
            interruptions += 1
            del ids, batch
            continue  # Partial generations never enter validation metrics.
        text = engine.processor.batch_decode(ids[:, batch['input_ids'].shape[1]:], skip_special_tokens=True)[0]
        try:
            result = envelope(parse(text), quality)
        except (ValueError, TypeError) as exc:
            result = dict(status='invalid_model_output', error=str(exc), requires_user_confirmation=True,
                eligible_for_auto_save=False)
        return dict(**result, raw_output=text, inference_seconds=round(time.monotonic() - started, 3),
            image_sha256=row['image_sha256'], photo_priority_interruptions=interruptions)


def evaluate(engine, rows, path, fingerprint, monitor, admission, resume=False):
    if path.exists():
        if not resume:
            raise FileExistsError('Explicit --resume is required to reuse validation results')
        report = json.loads(path.read_text())
        assert_fingerprint_matches(report['fingerprint'], fingerprint)
        if [item['id'] for item in report['predictions']] != [row['id'] for row in rows]:
            raise ValueError('Validation row order changed')
        return report['metrics']
    predictions = []
    partial_path = path.with_suffix('.partial-predictions.json')
    if partial_path.exists():
        if not resume:
            raise FileExistsError('Partial validation requires explicit --resume')
        partial = json.loads(partial_path.read_text())
        assert_fingerprint_matches(partial['fingerprint'], fingerprint)
        predictions = partial['predictions']
        if len(predictions) > len(rows) or [item['id'] for item in predictions] != [row['id'] for row in rows[:len(predictions)]]:
            raise ValueError('Partial validation row order changed')
        if any(prediction.get('image_sha256') != row['image_sha256'] for row, prediction in zip(rows, predictions)):
            raise ValueError('Partial validation image identity changed')
    engine.model.eval()
    engine.model.config.use_cache = True
    for row in rows[len(predictions):]:
        monitor.check()
        prediction = analyze_cooperatively(engine, row, monitor, admission)
        predictions.append(dict(id=row['id'], **prediction))
        write(partial_path, dict(fingerprint=fingerprint, predictions=predictions))
        write(path.with_suffix('.progress.json'), dict(completed=len(predictions), total=len(rows),
            fingerprint=fingerprint))
        if len(predictions) == 1 or len(predictions) % 10 == 0:
            print('EVAL_PROGRESS', path.name, len(predictions), len(rows), flush=True)
    metrics = score(rows, predictions)
    metrics['macro_f1'] = sum(metrics['by_tag'][tag]['f1'] or 0 for tag in TAGS) / len(TAGS)
    write(path, dict(fingerprint=fingerprint, metrics=metrics, predictions=predictions))
    return metrics


def preflight(args):
    mount = json.loads(subprocess.check_output(['findmnt', '-J', '-T', '/mnt/goodagents'], text=True))['filesystems'][0]
    require_mount(mount)
    for path in (args.data, args.model, args.output, args.report, args.backup_root):
        if not path.resolve().is_relative_to(Path('/mnt/goodagents')):
            raise ValueError('All training assets must stay on the exact persistent mount')
    assert_no_sealed_inputs(args.data / 'train.jsonl', args.data / 'val.jsonl')
    metadata = json.loads((args.data / 'split_info.json').read_text())
    for split in ('train', 'val'):
        if metadata['manifest_sha256'][split] != FROZEN[split]:
            raise ValueError('Unexpected dataset freeze')
    train_rows = load_split(args.data, metadata, 'train')
    validation_rows = load_split(args.data, metadata, 'val')
    if len(train_rows) != 559 or len(validation_rows) != 80:
        raise ValueError('Frozen train/validation count changed')
    if {row['subject_id'] for row in train_rows} & {row['subject_id'] for row in validation_rows}:
        raise ValueError('Train/validation group leakage')
    for rows in (train_rows, validation_rows):
        class_weights(rows, True)
    artifact = json.loads((args.model / 'artifact-metadata.json').read_text())
    if artifact.get('repo_id') != REPO or artifact.get('revision') != REVISION or not artifact.get('verified'):
        raise ValueError('Missing verified pinned official model')
    for name, digest in WEIGHTS.items():
        if file_hash(args.model / name) != digest:
            raise ValueError('Official model shard checksum mismatch')
    import torch
    if version('torch') != '2.13.0+rocm7.1' or not torch.version.hip or 'MI300X' not in torch.cuda.get_device_name(0):
        raise RuntimeError('Fresh approved MI300X training environment required')
    torch.set_num_threads(4)
    torch.cuda.set_per_process_memory_fraction(.65)
    files = sorted(path for path in ROOT.rglob('*.py') if '__pycache__' not in path.parts)
    protocol = dict(model={'repo_id': REPO, 'revision': REVISION, 'weights': WEIGHTS},
        data_manifest_sha256=FROZEN, train_images=559, validation_images=80,
        source_sha256={str(path.relative_to(ROOT)): file_hash(path) for path in files},
        runtime={name: version(name) for name in ('torch', 'torchvision', 'transformers', 'peft', 'accelerate', 'pillow', 'numpy', 'safetensors')},
        gpu=torch.cuda.get_device_name(0), hip=torch.version.hip, attention='eager', patch_projection='equivalent linear',
        grid=CONFIGS, replication_seeds=SEEDS, augmentation='Train-only same horizontal mirror for both objectives; no colour changes',
        objective='(full assistant JSON CE + lambda * balanced five-boolean auxiliary CE) / (1 + lambda)',
        aux_weights='Train-only class weights clipped .5..2; dark_marks/flaking feature weight2, others1; divide weighted CE sum by fixed feature weight sum7',
        generation=dict(max_new_tokens=400, max_time=60, do_sample=False, repetition_penalty=1.05),
        shared_gpu_policy='Production photo priority: health polling150ms, no forward/backward while photo busy; research generation interrupts at next token and retries the same validation row; no hard GPU compute isolation guarantee',
        checkpoints='Every25 optimizer steps and service pause: safetensors + hashed weights_only optimizer/scheduler/RNG state',
        validation_selection='Eligible gates first, then macro F1 and fewer parameters; repeat diagnostic best if none pass',
        mount=mount, test_evaluated=False, automatic_deployment=False, clinical_validation=False,
        phone_photo_evaluation=False, pending_or_AI_gold_used=False)
    return protocol, train_rows, validation_rows


def initialize_report(report, fingerprint, resume):
    entries = {path.name for path in report.iterdir()} if report.exists() else set()
    if not entries:
        return
    if resume:
        assert_fingerprint_matches(json.loads((report / 'protocol.json').read_text())['fingerprint'], fingerprint)
        return
    # The detached launcher opens its log and records our PID before preflight.
    # Those two bootstrap files are not prior scientific results.
    if entries <= {'worker.log', 'job.json'} and 'job.json' in entries:
        job = json.loads((report / 'job.json').read_text())
        if job.get('pid') == os.getpid() and job.get('trainer_sha256') == file_hash(Path(__file__)):
            return
    raise FileExistsError('Fresh report required without --resume')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'model', 'output', 'report', 'backup-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--pilot-only', action='store_true')
    args = parser.parse_args()
    protocol, rows, validation = preflight(args)
    fingerprint = protocol_fingerprint(protocol)
    initialize_report(args.report, fingerprint, args.resume)
    if not (args.report / 'protocol.json').exists() and args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError('Existing outputs without frozen report must not be reused')
    args.report.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    write(args.report / 'protocol.json', dict(protocol, fingerprint=fingerprint))
    write(args.report / 'status.json', dict(status='loading_base', fingerprint=fingerprint, test_evaluated=False))
    monitor = HealthMonitor(args.report / 'service-concurrency.jsonl')
    if not monitor.probe()['ok']:
        raise RuntimeError('Production probe failed before training')
    monitor.thread.start()
    admission = PhotoAdmission(args.report / 'photo-priority-events.jsonl')
    admission.start()
    lineage = dict(protocol_fingerprint=fingerprint, base_model=REPO, revision=REVISION,
        manifest_sha256=FROZEN, runtime=protocol['runtime'])
    try:
        yield_to_photo(admission, monitor)
        engine = SkinVLM(str(args.model))
        pilot_config = dict(CONFIGS[-1], seed=SEEDS[0], name='pilot-json-aux-visual-r16')
        if not (args.report / 'pilot.json').exists():
            write(args.report / 'status.json', dict(status='gpu_training_pilot', fingerprint=fingerprint, test_evaluated=False))
            pilot = train(engine, rows, pilot_config, args.output / pilot_config['name'], args.backup_root,
                lineage, monitor, admission, args.resume, pilot=True)
            write(args.report / 'pilot.json', pilot)
            detach_adapter(engine)
        elif not args.resume:
            raise FileExistsError('Pilot result exists')
        if args.pilot_only:
            write(args.report / 'status.json', dict(status='pilot_complete', fingerprint=fingerprint, test_evaluated=False))
            return
        write(args.report / 'status.json', dict(status='baseline_validation', fingerprint=fingerprint, test_evaluated=False))
        baseline = evaluate(engine, validation, args.report / 'base-val.json', fingerprint, monitor, admission, args.resume)
        results = []

        def trial(config, round_):
            write(args.report / 'status.json', dict(status='training', candidate=config['name'],
                completed=len(results), total=6, fingerprint=fingerprint, test_evaluated=False))
            summary = train(engine, rows, config, args.output / config['name'], args.backup_root,
                lineage, monitor, admission, args.resume)
            write(args.report / 'status.json', dict(status='candidate_validation', candidate=config['name'],
                completed=len(results), total=6, fingerprint=fingerprint, test_evaluated=False))
            metrics = evaluate(engine, validation, args.report / (config['name'] + '-val.json'),
                fingerprint, monitor, admission, args.resume)
            result = dict(name=config['name'], config=config, seed=config['seed'], validation=metrics,
                gate=gate(metrics), parameters=summary['trainable_parameters'], adapter=summary['checkpoint'], round=round_)
            results.append(result)
            write(args.report / 'progress.json', dict(completed=len(results), total=6, results=results))
            detach_adapter(engine)
            return result

        first_round = [trial(dict(config, seed=SEEDS[0]), 1) for config in CONFIGS]
        choice = choose_recipe(first_round)
        name = (choice['selected'] or choice['diagnostic'])['name']
        first = next(item for item in first_round if item['name'] == name)
        repeated = [first]
        for seed in SEEDS[1:]:
            config = dict(first['config'], seed=seed, name=first['name'] + '-seed' + str(seed))
            repeated.append(trial(config, 2))
        stability = stability_report(baseline, repeated)
        selected = first['adapter'] if choice['selected'] and stability['eligible'] else None
        write(args.report / 'selection.json', dict(status='six_trials_complete', fingerprint=fingerprint,
            baseline_validation=baseline, candidates=results, choice=choice, stability=stability,
            selected_research_adapter=selected, test_evaluated=False, automatic_deployment=False,
            phone_photo_evaluation=False, clinical_validation=False))
        write(args.report / 'status.json', dict(status='complete', completed=6, total=6, test_evaluated=False))
    except ServicePause as exc:
        write(args.report / 'status.json', dict(status='suspended_for_service_health', error=str(exc),
            fingerprint=fingerprint, test_evaluated=False))
        print('TRAINING_SUSPENDED_FOR_SERVICE_HEALTH', flush=True)
    except Exception as exc:
        write(args.report / 'status.json', dict(status='failed', error_type=type(exc).__name__,
            error=str(exc), fingerprint=fingerprint, test_evaluated=False))
        raise
    finally:
        monitor.stop.set()
        admission.close()


if __name__ == '__main__':
    main()

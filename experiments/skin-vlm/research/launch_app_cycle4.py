"""Start one persistent research job; never signal or modify serving processes."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from install_training_from_cache import require_mount
from download_model import file_hash


def refuse_invalidated_resume(report, resume):
    """A quality stop invalidates the experiment, not just the current process."""
    if resume and any(path.exists() or path.is_symlink() for path in (
            Path(report) / 'data-quality-stop.json', Path(report) / 'DO_NOT_RESUME')):
        raise ValueError('Data-quality-stopped research cannot resume; freeze reviewed data in a fresh run')


def trainer_command(args):
    """Reuse the same restricted launcher for the independent silver experiment."""
    silver = args.trainer == 'silver-comparison'
    historical = args.trainer in {'silver-comparison', 'gold-recall-ablation', 'gold-focal-comparison'}
    names = ('silver_train', 'silver_sha256', 'silver_audit', 'silver_audit_sha256')
    if silver and any(getattr(args, name) is None for name in names):
        raise ValueError('Silver training requires its manifest/audit hashes and historical control directory')
    if historical and args.historical_report is None:
        raise ValueError('The ablation requires a historical control directory')
    if not silver and (any(getattr(args, name) is not None for name in names) or args.replicate):
        raise ValueError('Silver arguments cannot change the original cycle4 experiment')
    if not historical and args.historical_report is not None:
        raise ValueError('Historical control arguments do not belong to the original cycle4 experiment')
    if args.pilot_only and args.replicate:
        raise ValueError('Pilot-only and replication cannot be combined')
    trainer = ROOT / {'app-cycle4': 'research/train_app_cycle4.py',
        'silver-comparison': 'research/train_silver_comparison.py',
        'gold-recall-ablation': 'research/train_gold_recall_ablation.py',
        'gold-focal-comparison': 'research/train_gold_focal_comparison.py'}[args.trainer]
    command = [str(args.python), '-u', str(trainer)]
    for name in ('data', 'model', 'output', 'report', 'backup_root'):
        command += ['--' + name.replace('_', '-'), str(getattr(args, name).resolve())]
    if silver:
        for name in names:
            value = getattr(args, name)
            command += ['--' + name.replace('_', '-'), str(value.resolve()) if isinstance(value, Path) else value]
    if historical:
        command += ['--historical-report', str(args.historical_report.resolve())]
    for name in ('resume', 'pilot_only', 'replicate'):
        if getattr(args, name):
            command.append('--' + name.replace('_', '-'))
    return trainer, command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('python', 'data', 'model', 'output', 'report', 'backup-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--pilot-only', action='store_true')
    parser.add_argument('--trainer', choices=('app-cycle4', 'silver-comparison', 'gold-recall-ablation', 'gold-focal-comparison'), default='app-cycle4')
    for name in ('silver-train', 'silver-audit', 'historical-report'):
        parser.add_argument('--' + name, type=Path)
    for name in ('silver-sha256', 'silver-audit-sha256'):
        parser.add_argument('--' + name)
    parser.add_argument('--replicate', action='store_true')
    args = parser.parse_args()
    refuse_invalidated_resume(args.report, args.resume)
    trainer, command = trainer_command(args)
    mount = json.loads(subprocess.check_output(['findmnt', '-J', '-T', '/mnt/goodagents'], text=True))['filesystems'][0]
    require_mount(mount)
    paths = [ROOT, args.python, args.data, args.model, args.output, args.report, args.backup_root]
    if args.trainer == 'silver-comparison':
        paths += [args.silver_train, args.silver_audit]
    if args.trainer in {'silver-comparison', 'gold-recall-ablation', 'gold-focal-comparison'}:
        paths += [args.historical_report]
    for path in paths:
        if not path.resolve().is_relative_to(Path('/mnt/goodagents')):
            raise ValueError('Research job assets must remain on persistent storage')
    ready = args.python.parent.parent / 'training-runtime-ready.json'
    if not json.loads(ready.read_text()).get('offline_cache_reuse_only'):
        raise RuntimeError('Fresh training runtime gate not complete')
    if args.report.exists() and any(args.report.iterdir()) and not args.resume:
        raise FileExistsError('Existing report requires explicit --resume')
    args.report.mkdir(parents=True, exist_ok=True)
    if args.resume and (args.report / 'job.json').exists():
        previous = json.loads((args.report / 'job.json').read_text())
        stat = Path('/proc') / str(previous['pid']) / 'stat'
        if stat.exists() and stat.read_text().split(') ')[-1].split()[19] == previous['start_ticks']:
            raise RuntimeError('The existing research job is still running')
    runtime = args.python.parent.parent
    kernel_cache = Path('/mnt/goodagents/blink-runtime/data/kernel-cache/qwen-app-cycle4-t213-r71')
    if not kernel_cache.resolve().is_relative_to(Path('/mnt/goodagents')):
        raise ValueError('Kernel cache escapes persistent storage')
    kernel_cache.mkdir(parents=True, exist_ok=True)
    env = {'PATH': str(runtime / 'bin') + ':/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1',
        'PYTHONNOUSERSITE': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'HF_HOME': '/mnt/goodagents/blink-runtime/data/models/hf-cache',
        'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '4', 'MKL_NUM_THREADS': '4',
        'OPENBLAS_NUM_THREADS': '4', 'MALLOC_ARENA_MAX': '4',
        'PYTORCH_KERNEL_CACHE_PATH': str(kernel_cache),
        'LD_LIBRARY_PATH': str(runtime / 'lib/python3.11/site-packages/torch/lib') + ':/opt/rocm/lib:/opt/rocm/lib64'}
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with (args.report / 'worker.log').open('ab') as log:
        process = subprocess.Popen(['/usr/bin/nice', '-n', '10', *command], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    stat = Path('/proc') / str(process.pid) / 'stat'
    record = dict(pid=process.pid, start_ticks=stat.read_text().split(') ')[-1].split()[19],
        started_at=time.time(), command=command, source=str(ROOT),
        trainer_sha256=file_hash(trainer),
        runtime_ready_sha256=file_hash(ready), research_only=True, serving_modified=False)
    temporary = args.report / 'job.json.partial'
    temporary.write_text(json.dumps(record, indent=2))
    temporary.replace(args.report / 'job.json')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()

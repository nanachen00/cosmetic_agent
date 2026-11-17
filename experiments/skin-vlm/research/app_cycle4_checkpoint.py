"""Atomic local training checkpoints; model loading remains the caller's job.

Only locally generated tensor/basic-container state is saved to state.pt and
loaded with weights_only=True, after every checkpoint file passes SHA-256.
Callers must supply canonical paths (resolve trusted NFS aliases first).
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import sys

MANIFEST = "files.sha256.json"
RESERVED = {MANIFEST, "metadata.json", "state.json", "python-random-state.json", "state.pt"}


def _path(value):
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Checkpoint path traversal is forbidden")
    path = path.absolute()
    if any(item.is_symlink() for item in [path, *path.parents]):
        raise ValueError("Checkpoint paths cannot contain symlinks; resolve trusted aliases before calling")
    return path


def _sha(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _verified_bytes(path, expected):
    if path.is_symlink():
        raise ValueError("Symlinks are forbidden inside a checkpoint")
    value = path.read_bytes()
    if hashlib.sha256(value).hexdigest() != expected:
        raise ValueError("Checkpoint changed after SHA-256 verification")
    return value


def _json(path, value):
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    with path.open("x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def _cpu_basic(value, torch):
    if isinstance(value, torch.Tensor):
        return value.detach().cpu().clone()
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, dict):
        if any(type(key) not in {str, int} for key in value):
            raise TypeError("Optimizer/scheduler state keys must be strings or integers")
        return {key: _cpu_basic(item, torch) for key, item in value.items()}
    if type(value) in {tuple, list}:
        return type(value)(_cpu_basic(item, torch) for item in value)
    raise TypeError("Unsafe/custom optimizer or scheduler state is forbidden: " + type(value).__name__)


def _files(folder):
    result = {}
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinks are forbidden inside a checkpoint")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Only regular checkpoint files are supported")
        name = path.relative_to(folder).as_posix()
        if path.suffix.lower() in {".bin", ".pkl", ".pickle", ".pt", ".pth"} and name != "state.pt":
            raise ValueError("Unsafe model/processor serialization is forbidden")
        if name != MANIFEST:
            result[name] = _sha(path)
    return result


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _native_rename_without_replacement(source, target):
    """Use an atomic native directory no-replace operation where supported."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(target), 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # AT_FDCWD / RENAME_NOREPLACE
    else:
        raise RuntimeError("Atomic no-replace directory rename is required on this platform")
    if result:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise FileExistsError(number, "Checkpoint already exists", str(target))
        raise OSError(number, "Atomic checkpoint publication failed", str(target))


def _publish_reserved(source, target):
    """NFS fallback: exclusive directory reservation, manifest-last commit.

    Some NFS servers reject RENAME_NOREPLACE. An exclusive mkdir prevents all
    cooperating checkpoint writers from sharing or replacing this target. The
    target is not loadable until its complete SHA manifest is atomically moved
    last. Interrupted publication preserves both locations for inspection.
    """
    expected = json.loads((source / MANIFEST).read_text())["files"]
    target.mkdir(mode=0o700, exist_ok=False)
    for path in source.iterdir():
        if path.name == MANIFEST:
            continue
        destination = target / path.name
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Reserved checkpoint destination unexpectedly occupied")
        os.rename(path, destination)
    if _files(target) != expected:
        raise ValueError("Reserved checkpoint differs before manifest commit")
    _sync_directory(target)
    if (target / MANIFEST).exists() or (target / MANIFEST).is_symlink():
        raise FileExistsError("Checkpoint commit manifest already exists")
    os.rename(source / MANIFEST, target / MANIFEST)
    _sync_directory(target)
    source.rmdir()


def _rename_without_replacement(source, target):
    try:
        _native_rename_without_replacement(source, target)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            raise
        _publish_reserved(source, target)


def save_checkpoint(root, model, processor, optimizer, scheduler, state, fingerprint, rng) -> Path:
    import torch

    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("A non-empty protocol fingerprint is required")
    if not isinstance(state, dict) or type(state.get("global_step")) is not int or state["global_step"] < 0:
        raise ValueError("state.global_step must be a nonnegative integer")
    # Validate all JSON and locally generated state before creating a partial.
    json.dumps(state, allow_nan=False)
    if not isinstance(rng, random.Random):
        raise TypeError("rng must be the caller's random.Random instance")
    python_state = rng.getstate()
    random.Random().setstate(python_state)
    payload = _cpu_basic({"optimizer": optimizer.state_dict(), "scheduler": scheduler.state_dict(),
                         "torch_rng": torch.get_rng_state(),
                         "cuda_rng": torch.cuda.get_rng_state_all() if torch.cuda.is_initialized() else []}, torch)
    root = _path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"step-{state['global_step']:06d}"
    target, partial = root / name, root / (name + ".partial")
    if target.exists() or target.is_symlink() or partial.exists() or partial.is_symlink():
        raise FileExistsError("Existing checkpoint/partial is never overwritten")
    partial.mkdir(mode=0o700)  # Exclusive creation also serializes same-step writers.
    # Failures retain this partial for inspection; loaders reject partial paths.
    model.save_pretrained(str(partial), safe_serialization=True)
    processor.save_pretrained(str(partial), safe_serialization=True)
    if any((partial / item).exists() or (partial / item).is_symlink() for item in RESERVED):
        raise ValueError("Model/processor attempted to overwrite reserved checkpoint state")
    if not any(partial.rglob("*.safetensors")):
        raise ValueError("The adapter must be serialized as safetensors")
    _files(partial)  # Reject symlink/pickle artifacts before writing optimizer state.
    torch.save(payload, partial / "state.pt")
    _json(partial / "state.json", state)
    _json(partial / "python-random-state.json", python_state)
    _json(partial / "metadata.json", {"schema_version": 1, "fingerprint": fingerprint,
          "global_step": state["global_step"], "state_format": "locally_generated_weights_only_v1",
          "publication_policy": "native_no_replace_else_exclusive_directory_manifest_last"})
    _json(partial / MANIFEST, {"schema_version": 1, "files": _files(partial)})
    for path in partial.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    _sync_directory(partial)
    _rename_without_replacement(partial, target)
    _sync_directory(root)
    return target


def load_checkpoint(path, fingerprint) -> dict:
    import torch

    folder = _path(path)
    if not re.fullmatch(r"step-[0-9]{6,}", folder.name):
        raise ValueError("A completed step checkpoint is required; partial paths are not resumable")
    if not folder.is_dir():
        raise FileNotFoundError("Checkpoint directory is missing")
    manifest_path = folder / MANIFEST
    if not manifest_path.is_file():
        raise ValueError("Checkpoint manifest missing: publication is incomplete")
    if manifest_path.is_symlink():
        raise ValueError("Symlink manifest is forbidden")
    manifest = json.loads(manifest_path.read_text())
    expected = manifest.get("files")
    if manifest.get("schema_version") != 1 or not isinstance(expected, dict):
        raise ValueError("Invalid checkpoint hash manifest")
    for name, digest in expected.items():
        member = Path(name)
        if member.is_absolute() or ".." in member.parts or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Unsafe checkpoint manifest member")
    if _files(folder) != expected:
        raise ValueError("Checkpoint SHA-256 mismatch, missing file or unrecorded file")
    if not (RESERVED - {MANIFEST}).issubset(expected) or not any(name.endswith(".safetensors") for name in expected):
        raise ValueError("Incomplete checkpoint")
    metadata = json.loads(_verified_bytes(folder / "metadata.json", expected["metadata.json"]))
    state = json.loads(_verified_bytes(folder / "state.json", expected["state.json"]))
    if metadata.get("schema_version") != 1 or metadata.get("state_format") != "locally_generated_weights_only_v1" or metadata.get("fingerprint") != fingerprint:
        raise ValueError("Checkpoint protocol fingerprint/schema mismatch")
    if type(state.get("global_step")) is not int or state["global_step"] < 0 or metadata.get("global_step") != state["global_step"] or folder.name != f"step-{state['global_step']:06d}":
        raise ValueError("Checkpoint step metadata mismatch")
    def tuples(value):
        return tuple(tuples(item) for item in value) if isinstance(value, list) else value
    python_state = tuples(json.loads(_verified_bytes(folder / "python-random-state.json", expected["python-random-state.json"])))
    random.Random().setstate(python_state)
    # Never relax weights_only, even for a local-looking file or failed load.
    payload = torch.load(io.BytesIO(_verified_bytes(folder / "state.pt", expected["state.pt"])), weights_only=True, map_location="cpu")
    if not isinstance(payload, dict) or set(payload) != {"optimizer", "scheduler", "torch_rng", "cuda_rng"}:
        raise ValueError("Unexpected checkpoint tensor payload")
    payload = _cpu_basic(payload, torch)
    if not isinstance(payload["optimizer"], dict) or not isinstance(payload["scheduler"], dict):
        raise ValueError("Invalid optimizer/scheduler state")
    if type(payload["cuda_rng"]) is not list:
        raise ValueError("CUDA RNG state must be a list of CPU tensors")
    for tensor in [payload["torch_rng"], *payload["cuda_rng"]]:
        if not isinstance(tensor, torch.Tensor) or tensor.device.type != "cpu" or tensor.dtype != torch.uint8 or tensor.ndim != 1:
            raise ValueError("Invalid CPU/CUDA RNG tensor")
    return dict(payload, python_random_state=python_state, state=state)

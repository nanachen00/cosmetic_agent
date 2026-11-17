"""Build a fresh MI300X training venv entirely from approved, cached wheels.

This does not change a serving environment, download models, or start training.
The upstream URL lock is used only as a trust anchor; pip receives local paths.
"""
from __future__ import annotations

import argparse
from email import message_from_bytes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import zipfile

ROOT = Path("/mnt/goodagents")
EXPECTED_NFS = "192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11"
APPROVED_PURE_SHA = "8716d3d3802fa8a2b938bd0d7add4336a6211554055ab60351c104da0401cf1e"
APPROVED_NATIVE_SHA = "677ab1bcbf0e7c8150ed7912f7af0e07a6460e6735d42589c7a87b1999937ee9"
NATIVE_VERSIONS = {"torch": "2.13.0+rocm7.1", "torchvision": "0.28.0+rocm7.1", "triton-rocm": "3.7.1"}
HERE = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def require_mount(record: dict, root: Path = ROOT) -> None:
    if (record.get("target") != str(root) or record.get("source") != EXPECTED_NFS
            or record.get("fstype") not in {"nfs", "nfs4"}
            or "rw" not in record.get("options", "").split(",")):
        raise RuntimeError("Expected exact writable school NFS mount is required")


def require_fresh_target(target: Path, root: Path = ROOT) -> Path:
    if target.is_symlink():
        raise ValueError("Training target must not be a symlink")
    target = target.resolve()
    if target.parent != root / "blink-runtime/envs":
        raise ValueError("Choose a fresh venv directly in persistent blink-runtime/envs")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FileExistsError("Refuse to modify any nonempty/existing runtime")
    return target


def lock_lines(path: Path, expected_sha: str) -> list[str]:
    if sha(path) != expected_sha:
        raise ValueError("Approved dependency lock changed")
    return [line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def read_approved(pure: Path, native: Path) -> tuple[dict, dict]:
    pure_pins = {}
    for line in lock_lines(pure, APPROVED_PURE_SHA):
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+) --hash=sha256:([a-f0-9]{64})", line)
        if not match:
            raise ValueError("Only exact hash-locked package versions are permitted")
        name, version, digest = match.groups()
        key = normalized(name)
        if key in pure_pins:
            raise ValueError("Duplicate pure package lock entry")
        pure_pins[key] = {"version": version, "sha256": digest}
    native_pins = {}
    for line in lock_lines(native, APPROVED_NATIVE_SHA):
        url, digest = line.split(" --hash=sha256:")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"download.pytorch.org", "download-r2.pytorch.org"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("Invalid approved native lock entry")
        filename = Path(unquote(parsed.path)).name
        if filename in native_pins:
            raise ValueError("Duplicate native wheel lock entry")
        native_pins[filename] = digest
    if len(pure_pins) != 42 or len(native_pins) != 3 or set(pure_pins) & set(NATIVE_VERSIONS):
        raise ValueError("Expected exactly42 pure packages and3 separately locked native wheels")
    return pure_pins, native_pins


def wheel_metadata(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        # pip itself includes vendored dependency metadata under pip/_vendor;
        # only the wheel's single top-level dist-info identifies its package.
        names = [name for name in archive.namelist()
                 if name.endswith(".dist-info/METADATA") and name.count("/") == 1]
        if len(names) != 1:
            raise ValueError("Invalid wheel metadata")
        metadata = message_from_bytes(archive.read(names[0]))
    return normalized(metadata["Name"]), metadata["Version"]


def verify_pure_cache(cache: Path, pins: dict, root: Path = ROOT) -> list[dict]:
    if not cache.resolve().is_relative_to(root):
        raise ValueError("Wheel cache must stay on the persistent mount")
    found = {}
    records = []
    for wheel in sorted(cache.glob("*.whl")):
        if not wheel.resolve().is_relative_to(root):
            raise ValueError("Cached wheel escapes persistent storage")
        name, version = wheel_metadata(wheel)
        expected = pins.get(name)
        if name in found or not expected or version != expected["version"] or sha(wheel) != expected["sha256"]:
            raise ValueError("Cached pure wheel differs from the approved hash/version")
        found[name] = version
        records.append({"name": name, "version": version, "path": str(wheel), "sha256": expected["sha256"]})
    if set(found) != set(pins):
        raise ValueError("Approved pure wheel cache is incomplete")
    return records


def verify_native_cache(local_lock: Path, cache: Path, pins: dict, root: Path = ROOT) -> list[dict]:
    if not local_lock.resolve().is_relative_to(root) or not cache.resolve().is_relative_to(root):
        raise ValueError("Native cache/lock must remain persistent")
    found = set()
    records = []
    for line in local_lock.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"(/[^\s]+\.whl) --hash=sha256:([a-f0-9]{64})", line)
        if not match:
            raise ValueError("Native pip lock may contain only local absolute wheel paths")
        wheel = Path(match[1]);digest = match[2]
        if wheel.resolve().parent != cache.resolve() or not wheel.resolve().is_relative_to(root):
            raise ValueError("Native wheel escapes the approved local cache")
        if wheel.name in found or pins.get(wheel.name) != digest or sha(wheel) != digest:
            raise ValueError("Native cached wheel differs from approved upstream lock")
        name, version = wheel_metadata(wheel)
        if NATIVE_VERSIONS.get(name) != version:
            raise ValueError("Native package name/version differs from secure profile")
        found.add(wheel.name)
        records.append({"name": name, "version": version, "path": str(wheel), "sha256": digest})
    if found != set(pins):
        raise ValueError("Native cache lock does not contain exactly the approved wheels")
    return records


def clean_environment() -> dict[str, str]:
    # Do not inherit model credentials, pip indexes/configuration, or old Torch paths.
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1",
            "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4"}


def install_commands(py: Path, target: Path, local_native: Path, pure_lock: Path, pure_cache: Path) -> list[list[str]]:
    base = [str(py), "-m", "pip", "install", "--disable-pip-version-check", "--no-index", "--no-deps", "--require-hashes"]
    return [base + ["-r", str(local_native), "--report", str(target / "native-install-report.json")],
            base + ["--find-links", str(pure_cache), "-r", str(pure_lock), "--report", str(target / "python-install-report.json")]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--native-cache", type=Path, required=True)
    parser.add_argument("--native-local-lock", type=Path, required=True)
    parser.add_argument("--pure-cache", type=Path, required=True)
    args = parser.parse_args()
    mount = json.loads(subprocess.check_output(["findmnt", "-J", "-T", str(ROOT)], text=True))["filesystems"][0]
    require_mount(mount)
    if sys.platform != "linux" or platform.machine() != "x86_64" or sys.version_info[:2] != (3, 11):
        raise RuntimeError("Use persistent Linux x86_64 Python3.11")
    if not all(path.resolve().is_relative_to(ROOT) for path in (Path(sys.executable), Path(sys.base_prefix), HERE)):
        raise RuntimeError("Installer, base Python and standard library must be persistent")
    target = require_fresh_target(args.venv)
    pure_lock = HERE / "requirements-photo-linux-py311.lock"
    native_lock = HERE / "requirements-photo-native-rocm71-py311.lock"
    pins, native_pins = read_approved(pure_lock, native_lock)
    pure_records = verify_pure_cache(args.pure_cache, pins)
    native_records = verify_native_cache(args.native_local_lock, args.native_cache, native_pins)
    print("ALL45_CACHED_WHEELS_VERIFIED", flush=True)
    child_env = clean_environment()
    subprocess.run([sys.executable, "-m", "venv", str(target)], check=True, env=child_env)
    py = target / "bin/python"
    # Snapshot validated local entries so pip cannot follow a later edit that
    # introduces a URL in the original shared cache lock.
    snapshot_native = target / "approved-native-local.lock"
    snapshot_native.write_text("\n".join(record["path"] + " --hash=sha256:" + record["sha256"] for record in native_records) + "\n")
    snapshot_pure = target / "approved-python.lock"
    pure_bytes = pure_lock.read_bytes()
    if hashlib.sha256(pure_bytes).hexdigest() != APPROVED_PURE_SHA:
        raise ValueError("Approved pure lock changed before installation")
    snapshot_pure.write_bytes(pure_bytes)
    commands = install_commands(py, target, snapshot_native, snapshot_pure, args.pure_cache)
    for command in commands:
        subprocess.run(command, check=True, env=child_env)
    subprocess.run([str(py), "-m", "pip", "check"], check=True, env=child_env)
    installed = json.loads(subprocess.check_output([str(py), "-c", "import importlib.metadata as m,json;print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()}))"], text=True, env=child_env))
    wanted = {name: record["version"] for name, record in pins.items()} | NATIVE_VERSIONS
    if {normalized(name): version for name, version in installed.items()} != wanted:
        raise RuntimeError("Fresh training venv does not contain exactly the approved45 packages")
    site = subprocess.check_output([str(py), "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"], text=True, env=child_env).strip()
    gpu_env = child_env | {"LD_LIBRARY_PATH": str(Path(site) / "torch/lib") + ":/opt/rocm/lib:/opt/rocm/lib64"}
    gpu = subprocess.check_output([str(py), "-c", "import torch,json;assert torch.__version__=='2.13.0+rocm7.1';assert torch.version.hip.startswith('7.1.');assert torch.cuda.is_available();assert 'MI300X' in torch.cuda.get_device_name(0);a=torch.randn(128,128,device='cuda');b=a@a.T;assert torch.isfinite(b).all();torch.cuda.synchronize();print(json.dumps({'torch':torch.__version__,'hip':torch.version.hip,'gpu':torch.cuda.get_device_name(0),'matmul128_finite':True}))"], text=True, env=gpu_env, timeout=180)
    metadata = {"profile": "secure-rocm71-training", "mount": mount, "base_python": sys.executable,
                "base_prefix": sys.base_prefix, "native_lock_sha256": APPROVED_NATIVE_SHA, "python_lock_sha256": APPROVED_PURE_SHA,
                "installer_sha256": sha(Path(__file__)), "cached_wheels": native_records + pure_records,
                "installed_packages": installed, "install_argv": commands, "gpu_check": json.loads(gpu),
                "offline_cache_reuse_only": True, "models_downloaded": False, "training_started": False,
                "native_hash_policy": "Torch official index lacked an upstream SHA; the approved Torch hash is the prior recorded HTTPS-download measurement. Torchvision and Triton hashes were supplied by the official index.",
                "serving_environments_modified": False, "scope": "Fresh dependency environment and small GPU matmul; Qwen forward/backward is a separate gate."}
    (target / "training-runtime-ready.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print("FRESH_OFFLINE_TRAINING_ENV_READY", target, flush=True)


if __name__ == "__main__":
    main()

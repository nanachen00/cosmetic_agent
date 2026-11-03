#!/usr/bin/env python3
"""
BLINK Python / ROCm runtime audit.

Read-only audit of:
1) the Python interpreter actually used by each listening BLINK service; and
2) other discovered Conda / project Python environments, to find stray
   NVIDIA CUDA PyTorch builds such as torch+cu128.

The script does not install/remove packages, restart/stop services, load model
weights, run inference, access application databases, or modify source files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import Any


DEFAULT_ROOT = Path("/var/lib/jenkins/tomod")
DEFAULT_PORTS = (8000, 8001, 8002, 8003, 8004, 8005, 8010, 8011, 8100, 8200)

# 8004 / 8005 are retired in the current backend lifecycle.
SERVICE_POLICY: dict[int, dict[str, str]] = {
    8000: {"role": "TAIDE main", "gpu_policy": "required"},
    8001: {"role": "TAIDE router", "gpu_policy": "not_required"},
    8002: {"role": "BGE-M3 embedding", "gpu_policy": "required"},
    8003: {"role": "LiteLLM gateway", "gpu_policy": "not_required"},
    8004: {"role": "Retired service", "gpu_policy": "retired"},
    8005: {"role": "Retired service", "gpu_policy": "retired"},
    8010: {"role": "Main backend API", "gpu_policy": "not_required"},
    8011: {"role": "Auth / secondary API", "gpu_policy": "not_required"},
    8100: {"role": "SGLang candidate", "gpu_policy": "required_if_listening"},
    8200: {"role": "vLLM candidate", "gpu_policy": "required_if_listening"},
}

# Values are safe to print. Full process environments are never written to disk.
SAFE_ENV_KEYS = (
    "ROCM_PATH",
    "ROCR_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "CUDA_VISIBLE_DEVICES",
    "HSA_OVERRIDE_GFX_VERSION",
)

# Needed to reproduce the service interpreter's library/runtime resolution.
# These values are passed to the probe subprocess but are never logged.
PROBE_ENV_KEYS = (
    "PATH",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "CPATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "ROCM_PATH",
    "ROCR_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "CUDA_VISIBLE_DEVICES",
    "HSA_OVERRIDE_GFX_VERSION",
    "HOME",
    "TMPDIR",
)

PROBE_CODE = r'''import json
import platform
import sys
from importlib import metadata

out = {
    "python": sys.executable,
    "python_version": platform.python_version(),
    "torch_metadata_version": None,
    "torch_importable": False,
    "torch_version": None,
    "torch_hip": None,
    "torch_cuda": None,
    "torch_cuda_is_available": None,
    "device_count": None,
    "device_names": [],
    "import_error_type": None,
    "import_error": None,
}

try:
    out["torch_metadata_version"] = metadata.version("torch")
except Exception:
    pass

try:
    import torch
    out["torch_importable"] = True
    out["torch_version"] = str(getattr(torch, "__version__", None))
    out["torch_hip"] = getattr(getattr(torch, "version", None), "hip", None)
    out["torch_cuda"] = getattr(getattr(torch, "version", None), "cuda", None)

    try:
        available = bool(torch.cuda.is_available())
        out["torch_cuda_is_available"] = available
    except Exception as exc:
        out["torch_cuda_is_available"] = False
        out["cuda_check_error"] = f"{type(exc).__name__}: {exc}"

    if out["torch_cuda_is_available"]:
        try:
            count = int(torch.cuda.device_count())
            out["device_count"] = count
            for i in range(count):
                try:
                    out["device_names"].append(str(torch.cuda.get_device_name(i)))
                except Exception as exc:
                    out["device_names"].append(
                        f"<device-name-error:{type(exc).__name__}>"
                    )
        except Exception as exc:
            out["device_count_error"] = f"{type(exc).__name__}: {exc}"
except Exception as exc:
    out["import_error_type"] = type(exc).__name__
    out["import_error"] = str(exc)[:1500]

print(json.dumps(out, ensure_ascii=False))
'''


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_cmd(
    argv: list[str],
    *,
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def listener_pids(port: int) -> tuple[list[int], str | None]:
    ss = shutil.which("ss")
    if not ss:
        return [], "ss not found"

    try:
        cp = run_cmd(
            [ss, "-H", "-ltnp", f"sport = :{port}"],
            timeout=2.0,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        return [], "ss timeout"
    except OSError as exc:
        return [], f"ss error: {type(exc).__name__}: {exc}"

    if cp.returncode != 0:
        return [], f"ss rc={cp.returncode}: {cp.stderr.strip()[:300]}"

    pids: set[int] = set()
    for line in cp.stdout.splitlines():
        for found in re.findall(r"\bpid=(\d+),", line):
            pids.add(int(found))
    return sorted(pids), None


def read_proc_environ(pid: int) -> tuple[dict[str, str], str | None]:
    try:
        items = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    except Exception as exc:
        return {}, type(exc).__name__

    env: dict[str, str] = {}
    for item in items:
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env, None


def proc_info(pid: int) -> tuple[dict[str, Any], dict[str, str]]:
    proc = Path("/proc") / str(pid)
    info: dict[str, Any] = {"pid": pid}

    try:
        info["exe"] = str((proc / "exe").resolve(strict=True))
    except Exception as exc:
        info["exe"] = None
        info["exe_error"] = type(exc).__name__

    try:
        info["cwd"] = str((proc / "cwd").resolve(strict=True))
    except Exception as exc:
        info["cwd"] = None
        info["cwd_error"] = type(exc).__name__

    try:
        raw = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        info["argv"] = [os.fsdecode(x) for x in raw if x]
        info["cmd"] = " ".join(info["argv"])
    except Exception as exc:
        info["argv"] = []
        info["cmd"] = None
        info["cmd_error"] = type(exc).__name__

    process_env, env_error = read_proc_environ(pid)
    info["safe_env"] = {
        key: process_env[key] for key in SAFE_ENV_KEYS if key in process_env
    }
    if env_error:
        info["env_error"] = env_error

    return info, process_env


def env_prefix_from_python(python_path: str | None) -> str | None:
    if not python_path:
        return None
    p = Path(python_path)
    if p.parent.name == "bin":
        return str(p.parent.parent)
    return None


def build_probe_env(process_env: dict[str, str] | None = None) -> dict[str, str]:
    # Start from the audit process environment for locale and normal runtime basics.
    env = dict(os.environ)
    if process_env:
        for key in PROBE_ENV_KEYS:
            if key in process_env:
                env[key] = process_env[key]
    env["PYTHONNOUSERSITE"] = "1"
    env.setdefault("LC_ALL", "C")
    return env


def probe_python(
    python_path: str,
    timeout: float,
    *,
    process_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "probe_python": python_path,
        "probe_ok": False,
    }

    try:
        cp = run_cmd(
            [python_path, "-I", "-c", PROBE_CODE],
            timeout=timeout,
            env=build_probe_env(process_env),
        )
    except subprocess.TimeoutExpired:
        result["probe_error"] = f"timeout>{timeout}s"
        return result
    except OSError as exc:
        result["probe_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["probe_rc"] = cp.returncode
    if cp.returncode != 0:
        result["probe_error"] = (cp.stderr or cp.stdout)[-1800:]
        return result

    payload: dict[str, Any] | None = None
    for line in reversed(cp.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break

    if payload is None:
        result["probe_error"] = (
            "probe produced no JSON; stdout tail="
            + cp.stdout[-1000:]
            + "; stderr tail="
            + cp.stderr[-1000:]
        )
        return result

    result.update(payload)
    result["probe_ok"] = True
    if cp.stderr.strip():
        result["probe_stderr_tail"] = cp.stderr[-1500:]
    return result


def torch_backend(probe: dict[str, Any]) -> str:
    if not probe.get("probe_ok"):
        return "PROBE_ERROR"
    if not probe.get("torch_importable"):
        if probe.get("torch_metadata_version"):
            return "TORCH_IMPORT_ERROR"
        return "NO_TORCH"
    if probe.get("torch_hip"):
        return "ROCm"
    if probe.get("torch_cuda"):
        return "CUDA"
    return "CPU"


def looks_like_amd_device(names: list[str]) -> bool:
    return any(
        any(marker in name.upper() for marker in ("AMD", "INSTINCT", "MI300", "MI250", "MI210"))
        for name in names
    )


def service_verdict(
    gpu_policy: str,
    listening: bool,
    probe: dict[str, Any] | None,
) -> tuple[str, str]:
    if not listening:
        if gpu_policy == "retired":
            return "PASS_RETIRED_DOWN", "retired port is down"
        return "DOWN", "no listener"

    if gpu_policy == "retired":
        return "FAIL_RETIRED_LISTENING", "retired port unexpectedly has a listener"

    if probe is None:
        return "UNKNOWN", "listener interpreter could not be probed"

    backend = torch_backend(probe)
    available = bool(probe.get("torch_cuda_is_available"))
    devices = list(probe.get("device_names") or [])
    amd_device = looks_like_amd_device(devices)
    gpu_required = gpu_policy in ("required", "required_if_listening")

    if gpu_required:
        if backend == "ROCm" and available and amd_device:
            return "PASS_ROCM", "ROCm PyTorch is active and sees an AMD GPU"
        if backend == "ROCm" and available:
            return "WARN_ROCM_DEVICE_NAME", "ROCm is active but AMD device identity was not recognized"
        if backend == "ROCm" and not available:
            return "FAIL_ROCM_UNAVAILABLE", "ROCm build is installed but torch.cuda.is_available() is false"
        if backend == "CUDA":
            return "FAIL_NVIDIA_CUDA_BUILD", "GPU-required service is using an NVIDIA CUDA PyTorch build"
        if backend == "CPU":
            return "FAIL_CPU_TORCH", "GPU-required service is using CPU-only PyTorch"
        if backend == "NO_TORCH":
            return "FAIL_NO_TORCH", "GPU-required service interpreter has no PyTorch"
        if backend == "TORCH_IMPORT_ERROR":
            return "FAIL_TORCH_IMPORT", "PyTorch package exists but cannot be imported"
        return "FAIL_PROBE", "runtime probe failed"

    # Router/API/gateway processes do not need GPU acceleration themselves.
    if backend == "CUDA":
        return "WARN_CUDA_BUILD_NON_GPU_SERVICE", (
            "service is control-plane/API only, but its interpreter contains an NVIDIA CUDA PyTorch build"
        )
    if backend == "ROCm":
        return "PASS_ROCM_NON_GPU_SERVICE", (
            "ROCm PyTorch is present; this service does not itself require GPU acceleration"
        )
    if backend == "TORCH_IMPORT_ERROR":
        return "WARN_TORCH_IMPORT_NON_GPU_SERVICE", (
            "service does not require PyTorch, but an installed torch package cannot import"
        )
    return "PASS_CONTROL_PLANE", "GPU acceleration is not required for this service"


def conda_env_pythons() -> list[Path]:
    found: list[Path] = []
    conda = shutil.which("conda")
    if not conda:
        return found
    try:
        cp = run_cmd([conda, "env", "list", "--json"], timeout=8.0)
        if cp.returncode == 0:
            payload = json.loads(cp.stdout)
            for prefix in payload.get("envs", []):
                p = Path(prefix) / "bin" / "python"
                if p.exists():
                    found.append(p)
    except Exception:
        pass
    return found


def discover_python_envs(root: Path) -> list[Path]:
    candidates: list[Path] = []

    for p in (Path("/opt/conda/bin/python"), Path("/opt/conda/envs/rocm_env/bin/python")):
        if p.exists():
            candidates.append(p)

    env_root = Path("/opt/conda/envs")
    if env_root.is_dir():
        candidates.extend(p for p in sorted(env_root.glob("*/bin/python")) if p.exists())

    candidates.extend(conda_env_pythons())

    for rel in ("venv/bin/python", ".venv/bin/python"):
        p = root / rel
        if p.exists():
            candidates.append(p)

    # One interpreter per environment prefix.
    by_prefix: dict[str, Path] = {}
    for p in candidates:
        try:
            prefix = str(p.resolve().parent.parent)
        except Exception:
            prefix = str(p.parent.parent)
        by_prefix.setdefault(prefix, p)
    return list(by_prefix.values())


def service_console_table(rows: list[dict[str, Any]]) -> None:
    headers = ("PORT", "ROLE", "PID", "PYTHON ENV", "TORCH", "BACKEND", "GPU", "VERDICT")
    widths = (6, 22, 9, 34, 22, 10, 25, 31)

    def cell(value: Any, width: int) -> str:
        s = str(value if value is not None else "-")
        return s if len(s) <= width else s[: max(1, width - 1)] + "…"

    line = " ".join(cell(h, w).ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        probe = row.get("probe") or {}
        devices = ", ".join(probe.get("device_names") or []) or "-"
        values = (
            row["port"],
            row["role"],
            row.get("pid") or "-",
            row.get("env_prefix") or "-",
            probe.get("torch_version") or probe.get("torch_metadata_version") or "-",
            row.get("torch_backend") or "-",
            devices,
            row.get("verdict") or "-",
        )
        print(" ".join(cell(v, w).ljust(w) for v, w in zip(values, widths)))


def write_services_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = (
        "port",
        "role",
        "gpu_policy",
        "listening",
        "pid",
        "exe",
        "env_prefix",
        "torch_version",
        "torch_hip",
        "torch_cuda",
        "torch_cuda_is_available",
        "device_names",
        "torch_backend",
        "verdict",
        "verdict_detail",
    )
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("\t".join(columns) + "\n")
        for row in rows:
            probe = row.get("probe") or {}
            values: dict[str, Any] = {
                **row,
                "torch_version": probe.get("torch_version") or probe.get("torch_metadata_version"),
                "torch_hip": probe.get("torch_hip"),
                "torch_cuda": probe.get("torch_cuda"),
                "torch_cuda_is_available": probe.get("torch_cuda_is_available"),
                "device_names": "; ".join(probe.get("device_names") or []),
            }
            f.write(
                "\t".join(
                    str(values.get(col, "") if values.get(col, "") is not None else "")
                    .replace("\t", " ")
                    .replace("\n", " ")
                    for col in columns
                )
                + "\n"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit BLINK service Python/ROCm runtimes")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--ports", default=",".join(map(str, DEFAULT_PORTS)))
    ap.add_argument("--output-dir", type=Path, default=None)
    ap.add_argument("--probe-timeout", type=float, default=25.0)
    ap.add_argument(
        "--no-scan-all-envs",
        action="store_true",
        help="only audit interpreters used by listening services",
    )
    args = ap.parse_args()

    root = args.root.resolve()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir or (root / "ops" / "python-rocm-audit" / stamp)
    output_dir.mkdir(parents=True, exist_ok=False)

    ports = [int(x.strip()) for x in args.ports.split(",") if x.strip()]

    host: dict[str, Any] = {
        "timestamp_utc": utc_now(),
        "hostname": platform.node(),
        "root": str(root),
        "amd_smi": shutil.which("amd-smi"),
        "hipcc": shutil.which("hipcc") or (
            "/opt/rocm/bin/hipcc" if Path("/opt/rocm/bin/hipcc").exists() else None
        ),
    }
    if host["hipcc"]:
        try:
            cp = run_cmd([host["hipcc"], "--version"], timeout=5.0)
            host["hipcc_version"] = (cp.stdout + cp.stderr)[-3000:]
        except Exception as exc:
            host["hipcc_error"] = type(exc).__name__

    service_rows: list[dict[str, Any]] = []
    # Cache only inventory probes. Live process probes must keep the process env.
    inventory_probe_cache: dict[str, dict[str, Any]] = {}

    for port in ports:
        policy = SERVICE_POLICY.get(
            port, {"role": "Unclassified service", "gpu_policy": "not_required"}
        )
        pids, listener_error = listener_pids(port)
        row: dict[str, Any] = {
            "port": port,
            "role": policy["role"],
            "gpu_policy": policy["gpu_policy"],
            "listening": bool(pids),
            "listener_pids": pids,
            "listener_error": listener_error,
        }

        if len(pids) > 1:
            row["verdict"] = "UNKNOWN_MULTIPLE_PIDS"
            row["verdict_detail"] = f"multiple listener PIDs: {pids}"
            service_rows.append(row)
            continue

        if not pids:
            verdict, detail = service_verdict(policy["gpu_policy"], False, None)
            row["verdict"] = verdict
            row["verdict_detail"] = detail
            service_rows.append(row)
            continue

        pid = pids[0]
        pinfo, process_env = proc_info(pid)
        row.update(pinfo)
        row["pid"] = pid
        row["env_prefix"] = env_prefix_from_python(pinfo.get("exe"))

        python_path = pinfo.get("exe")
        probe = (
            probe_python(
                python_path,
                args.probe_timeout,
                process_env=process_env,
            )
            if python_path
            else None
        )
        row["probe"] = probe
        row["torch_backend"] = torch_backend(probe or {})
        verdict, detail = service_verdict(policy["gpu_policy"], True, probe)
        row["verdict"] = verdict
        row["verdict_detail"] = detail
        service_rows.append(row)

    active_prefix_ports: dict[str, list[int]] = {}
    for row in service_rows:
        prefix = row.get("env_prefix")
        if prefix:
            active_prefix_ports.setdefault(prefix, []).append(int(row["port"]))

    all_env_rows: list[dict[str, Any]] = []
    if not args.no_scan_all_envs:
        for python_path in discover_python_envs(root):
            python_string = str(python_path)
            try:
                prefix = str(python_path.resolve().parent.parent)
            except Exception:
                prefix = str(python_path.parent.parent)

            probe = inventory_probe_cache.get(python_string)
            if probe is None:
                probe = probe_python(python_string, args.probe_timeout)
                inventory_probe_cache[python_string] = probe

            backend = torch_backend(probe)
            all_env_rows.append(
                {
                    "env_prefix": prefix,
                    "python": python_string,
                    "active_service_ports": active_prefix_ports.get(prefix, []),
                    "torch_backend": backend,
                    "torch_version": probe.get("torch_version")
                    or probe.get("torch_metadata_version"),
                    "torch_hip": probe.get("torch_hip"),
                    "torch_cuda": probe.get("torch_cuda"),
                    "torch_cuda_is_available": probe.get("torch_cuda_is_available"),
                    "device_names": probe.get("device_names") or [],
                    "cuda_build_found": backend == "CUDA",
                    "rocm_build_found": backend == "ROCm",
                    "probe": probe,
                }
            )

    report = {
        "schema_version": 2,
        "host": host,
        "service_policy": SERVICE_POLICY,
        "services": service_rows,
        "python_environments": all_env_rows,
    }

    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_services_tsv(output_dir / "services.tsv", service_rows)

    with (output_dir / "python-environments.tsv").open("w", encoding="utf-8") as f:
        cols = (
            "env_prefix",
            "active_service_ports",
            "torch_version",
            "torch_backend",
            "torch_hip",
            "torch_cuda",
            "torch_cuda_is_available",
            "device_names",
        )
        f.write("\t".join(cols) + "\n")
        for row in all_env_rows:
            values = dict(row)
            values["active_service_ports"] = ",".join(map(str, row["active_service_ports"]))
            values["device_names"] = "; ".join(row.get("device_names") or [])
            f.write(
                "\t".join(
                    str(values.get(col, "") if values.get(col, "") is not None else "")
                    .replace("\t", " ")
                    .replace("\n", " ")
                    for col in cols
                )
                + "\n"
            )

    print("\n===== ACTIVE SERVICE ROCm AUDIT =====")
    service_console_table(service_rows)

    active_failures = [
        row for row in service_rows if str(row.get("verdict", "")).startswith("FAIL_")
    ]
    warnings = [
        row for row in service_rows if str(row.get("verdict", "")).startswith("WARN_")
    ]
    stray_cuda = [row for row in all_env_rows if row.get("cuda_build_found")]

    print("\n===== IMPORTANT FINDINGS =====")
    if active_failures:
        for row in active_failures:
            print(
                f"[FAIL] :{row['port']} {row['role']}: "
                f"{row['verdict']} — {row['verdict_detail']}"
            )
    else:
        print(
            "[OK] No GPU-required listening service was detected using "
            "NVIDIA CUDA / CPU-only / unavailable ROCm PyTorch."
        )

    for row in warnings:
        print(
            f"[WARN] :{row['port']} {row['role']}: "
            f"{row['verdict']} — {row['verdict_detail']}"
        )

    if stray_cuda:
        print("\nCUDA/NVIDIA PyTorch environments discovered:")
        for row in stray_cuda:
            ports_text = (
                ",".join(map(str, row["active_service_ports"]))
                if row["active_service_ports"]
                else "unused-by-scanned-services"
            )
            print(
                f"  {row['env_prefix']} | torch={row['torch_version']} | "
                f"torch.version.cuda={row['torch_cuda']} | services={ports_text}"
            )
    else:
        print("\nNo CUDA/NVIDIA PyTorch build was discovered in scanned environments.")

    print(f"\nArtifacts: {output_dir}")
    print(f"  {output_dir / 'report.json'}")
    print(f"  {output_dir / 'services.tsv'}")
    print(f"  {output_dir / 'python-environments.tsv'}")

    return 2 if active_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

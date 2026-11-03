#!/usr/bin/env python3
"""Build the current frontend in an isolated release; never start/promote it.

Commands: prepare, build, check-fingerprint. No package installation, live
source edits, production process control, or environment values in reports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


ROOT = Path("/var/lib/jenkins/tomod")
RELEASE = "kg-knowledge-20261031T0630Z"
NODE_DIRECTORY = Path("/opt/node-v22.23.4/bin")
PAGE = "app/knowledge-map/page.tsx"
SKIP_DIRS = {
    "node_modules", ".next", ".git", "public", ".turbo", "coverage",
    "playwright-report", "test-results", "dist", "build", "out", ".cache",
    ".venv", ".pytest_cache", "__pycache__", "logs", "tmp", "temp", "recovery-backups",
}


class BuildError(Exception):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_files(directory):
    """Reject unexpected source symlinks instead of following external data."""
    files = {}
    for parent, directories, filenames in os.walk(directory, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in SKIP_DIRS)
        for name in directories:
            if (Path(parent) / name).is_symlink():
                raise BuildError("source_directory_symlink_requires_review")
        for name in sorted(filenames):
            if name == ".DS_Store" or name.endswith((".tsbuildinfo", ".log", ".pyc")):
                continue
            path = Path(parent) / name
            if path.is_symlink():
                raise BuildError("source_file_symlink_requires_review")
            if path.is_file():
                files[path.relative_to(directory).as_posix()] = sha(path)
    return files


def public_fingerprint(directory):
    """Hash shared static assets without copying them into the release."""
    if not directory.is_dir():
        raise BuildError("public_directory_missing")
    result = {}
    for parent, directories, filenames in os.walk(directory, followlinks=False):
        directories.sort()
        for name in directories:
            if (Path(parent) / name).is_symlink():
                raise BuildError("public_symlink_requires_review")
        for name in sorted(filenames):
            path = Path(parent) / name
            if path.is_symlink():
                raise BuildError("public_symlink_requires_review")
            if path.is_file():
                result[path.relative_to(directory).as_posix()] = sha(path)
    return result


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


class FrontendBuild:
    def __init__(self, root=ROOT):
        # Root injection is unit-test-only, not exposed through the CLI.
        self.root = Path(root).resolve()
        self.live = self.root / "frontend"
        self.release = self.root / "ops" / "releases" / RELEASE
        self.candidate = self.release / "candidate"
        self.frontend = self.release / "frontend"
        self.manifest_path = self.release / "frontend-build-manifest.json"
        self.result_path = self.release / "frontend-build-result.json"

    def _manifest(self):
        try:
            result = json.loads(self.manifest_path.read_text())
        except (OSError, ValueError):
            raise BuildError("build_manifest_missing_or_invalid") from None
        if result.get("schema") != 1 or result.get("release") != RELEASE:
            raise BuildError("build_manifest_missing_or_invalid")
        return result

    def _dependencies(self):
        dependencies = self.live / "node_modules"
        if not dependencies.is_dir():
            raise BuildError("existing_dependencies_missing")
        result = {}
        for name in ("next", "typescript", "react", "react-dom"):
            path = dependencies / name / "package.json"
            if path.is_file():
                result[f"node_modules/{name}/package.json"] = sha(path)
        for name in ("node", "npm"):
            path = NODE_DIRECTORY / name
            if not path.is_file():
                raise BuildError("node_runtime_missing")
            result[f"runtime/{name}"] = sha(path)
        return result

    def _check(self, manifest):
        current = source_files(self.live)
        expected = dict(manifest["live_source_sha256"])
        page_state = "original"
        if current.get(PAGE) == manifest["candidate_page_sha256"]:
            expected[PAGE] = manifest["candidate_page_sha256"]
            page_state = "candidate"
        changed = sorted(name for name in set(expected) | set(current) if expected.get(name) != current.get(name))
        if changed:
            raise BuildError("live_source_fingerprint_changed")
        if public_fingerprint(self.live / "public") != manifest["public_sha256"]:
            raise BuildError("shared_public_fingerprint_changed")
        if self._dependencies() != manifest["dependency_runtime_sha256"]:
            raise BuildError("dependency_runtime_fingerprint_changed")
        return {"ok": True, "code": "fingerprint_matched", "page_state": page_state,
                "source_files": len(current), "live_modified_by_helper": False}

    def check_fingerprint(self):
        return self._check(self._manifest())

    def prepare(self):
        if self.frontend.exists() or self.frontend.is_symlink() or self.manifest_path.exists():
            raise BuildError("isolated_frontend_already_exists")
        if not self.live.is_dir() or self.live.is_symlink():
            raise BuildError("canonical_frontend_missing")
        try:
            candidate_manifest = json.loads((self.candidate / "manifest.json").read_text())
            page_entry = candidate_manifest["changed"]["frontend/" + PAGE]
        except (OSError, ValueError, KeyError, TypeError):
            raise BuildError("candidate_manifest_missing_or_invalid") from None
        candidate_page = self.candidate / "frontend" / PAGE
        baseline = source_files(self.live)
        if baseline.get(PAGE) != page_entry["before_sha256"]:
            raise BuildError("live_knowledge_page_changed")
        if sha(candidate_page) != page_entry["after_sha256"]:
            raise BuildError("candidate_knowledge_page_changed")
        assets = public_fingerprint(self.live / "public")
        dependencies = self._dependencies()
        self.frontend.mkdir(parents=True)
        for name, expected in baseline.items():
            source, destination = self.live / name, self.frontend / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.name.startswith(".env"):
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
            else:
                shutil.copy2(source, destination)
            if sha(destination) != expected:
                raise BuildError("source_changed_during_copy")
        (self.frontend / "node_modules").symlink_to(self.live / "node_modules", target_is_directory=True)
        (self.frontend / "public").symlink_to(self.live / "public", target_is_directory=True)
        shutil.copy2(candidate_page, self.frontend / PAGE)
        config = self.frontend / "next.config.mjs"
        text = config.read_text()
        matches = list(re.finditer(r"\bconst\s+nextConfig\s*=\s*\{", text))
        if len(matches) != 1 or re.search(r"\bturbopack\s*:", text):
            raise BuildError("turbopack_config_requires_review")
        position = matches[0].end()
        text = text[:position] + (
            "\n  // Build isolation only: linked dependencies share this root.\n"
            f"  turbopack: {{ root: {json.dumps(str(self.root))} }},\n"
        ) + text[position:]
        config.write_text(text)
        manifest = {
            "schema": 1, "release": RELEASE, "created_epoch": int(time.time()),
            "live_source_sha256": baseline, "public_sha256": assets,
            "dependency_runtime_sha256": dependencies,
            "dependency_scope": "lockfiles_and_key_packages_not_full_node_modules",
            "candidate_page_sha256": page_entry["after_sha256"],
            "candidate_initial_source_sha256": source_files(self.frontend),
            "build_only_override": {"key": "turbopack.root", "live_config_changed": False},
            "private_environment_copied": True,
            "environment_values_in_report": False,
            "live_modified_by_helper": False,
        }
        private_json(self.manifest_path, manifest)
        self._check(manifest)
        return {"ok": True, "code": "isolated_frontend_prepared", "frontend": str(self.frontend),
                "manifest": str(self.manifest_path), "source_files": len(baseline),
                "live_modified_by_helper": False}

    def _run(self, script):
        log_path = self.release / f"frontend-{script}.log"
        descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        environment = {key: os.environ[key] for key in ("HOME", "LANG", "LC_ALL", "TZ", "TMPDIR") if key in os.environ}
        environment.update({
            "PATH": str(NODE_DIRECTORY) + ":/usr/local/bin:/usr/bin:/bin",
            "LOCAL_BACKEND_OVERRIDE": "1", "BACKEND_API_URL": "http://127.0.0.1:8010",
            "INTERNAL_API_ORIGIN": "http://127.0.0.1:8010", "NEXT_TELEMETRY_DISABLED": "1",
        })
        try:
            with os.fdopen(descriptor, "ab", buffering=0) as logfile:
                completed = subprocess.run([str(NODE_DIRECTORY / "npm"), "run", script],
                                           cwd=self.frontend, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=logfile, stderr=subprocess.STDOUT, timeout=900, check=False)
        except subprocess.TimeoutExpired:
            raise BuildError(f"{script}_timed_out") from None
        return {"exit_code": completed.returncode, "log": str(log_path)}

    def build(self):
        if not self.manifest_path.exists():
            self.prepare()
        manifest = self._manifest()
        self._check(manifest)
        if source_files(self.frontend) != manifest["candidate_initial_source_sha256"]:
            raise BuildError("isolated_build_source_changed")
        result = {"ok": False, "code": "not_built", "frontend": str(self.frontend),
                  "manifest": str(self.manifest_path), "live_modified_by_helper": False}
        for stage in ("typecheck", "build"):
            self._check(manifest)
            result[stage] = self._run(stage)
            if result[stage]["exit_code"] != 0:
                result["code"] = f"{stage}_failed"
                private_json(self.result_path, result)
                return result
        self._check(manifest)
        build_id = self.frontend / ".next" / "BUILD_ID"
        if not build_id.is_file() or not build_id.read_text().strip():
            raise BuildError("next_build_id_missing")
        result.update(ok=True, code="frontend_build_passed", build_id=build_id.read_text().strip(),
                      build_id_sha256=sha(build_id),
                      candidate_final_source_sha256=source_files(self.frontend),
                      deployment_performed=False, server_started=False)
        private_json(self.result_path, result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "build", "check-fingerprint"))
    args = parser.parse_args(argv)
    try:
        action = args.action.replace("-", "_")
        result = getattr(FrontendBuild(), action)()
    except BuildError as error:
        result = {"ok": False, "code": str(error), "deployment_performed": False}
    except Exception:
        result = {"ok": False, "code": "frontend_build_operation_failed", "deployment_performed": False}
    print(json.dumps(result, allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())

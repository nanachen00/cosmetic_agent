#!/usr/bin/env python3
"""Scoped knowledge-backend activation. Never restarts frontend or model services."""
from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from dotenv import dotenv_values, set_key, unset_key

ROOT = Path("/var/lib/jenkins/tomod")
RELEASE = "kg-knowledge-20261031T0630Z"
HELPERS = {
    "manage_knowledge_backend.py": "ops/runtime/manage-knowledge-backend.py",
    "prepare_frontend_build.py": "ops/runtime/knowledge_frontend_build.py",
    "manage_knowledge_frontend.py": "ops/runtime/manage-knowledge-frontend.py",
}
ENVIRONMENT = {
    "APP_RELEASE": RELEASE, "RERANKER_BASE_URL": "http://127.0.0.1:8006/v1",
    "RERANKER_MODEL": "BAAI/bge-reranker-v2-m3", "RERANKER_MODEL_NAME": "BAAI/bge-reranker-v2-m3",
    "RERANKER_DEVICE": "cuda", "KNOWLEDGE_GRAPH_ENABLED": "1", "KNOWLEDGE_QUERY_PIPELINE_ENABLED": "1",
    "KNOWLEDGE_EXPLORER_SOURCE": "neo4j-reviewed", "KNOWLEDGE_CURATED_REQUIRED": "1",
    "KNOWLEDGE_EXPLAINER_PROVIDER": "terra",
    "KNOWLEDGE_MIN_SEMANTIC_SCORE": "0.45",
    "KNOWLEDGE_QUERY_ADMISSION_REQUIRE_REDIS": "1",
}


class ActivationError(Exception):
    pass


def digest(path):
    if path.is_symlink():
        raise ActivationError("symlink_target_rejected")
    if path.exists() and not path.is_file():
        raise ActivationError("non_file_target_rejected")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def private_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, indent=2)
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class Installer:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.release = self.root / "ops/releases" / RELEASE
        self.candidate = self.release / "candidate"
        self.bundle = self.root / "ops/releases/kg-reranker-bundle-20261031"
        self.archive = self.root / "ops/archive" / RELEASE
        self.version = self.release / "source-control"
        self.state_path = self.release / "deployment-state.json"
        self.env_path = self.root / "cosmetic-agent/.env"

    def safe(self, path):
        try:
            path.relative_to(self.root)
        except ValueError:
            raise ActivationError("path_outside_project") from None
        for current in (path, *path.parents):
            if current == self.root:
                break
            if current.is_symlink():
                raise ActivationError("symlink_target_rejected")
        return path

    @contextlib.contextmanager
    def lock(self):
        runtime = self.root / "ops/runtime"
        self.safe(runtime)
        runtime.mkdir(parents=True, exist_ok=True)
        if runtime.is_symlink():
            raise ActivationError("runtime_symlink_rejected")
        fd = os.open(runtime / "knowledge-activation.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ActivationError("activation_busy") from None
            yield
        finally:
            os.close(fd)

    def plan(self):
        manifest = json.loads(self.safe(self.candidate / "manifest.json").read_text())
        plan = {}
        for name, row in manifest["changed"].items():
            if name == "frontend/app/knowledge-map/page.tsx":
                continue  # Backend activation intentionally does not publish frontend.
            parts = Path(name).parts
            if not parts or parts[0] != "app" or ".." in parts or not name.endswith(".py"):
                raise ActivationError("candidate_path_rejected")
            source, target = self.safe(self.candidate / name), "cosmetic-agent/" + name
            self.safe(self.root / target)
            if digest(source) != row["after_sha256"] or digest(self.root / target) != row["before_sha256"]:
                raise ActivationError("candidate_or_live_hash_changed")
            ast.parse(source.read_text())
            plan[target] = {"source": str(source), "before": row["before_sha256"], "after": row["after_sha256"]}
        if not plan:
            raise ActivationError("empty_candidate")
        for name, target in HELPERS.items():
            source = self.safe(self.bundle / name)
            self.safe(self.root / target)
            ast.parse(source.read_text())
            plan[target] = {"source": str(source), "before": digest(self.root / target), "after": digest(source)}
        return plan

    def check_hashes(self, files, field):
        return [name for name, row in files.items() if digest(self.safe(self.root / name)) != row[field]]

    def read_env(self):
        self.safe(self.env_path)
        if self.env_path.is_symlink() or not self.env_path.is_file():
            raise ActivationError("canonical_env_missing")
        return dotenv_values(self.env_path, interpolate=False)

    def git(self, *arguments):
        subprocess.run(["git", "-C", str(self.version), *arguments], capture_output=True,
                       check=True, timeout=10, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.version)})

    def checkpoint(self, message):
        self.git("add", "--all", ".")
        self.git("-c", "user.name=Codex Agent", "-c", "user.email=codex@localhost",
                 "commit", "--allow-empty", "-q", "-m", message)

    def copy_target(self, source, target):
        self.safe(source)
        self.safe(target)
        if target.is_symlink():
            raise ActivationError("symlink_target_rejected")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
        shutil.copy2(source, temporary)
        os.replace(temporary, target)

    def manager(self):
        path = self.safe(self.root / HELPERS["manage_knowledge_backend.py"])
        spec = importlib.util.spec_from_file_location("blink_activation_backend_manager", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.Manager(self.root)

    def set_owned_env(self, values):
        self.safe(self.env_path)
        for key, value in values.items():
            if value is None:
                unset_key(str(self.env_path), key)
            else:
                set_key(str(self.env_path), key, value, quote_mode="always")

    def restore(self, state, manager, *, strict):
        # Never overwrite source or owned configuration changed by another operator.
        current = self.read_env()
        bad = self.check_hashes(state["files"], "after") if strict else [
            name for name, row in state["files"].items()
            if digest(self.safe(self.root / name)) not in (row["before"], row["after"])]
        if bad:
            raise ActivationError("rollback_source_conflict")
        for name, row in state["files"].items():
            if row["before"] is not None and digest(self.safe(self.archive / name)) != row["before"]:
                raise ActivationError("rollback_backup_hash_mismatch")
        for key in ENVIRONMENT:
            acceptable = (ENVIRONMENT[key],) if strict else (ENVIRONMENT[key], state["env_before"][key])
            if current.get(key) not in acceptable:
                raise ActivationError("rollback_owned_env_conflict")
        manager.run("stop")
        for name, row in state["files"].items():
            target = self.root / name
            if row["before"] is None:
                if target.exists():
                    saved = self.archive / "removed-new-files" / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    if saved.exists():
                        raise ActivationError("rollback_archive_exists")
                    shutil.move(str(target), str(saved))
            else:
                self.copy_target(self.archive / name, target)
        self.set_owned_env(state["env_before"])
        state["status"] = "rolled_back"
        private_json(self.state_path, state)
        manager.run("start", state["env_before"].get("APP_RELEASE"))

    def install(self):
        if self.state_path.exists() or self.archive.exists() or self.version.exists():
            raise ActivationError("release_already_prepared")
        files, before_env = self.plan(), self.read_env()
        self.safe(self.archive)
        self.safe(self.version)
        self.archive.mkdir(parents=True, mode=0o700)
        os.chmod(self.archive, 0o700)
        self.version.mkdir(parents=True)
        shutil.copy2(self.env_path, self.archive / "canonical.env.private")
        os.chmod(self.archive / "canonical.env.private", 0o600)
        state = {"release": RELEASE, "status": "prepared", "files": files,
                 "env_before": {key: before_env.get(key) for key in ENVIRONMENT},
                 "env_after": ENVIRONMENT, "database_writes": 0, "frontend_restarted": False}
        for name, row in files.items():
            if row["before"] is not None:
                for directory in (self.archive, self.version):
                    saved = directory / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.root / name, saved)
        self.git("init", "-q")
        self.checkpoint("Checkpoint backend source before knowledge release")
        private_json(self.state_path, state)
        # Revalidate every target AND candidate immediately before stopping.
        if self.check_hashes(files, "before") or self.read_env() != before_env:
            raise ActivationError("baseline_changed_before_stop")
        for row in files.values():
            source = Path(row["source"])
            if digest(source) != row["after"]:
                raise ActivationError("candidate_changed_before_stop")
            ast.parse(source.read_text())
        manager, attempted_stop = None, False
        try:
            bootstrap = HELPERS["manage_knowledge_backend.py"]
            self.copy_target(Path(files[bootstrap]["source"]), self.root / bootstrap)
            manager = self.manager()
            attempted_stop = True
            manager.run("stop")
            for name, row in files.items():
                self.copy_target(Path(row["source"]), self.root / name)
                self.copy_target(Path(row["source"]), self.version / name)
            self.set_owned_env(ENVIRONMENT)
            if self.check_hashes(files, "after"):
                raise ActivationError("installed_source_hash_mismatch")
            self.checkpoint("Activate reviewed-cloud knowledge retrieval backend")
            state["status"] = "deployed_starting"
            private_json(self.state_path, state)
            manager.run("start", RELEASE)
        except Exception:
            if manager is not None and attempted_stop:
                try:
                    self.restore(state, manager, strict=False)
                except Exception:
                    state["status"] = "failed_manual_recovery_required"
                    private_json(self.state_path, state)
                    raise ActivationError("activation_failed_automatic_recovery_failed") from None
                raise ActivationError("activation_failed_previous_release_restored") from None
            bootstrap = HELPERS["manage_knowledge_backend.py"]
            row, target = files[bootstrap], self.root / bootstrap
            if digest(target) == row["after"]:
                if row["before"] is not None:
                    self.copy_target(self.archive / bootstrap, target)
                else:
                    saved = self.archive / "removed-new-files" / bootstrap
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(saved))
            raise ActivationError("activation_failed_before_backend_stop") from None
        return {"status": "deployed_starting", "release": RELEASE, "files": list(files),
                "readiness_verified": False, "database_writes": 0, "frontend_changed": False}

    def verify(self):
        state = json.loads(self.state_path.read_text())
        env = self.read_env()
        return {"status": state["status"], "mismatched_paths": self.check_hashes(state["files"], "after"),
                "owned_env_mismatches": [key for key, value in ENVIRONMENT.items() if env.get(key) != value],
                "app_release": env.get("APP_RELEASE"), "readiness_verified": False}

    def run(self, action):
        if action not in {"install", "rollback", "verify"}:
            raise ActivationError("invalid_action")
        with self.lock():
            if action == "install":
                return self.install()
            if action == "verify":
                return self.verify()
            state = json.loads(self.state_path.read_text())
            if state["status"] not in {"deployed_starting", "deployed"}:
                raise ActivationError("release_not_deployed")
            # Validate the manager itself before importing executable source.
            if self.check_hashes(state["files"], "after"):
                raise ActivationError("rollback_source_conflict")
            current = self.read_env()
            if any(current.get(key) != value for key, value in ENVIRONMENT.items()):
                raise ActivationError("rollback_owned_env_conflict")
            self.restore(state, self.manager(), strict=True)
            return {"status": "rolled_back", "release": RELEASE, "readiness_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "rollback", "verify"))
    args = parser.parse_args()
    try:
        print(json.dumps(Installer().run(args.action)))
        return 0
    except ActivationError as exc:
        print(json.dumps({"error": str(exc)}))
    except Exception as exc:
        print(json.dumps({"error": "activation_operation_failed", "error_type": type(exc).__name__}))
    return 1


if __name__ == "__main__":
    sys.exit(main())

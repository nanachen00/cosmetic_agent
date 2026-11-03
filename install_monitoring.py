"""Safely install BLINK AI monitoring integration into the current server files."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(
    "/var/lib/jenkins/tomod"
)
AGENT_ROOT = PROJECT_ROOT / "cosmetic-agent"
API_FILE = AGENT_ROOT / "api/api_server.py"
ORCHESTRATOR_FILE = AGENT_ROOT / "agents/root/orchestrator.py"
OBSERVABILITY_FILE = AGENT_ROOT / "core/observability.py"


def insert_once(
    source: str,
    marker: str,
    insertion: str,
    already_present: str,
    filename: str,
) -> tuple[str, bool]:
    if already_present in source:
        print(
            f"[SKIP] {filename}: integration already exists"
        )
        return source, False

    if marker not in source:
        raise RuntimeError(
            f"{filename}: insertion marker not found: {marker!r}"
        )

    return (
        source.replace(
            marker,
            marker + insertion,
            1,
        ),
        True,
    )


def validate_python(
    source: str,
    filename: Path,
) -> None:
    compile(
        source,
        str(filename),
        "exec",
    )


def atomic_write(
    path: Path,
    content: str,
) -> None:
    temporary = path.with_suffix(
        path.suffix + ".monitoring.tmp"
    )
    temporary.write_text(
        content,
        encoding="utf-8",
    )
    os.replace(
        temporary,
        path,
    )


def main() -> None:
    for required in (
        API_FILE,
        ORCHESTRATOR_FILE,
        OBSERVABILITY_FILE,
    ):
        if not required.is_file():
            raise FileNotFoundError(
                f"Required file does not exist: {required}"
            )

    api_source = API_FILE.read_text(
        encoding="utf-8"
    )
    orchestrator_source = ORCHESTRATOR_FILE.read_text(
        encoding="utf-8"
    )

    api_source, api_import_changed = insert_once(
        source=api_source,
        marker=(
            "from agents.root.orchestrator import "
            "RootAgentOrchestrator\n"
        ),
        insertion=(
            "from core.observability import "
            "install_observability\n"
        ),
        already_present=(
            "from core.observability import "
            "install_observability"
        ),
        filename=str(API_FILE),
    )

    api_source, api_install_changed = insert_once(
        source=api_source,
        marker="_sessions = {}\n",
        insertion=(
            "\n"
            "install_observability(\n"
            "    app,\n"
            "    session_count_provider=lambda: len(_sessions),\n"
            ")\n"
        ),
        already_present="session_count_provider=lambda: len(_sessions)",
        filename=str(API_FILE),
    )

    orchestrator_source, orchestrator_import_changed = insert_once(
        source=orchestrator_source,
        marker=(
            "from tools.agent.input_guard import guard_input\n"
        ),
        insertion=(
            "from core.observability import monitor_agent_turn\n"
        ),
        already_present=(
            "from core.observability import monitor_agent_turn"
        ),
        filename=str(ORCHESTRATOR_FILE),
    )

    decorator_present = (
        "    @monitor_agent_turn\n"
        "    def process(self, user_input: str) -> dict:"
    )

    if decorator_present in orchestrator_source:
        print(
            f"[SKIP] {ORCHESTRATOR_FILE}: decorator already exists"
        )
        orchestrator_decorator_changed = False
    else:
        process_marker = (
            "    def process(self, user_input: str) -> dict:"
        )

        if process_marker not in orchestrator_source:
            raise RuntimeError(
                f"{ORCHESTRATOR_FILE}: process() marker not found"
            )

        orchestrator_source = orchestrator_source.replace(
            process_marker,
            "    @monitor_agent_turn\n" + process_marker,
            1,
        )
        orchestrator_decorator_changed = True

    validate_python(
        api_source,
        API_FILE,
    )
    validate_python(
        orchestrator_source,
        ORCHESTRATOR_FILE,
    )

    changed = any(
        (
            api_import_changed,
            api_install_changed,
            orchestrator_import_changed,
            orchestrator_decorator_changed,
        )
    )

    if not changed:
        print(
            "[OK] Monitoring integration was already installed; no files changed."
        )
        return

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    api_backup = API_FILE.with_name(
        API_FILE.name + f".bak_monitoring_{timestamp}"
    )
    orchestrator_backup = ORCHESTRATOR_FILE.with_name(
        ORCHESTRATOR_FILE.name + f".bak_monitoring_{timestamp}"
    )

    shutil.copy2(
        API_FILE,
        api_backup,
    )
    shutil.copy2(
        ORCHESTRATOR_FILE,
        orchestrator_backup,
    )

    try:
        atomic_write(
            API_FILE,
            api_source,
        )
        atomic_write(
            ORCHESTRATOR_FILE,
            orchestrator_source,
        )
    except Exception:
        shutil.copy2(
            api_backup,
            API_FILE,
        )
        shutil.copy2(
            orchestrator_backup,
            ORCHESTRATOR_FILE,
        )
        raise

    print(
        "[OK] Monitoring integration installed."
    )
    print(
        f"[BACKUP] {api_backup}"
    )
    print(
        f"[BACKUP] {orchestrator_backup}"
    )


if __name__ == "__main__":
    main()

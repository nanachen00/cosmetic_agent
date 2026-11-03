"""Simulated production tree for the provenance tests (VM condition reproduced on any OS).

fixture/cosmetic-agent plus the production patterns that leak: api/api_server.py:8 and agents/root/orchestrator.py:11
`sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')`, api_server.py:101 skin_tracking env_file, and a sentinel .env.
As on the VM, the hard-coded literal names this tree itself.  schemas/intent.py is a stub: these tests check where modules
are imported from, not what they do, so pydantic is not needed."""
import json, shutil, time
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixture" / "cosmetic-agent"
LIT = "/var/lib/jenkins/tomod/cosmetic-agent"
SENTINEL = "SHADOW_SIM_PROD_SENTINEL"

API_SERVER = '''"""Sim of api/api_server.py: the hard-coded production path (line 8) and the skin_tracking env_file (line 101)."""
import os
import sys
sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')
from agents.root.orchestrator import Orchestrator  # noqa: E402


def _env_file_values(env_file):          # like SkinTrackingSettings.from_env_file: read only when the file exists
    if not os.path.exists(env_file): return {}
    with open(env_file, encoding="utf-8") as f: return dict(l.strip().split("=", 1) for l in f if "=" in l)


for _k, _v in _env_file_values('/var/lib/jenkins/tomod/cosmetic-agent/.env').items(): os.environ.setdefault(_k, _v)
STABLE = '/var/lib/jenkins/tomod/cosmetic-agent-stable/x'      # a different tree: init-shadow must leave it alone
app = object()
'''

INTENT_STUB = '''"""Stub of schemas/intent.py for the provenance tests (imported, never called)."""
class IntentParseResult: pass
class IntentType: FIND_PRODUCT = "find_product"
def calculate_routing_confidence(*a, **k): return 1.0
def calculate_task_completeness(*a, **k): return 1.0
'''


def _w(p: Path, text: str): p.write_bytes(text.encode("utf-8"))       # LF on every OS: the overlay anchors are LF


def make(parent: Path) -> Path:
    """parent/cosmetic-agent: the simulated production tree.  Returns its path."""
    t = parent / "cosmetic-agent"
    shutil.copytree(FIXTURE, t, ignore=shutil.ignore_patterns("__pycache__"))
    (t / "api").mkdir(); _w(t / "api" / "__init__.py", "")
    _w(t / "api" / "api_server.py", API_SERVER); _w(t / "schemas" / "intent.py", INTENT_STUB)
    o = t / "agents" / "root" / "orchestrator.py"; first, rest = o.read_bytes().decode("utf-8").split("\n", 1)
    _w(o, first + f"\nimport sys; sys.path.insert(0, '{LIT}')  # as production orchestrator.py:11\n" + rest)
    _w(t / ".env", f"{SENTINEL}=from-sim-prod\n")
    for p in t.rglob("*.py"): _w(p, p.read_bytes().decode("utf-8").replace(LIT, t.as_posix()))
    return t


def plain_copy(src: Path, dest: Path):
    """What init-shadow did up to r5: copy without .env and caches, mark, no rewrite."""
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".env*", "__pycache__"))
    (dest / ".shadow-copy").write_text(json.dumps({"from": str(src), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")

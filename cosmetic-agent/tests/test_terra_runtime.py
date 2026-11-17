"""Offline checks for the active Terra and optional legacy runtime paths."""

import importlib
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
from types import SimpleNamespace

from app.clients.terra_completion_client import TerraCompletionClient
from core.model_dependencies import model_readiness_checks
from tools.agent import input_guard, intent_parser
from tools.agent.response_composer import ResponseComposer
from tools.getzep.conversation_title_service import ConversationTitleService


class ResponsesOnlyClient:
    """A chat completion call would fail because this fake has no chat API."""

    def __init__(self, text):
        self.text = text
        self.calls = []
        self.options = []
        self.responses = self

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.text)


def terra_adapter(text):
    client = TerraCompletionClient.__new__(TerraCompletionClient)
    client.model = "gpt-5.6-terra"
    client.client = ResponsesOnlyClient(text)
    return client


def test_intent_uses_terra_selection_and_structured_responses(monkeypatch):
    try:
        with monkeypatch.context() as local:
            local.setenv("OPENAI_INTENT_MODEL", "gpt-4.1-mini")
            local.setenv("TERRA_MODEL", "gpt-5.6-terra")
            importlib.reload(intent_parser)
            client = ResponsesOnlyClient('{"intent_type":"knowledge_query"}')
            local.setattr(intent_parser, "_get_openai_client", lambda: client)
            local.setattr(intent_parser, "_has_valid_openai_key", lambda: True)
            result = intent_parser._call_openai(
                [{"role": "user", "content": "什麼是保濕？"}]
            )
            assert result == client.text
            call = client.calls[0]
            assert call["model"] == "gpt-5.6-terra"
            assert call["text"]["format"]["type"] == "json_schema"
            assert call["store"] is False
    finally:
        importlib.reload(intent_parser)


def test_ambiguous_guard_uses_responses_without_chat_api(monkeypatch):
    adapter = terra_adapter("IRRELEVANT")
    monkeypatch.setattr(input_guard, "client", adapter)
    result = input_guard.guard_input("今天天氣如何")
    assert result["allowed"] is False
    assert result["reason"] == "terra_offtopic"
    call = adapter.client.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["store"] is False
    assert call["reasoning"] == {"effort": os.getenv("TERRA_REASONING_EFFORT", "none")}
    assert adapter.client.options == [{"timeout": 8, "max_retries": 0}]


def test_guard_outage_preserves_existing_fallback(monkeypatch):
    class Unavailable:
        def complete(self, **kwargs):
            raise TimeoutError()

    monkeypatch.setattr(input_guard, "client", Unavailable())
    result = input_guard.guard_input("今天天氣如何")
    assert result == {"allowed": True, "reason": "terra_unavailable_passthrough"}


def test_pain_gain_uses_responses_without_chat_api(monkeypatch):
    from agents.root import orchestrator

    adapter = terra_adapter('{"pain":"使用者覺得乾燥","gain":"您希望保濕"}')
    monkeypatch.setattr(orchestrator, "_terra_client", adapter)
    result = orchestrator._analyze_pain_gain("最近覺得乾燥，希望保濕")
    assert result["pain"] == "您覺得乾燥"
    assert adapter.client.calls[0]["model"] == orchestrator.TERRA_MODEL
    assert adapter.client.calls[0]["store"] is False


def test_composer_ignores_legacy_model_override(monkeypatch):
    monkeypatch.setenv("TERRA_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("RESPONSE_COMPOSER_MODEL", "gpt-4.1-mini")
    client = ResponsesOnlyClient('{"reply":"請先告訴我您的需求。"}')
    composer = ResponseComposer(client=client, enabled=True, skip_intents=())
    result = composer.compose(
        user_query="您好", intent_type="unclear", raw_reply="請先告訴我您的需求。"
    )
    assert result.used_model
    assert client.calls[0]["model"] == "gpt-5.6-terra"
    assert client.calls[0]["store"] is False
    assert client.calls[0]["max_output_tokens"] == 600


def test_title_is_deterministic_when_legacy_runtime_disabled(monkeypatch):
    monkeypatch.setenv("CONVERSATION_TITLE_LLM", "1")  # A stale existing .env.
    monkeypatch.setenv("ENABLE_TAIDE", "0")
    title = ConversationTitleService(client=object())
    message = "我最近的皮膚感覺乾燥，希望找適合的保濕商品"
    assert not title.enabled
    assert title._summarize(message) == message[:16]


def test_title_defaults_to_no_model_even_if_taide_enabled(monkeypatch):
    monkeypatch.delenv("CONVERSATION_TITLE_LLM", raising=False)
    monkeypatch.setenv("ENABLE_TAIDE", "1")
    assert not ConversationTitleService(client=object()).enabled


def test_terra_readiness_does_not_probe_disabled_taide():
    seen = []
    checks = model_readiness_checks(
        lambda url: seen.append(url) or True,
        {"OPENAI_API_KEY": "offline-test-placeholder", "ENABLE_TAIDE": "0"},
    )
    assert set(checks) == {"terra_config", "embedding"}
    assert all(check() for check in checks.values())
    assert seen == ["http://127.0.0.1:8002/healthz"]


def test_optional_legacy_readiness_keeps_upstream_probes():
    seen = []
    checks = model_readiness_checks(
        lambda url: seen.append(url) or True,
        {"OPENAI_API_KEY": "offline-test-placeholder", "ENABLE_TAIDE": "1"},
    )
    assert all(check() for check in checks.values())
    assert set(seen) == {
        "http://127.0.0.1:8000/healthz", "http://127.0.0.1:8001/healthz",
        "http://127.0.0.1:8002/healthz",
    }


def test_terra_configuration_check_does_not_claim_inference_availability():
    checks = model_readiness_checks(lambda url: True, {"OPENAI_API_KEY": " "})
    assert checks["terra_config"]() is False


def test_asgi_readyz_passes_with_only_terra_configuration_and_embedding():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.observability import install_observability

    seen = []
    app = FastAPI()
    install_observability(app, readiness_checks=model_readiness_checks(
        lambda url: seen.append(url) or url.endswith(":8002/healthz"),
        {"OPENAI_API_KEY": "offline-test-placeholder", "ENABLE_TAIDE": "0"},
    ))
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["dependencies"]["terra_config"] == "ok"
    assert "taide" not in response.json()["dependencies"]
    assert seen == ["http://127.0.0.1:8002/healthz"]


def test_ssh_tunnel_omits_taide_forward_from_canonical_env(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "ops" / "runtime"
    runtime.mkdir(parents=True)
    (tmp_path / "cosmetic-agent").mkdir()
    (tmp_path / "cosmetic-agent" / ".env").write_text(
        "ENABLE_TAIDE=0\nMI300X_SSH_HOST=test@example.invalid\n"
        "ENABLE_PHOTO_MODEL=1\n"
    )
    shutil.copyfile(root / "ops/runtime/start-ssh-tunnel.sh", runtime / "start-ssh-tunnel.sh")
    # SSH target attestation is independently tested; no SSH/network in this test.
    verify = runtime / "verify-mi300x-ssh.sh"
    verify.write_text("#!/bin/sh\nexit 0\n")
    verify.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_SSH_ARGS"\n')
    ssh.chmod(0o755)
    args_path = tmp_path / "ssh-args"
    env = {
        **os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "TUNNEL_BACKGROUND": "0",
        "TEST_SSH_ARGS": str(args_path), "ENABLE_TAIDE": "1",
    }
    subprocess.run(["/bin/bash", str(runtime / "start-ssh-tunnel.sh")],
                   env=env, capture_output=True, text=True, check=True)
    args = args_path.read_text().splitlines()
    assert "127.0.0.1:8000:127.0.0.1:8000" not in args
    assert "127.0.0.1:8002:127.0.0.1:8002" in args
    assert "127.0.0.1:8025:127.0.0.1:8025" in args


def test_direct_router_entry_rejects_disabled_legacy_runtime(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "ops/runtime"
    runtime.mkdir(parents=True)
    (tmp_path / "cosmetic-agent").mkdir()
    (tmp_path / "cosmetic-agent/.env").write_text("ENABLE_TAIDE=0\n")
    shutil.copyfile(root / "ops/runtime/run-mac-service.sh", runtime / "run-mac-service.sh")
    (tmp_path / "pids").mkdir()
    result = subprocess.run(
        ["/bin/bash", str(runtime / "run-mac-service.sh"), "router", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "router is disabled" in result.stderr


def test_mac_status_uses_canonical_disabled_legacy_flag(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "ops/runtime"
    runtime.mkdir(parents=True)
    (tmp_path / "cosmetic-agent").mkdir()
    (tmp_path / "cosmetic-agent/.env").write_text(
        "ENABLE_TAIDE=0\nENABLE_PHOTO_MODEL=0\nENABLE_RERANKER=0\n"
    )
    build = tmp_path / 'frontend/.next'
    build.mkdir(parents=True)
    (build / 'BUILD_ID').write_text('status-test-build')
    shutil.copyfile(root / "ops/runtime/status-production.sh", runtime / "status-production.sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text('''#!/bin/sh
printf "%s\\n" "$@" >> "$TEST_CURL_ARGS"
case "$*" in
  */api/runtime*) printf '{"status":"ok","service":"frontend","buildId":"status-test-build"}' ;;
  */api/skin-photo*) printf '{"ready":true}' ;;
esac
''')
    curl.chmod(0o755)
    redis = bindir / "redis-cli"
    redis.write_text('#!/bin/sh\nprintf "PONG\\n"\n')
    redis.chmod(0o755)
    args_path = tmp_path / "curl-args"
    result = subprocess.run(
        ["/bin/bash", str(runtime / "status-production.sh")],
        env={**os.environ, "BLINK_RUNTIME_ROLE": "mac", "ENABLE_TAIDE": "1", "NEXT_DIST_DIR": ".next",
             "PATH": f"{bindir}:/usr/bin:/bin", "TEST_CURL_ARGS": str(args_path)},
        capture_output=True, text=True, check=True,
    )
    assert "DISABLED" in result.stdout
    requests = args_path.read_text()
    assert ":8000/" not in requests
    assert ":8001/" not in requests
    assert ":8010/readyz" in requests
    assert ":8002/healthz" in requests
    assert 'frontend build alignment' in result.stdout
    assert 'website photo analysis' in result.stdout
    assert '/api/runtime' in requests
    assert '/api/skin-photo' in requests

    # HTTP200 alone must not hide a stale running frontend build.
    (build / 'BUILD_ID').write_text('different-selected-build')
    stale = subprocess.run(
        ["/bin/bash", str(runtime / "status-production.sh")],
        env={**os.environ, "BLINK_RUNTIME_ROLE": "mac", "NEXT_DIST_DIR": ".next",
             "PATH": f"{bindir}:/usr/bin:/bin", "TEST_CURL_ARGS": str(args_path)},
        capture_output=True, text=True,
    )
    assert stale.returncode == 1
    line = next(line for line in stale.stdout.splitlines() if 'frontend build alignment' in line)
    assert line.rstrip().endswith('DOWN')


def test_frontend_build_rejects_output_path_escape_before_npm(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "ops/runtime"
    runtime.mkdir(parents=True)
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    script = runtime / "build-mac-frontend.sh"
    shutil.copyfile(root / "ops/runtime/build-mac-frontend.sh", script)
    for output in ("../outside", "/tmp/outside", ".next/../outside", ".next-x/y", ".next-"):
        result = subprocess.run(
            ["/bin/bash", str(script)], env={**os.environ, "NEXT_DIST_DIR": output},
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "NEXT_DIST_DIR" in result.stderr
    (frontend / ".next-escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    result = subprocess.run(
        ["/bin/bash", str(script)], env={**os.environ, "NEXT_DIST_DIR": ".next-escape"},
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "symlink" in result.stderr


def test_mac_start_validates_build_selected_by_canonical_env(tmp_path):
    import json

    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "ops/runtime"
    runtime.mkdir(parents=True)
    backend = tmp_path / "cosmetic-agent"
    backend.mkdir()
    frontend = tmp_path / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    dist = frontend / ".next-security-20261116"
    dist.mkdir()
    (dist / "BUILD_ID").write_text("isolated-test-build")
    (dist / "routes-manifest.json").write_text(json.dumps({"rewrites": [{
        "source": "/api/:path*", "destination": "http://127.0.0.1:8010/api/:path*",
    }]}))
    script = runtime / "start-mac-production.sh"
    shutil.copyfile(root / "ops/runtime/start-mac-production.sh", script)
    # Stop deliberately before Redis or any app process can start.
    redis = runtime / "start-mac-redis.sh"
    redis.write_text("#!/bin/sh\nexit 47\n")
    redis.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("uname", "printf 'Darwin\\n'"), ("python", "exit 0")):
        executable = bindir / name
        executable.write_text(f"#!/bin/sh\n{body}\n")
        executable.chmod(0o755)
    (backend / ".env").write_text(
        "NEXT_DIST_DIR=.next-security-20261116\n"
        f"BACKEND_PYTHON={shlex.quote(str(bindir / 'python'))}\n"
        f"MAC_RUNTIME_DIR={shlex.quote(str(tmp_path / 'runtime-data'))}\n"
    )
    result = subprocess.run(
        ["/bin/bash", str(script)],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "NEXT_DIST_DIR": "../ignored"},
        capture_output=True, text=True,
    )
    assert result.returncode == 47, result.stderr
    assert not (frontend / ".next").exists()


def test_detached_frontend_normalizes_and_revalidates_its_env(tmp_path):
    import json

    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "ops/runtime"
    runtime.mkdir(parents=True)
    backend = tmp_path / "cosmetic-agent"
    backend.mkdir()
    frontend = tmp_path / "frontend"
    next_bin = frontend / "node_modules/next/dist/bin"
    next_bin.mkdir(parents=True)
    (tmp_path / "runtime-data/pids").mkdir(parents=True)
    script = runtime / "run-mac-service.sh"
    shutil.copyfile(root / "ops/runtime/run-mac-service.sh", script)
    # A Node stub, not the real server; no ports or services are opened.
    (next_bin / "next").write_text(
        "console.log(JSON.stringify({next_dist_dir:process.env.NEXT_DIST_DIR}));\n"
    )
    env_file = backend / ".env"
    env_file.write_text("NEXT_DIST_DIR=\n")
    args = ["/bin/bash", str(script), "frontend", str(tmp_path / "runtime-data")]
    env = {**os.environ, "NEXT_DIST_DIR": ".next"}
    result = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["next_dist_dir"] == ".next"
    env_file.write_text("NEXT_DIST_DIR=../outside\n")
    invalid = subprocess.run(args, env=env, capture_output=True, text=True)
    assert invalid.returncode == 2
    assert "NEXT_DIST_DIR" in invalid.stderr
    (frontend / ".next-escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    env_file.write_text("NEXT_DIST_DIR=.next-escape\n")
    symlink = subprocess.run(args, env=env, capture_output=True, text=True)
    assert symlink.returncode == 2
    assert "symlink" in symlink.stderr


def test_build_uses_explicit_dist_then_canonical_fallback_without_exporting_secrets(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "ops/runtime"
    runtime.mkdir(parents=True)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "tsconfig.json").write_text("{}")
    backend = tmp_path / "cosmetic-agent"
    backend.mkdir()
    (backend / ".env").write_text(
        "NEXT_DIST_DIR=.next-canonical\nexport REVIEW_FAKE_SECRET=not-a-real-secret\n"
    )
    script = runtime / "build-mac-frontend.sh"
    shutil.copyfile(root / "ops/runtime/build-mac-frontend.sh", script)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    npm = bindir / "npm"
    npm.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$NEXT_DIST_DIR" "${REVIEW_FAKE_SECRET-unset}"\n')
    npm.chmod(0o755)
    node = bindir / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)
    for explicit, expected in ((None, ".next-canonical"), (".next-explicit", ".next-explicit"), ("", ".next")):
        env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"}
        env.pop("NEXT_DIST_DIR", None)
        env.pop("REVIEW_FAKE_SECRET", None)
        if explicit is not None:
            env["NEXT_DIST_DIR"] = explicit
        result = subprocess.run(["/bin/bash", str(script)], env=env,
                                capture_output=True, text=True, check=True)
        assert result.stdout.strip() == f"{expected}|unset"


def test_mi300x_status_does_not_require_retired_taide(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "ops/runtime"
    runtime.mkdir(parents=True)
    shutil.copyfile(root / "ops/runtime/status-production.sh", runtime / "status-production.sh")
    shutil.copyfile(root / "ops/runtime/runtime.conf", runtime / "runtime.conf")
    verify = runtime / "verify-mi300x-host.sh"
    verify.write_text("#!/bin/sh\nexit 0\n")
    verify.chmod(0o755)
    manager = runtime / "manage-bge-model.py"
    manager.write_text(
        "import sys\nassert sys.argv[1:] == ['status','--instance','bge-model']\n"
        "print('BGE_TEST_IDENTITY_READY')\n"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    control = bindir / "capable-control-python"
    control.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-I" ]; then\n'
        '  case "$3" in *os.pidfd_open*signal.pidfd_send_signal*) exit 0;; *) exit 90;; esac\n'
        'fi\n'
        'exec "$TEST_REAL_PYTHON" "$@"\n'
    )
    control.chmod(0o755)
    curl = bindir / "curl"
    curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$TEST_CURL_ARGS"\n')
    curl.chmod(0o755)
    curl_args = tmp_path / "curl-args"
    result = subprocess.run(
        ["/bin/bash", str(runtime / "status-production.sh")],
        env={**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "BLINK_RUNTIME_ROLE": "mi300x",
             "MI300X_CONTROL_PYTHON": str(control), "MI300X_BGE_CONTROLLER": str(manager),
             "TEST_REAL_PYTHON": sys.executable,
             "ENABLE_TAIDE": "0", "ENABLE_AB_MODEL": "0", "ENABLE_RERANKER": "0",
             "ENABLE_PHOTO_MODEL": "0", "TEST_CURL_ARGS": str(curl_args)},
        capture_output=True, text=True, check=True,
    )
    assert "DISABLED" in result.stdout
    requests = curl_args.read_text() if curl_args.exists() else ""
    assert ":8000/" not in requests
    assert ":8200/" not in requests
    assert "BGE_TEST_IDENTITY_READY" in result.stdout
    assert "managed embedding :8002" in result.stdout


def test_experience_parser_keeps_json_context_and_uses_bounded_responses(monkeypatch):
    import json
    from tools.agent import experience_parser

    adapter = terra_adapter(json.dumps({
        "is_experience": True, "product_name": "", "sentiment": "negative",
        "attributes": ["太黏"], "avoid_preferences": ["偏黏質地"],
        "preferred_features": [], "reply": "之後會避開偏黏質地。",
    }, ensure_ascii=False))
    monkeypatch.setattr(experience_parser, "client", adapter)
    monkeypatch.setattr(experience_parser, "TIMEOUT_SECONDS", 20)
    monkeypatch.setenv("TERRA_REASONING_EFFORT", "high")
    history = [{"role": "user", "content": str(n)} for n in range(10)]
    result = experience_parser.parse_product_experience(
        "用起來太黏", pending_product_name="Alpha乳霜",
        conversation_stage="waiting_experience_detail", session_history=history,
        memory_context="m" * 5100, last_product_names=[str(n) for n in range(8)],
        model="taide-12b",  # Legacy caller parameter must not change provider.
    )
    assert result["product_name"] == "Alpha乳霜"
    assert result["sentiment"] == "negative"
    assert result["attributes"] == ["太黏"]
    assert result["needs_experience_detail"] is False
    call = adapter.client.calls[0]
    assert call["model"] == experience_parser.MODEL_NAME
    assert call["max_output_tokens"] == 700
    assert call["text"]["format"] == {"type": "json_object"}
    assert call["reasoning"] == {"effort": "none"}
    assert call["store"] is False
    assert "response_format" not in call
    context = json.loads(call["input"][1]["content"])
    assert context["recent_conversation"] == history[-6:]
    assert len(context["related_memory"]) == 5000
    assert context["last_recommended_products"] == [str(n) for n in range(5)]
    assert adapter.client.options == [{"timeout": 20, "max_retries": 0}]


def test_experience_invalid_json_or_timeout_preserves_rule_fallback(monkeypatch):
    from tools.agent import experience_parser

    invalid = terra_adapter("This is not JSON")

    class Timeout:
        def __init__(self):
            self.calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            raise TimeoutError()

    timeout = Timeout()
    for client in (invalid, timeout):
        monkeypatch.setattr(experience_parser, "client", client)
        result = experience_parser.parse_product_experience(
            "用起來太黏", pending_product_name="Alpha乳霜",
            conversation_stage="waiting_experience_detail",
        )
        assert result["is_experience"] is True
        assert result["product_name"] == "Alpha乳霜"
        assert result["sentiment"] == "negative"
        assert "偏黏質地" in result["avoid_preferences"]
    assert len(invalid.client.calls) == 1
    assert timeout.calls == 1


def test_experience_must_not_invent_product_after_bare_used_before(monkeypatch):
    from tools.agent import experience_parser

    adapter = terra_adapter('{"is_experience":true,"product_name":"invented","sentiment":"unknown"}')
    monkeypatch.setattr(experience_parser, "client", adapter)
    result = experience_parser.parse_product_experience("我用過")
    assert result["product_name"] == ""
    assert result["needs_product_name"] is True

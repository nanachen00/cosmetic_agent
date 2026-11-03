"""Gate 8 compatibility gateway.

All ordinary requests are delegated to the captured original compatibility
gateway. Only the exact BLINK root-intent Responses/JSON-schema request may be
short-circuited by the qualified Gate7 Parser/Kev treatment.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
import uuid
from pathlib import Path

import compat_gateway_base as base
from common import event
from gate8_treatment import Treatment, TreatmentError, inspect_intent_request
import gate2_audit

# Preserve the captured compat_gateway module-level public API for the original
# harness tests/importers. Gate8 overrides only Handler/serve/main below.
# This avoids maintaining a hand-written partial export list that can silently
# drop reviewed symbols such as Server.
_GATE8_OVERRIDES = {"Handler", "serve", "main"}
for _name in dir(base):
    if _name.startswith("__") or _name in _GATE8_OVERRIDES:
        continue
    globals()[_name] = getattr(base, _name)

# Preserve original module-global monkeypatch semantics used by the captured
# harness tests. base.Handler executes in compat_gateway_base's globals, while
# tests patch attributes on this wrapper module. Before delegating to the
# original Handler, temporarily mirror any changed wrapper globals into base,
# then restore base immediately afterwards.
_GATE8_BASE_BRIDGE_NAMES = tuple(
    name for name in dir(base)
    if not name.startswith("__") and name not in _GATE8_OVERRIDES
)

@contextlib.contextmanager
def _bridge_base_globals():
    saved = {}
    try:
        for name in _GATE8_BASE_BRIDGE_NAMES:
            if name not in globals() or not hasattr(base, name):
                continue
            wrapper_value = globals()[name]
            base_value = getattr(base, name)
            if wrapper_value is not base_value:
                saved[name] = base_value
                setattr(base, name, wrapper_value)
        yield
    finally:
        for name, value in saved.items():
            setattr(base, name, value)




def _decision_event(server, kind: str, **fields):
    session = getattr(server, "gate2_decision_audit_session", None)
    path = getattr(server, "gate8_decision_audit", None)
    # Compatibility/unit fixtures use the historical in-memory event hook.
    if not session or path == "memory":
        return event(path, kind, **fields)
    return gate2_audit.append_gateway_event(
        Path(path), session_id=session, kind=kind, fields=fields
    )

def _public_decision(decision: dict) -> dict:
    allowed = {
        "arm", "query_sha256", "query_chars", "schema_sha256", "local_bypass",
        "upstream_called", "kev_called", "route", "source", "error",
        "decision_ms", "kev_ms", "error_code",
    }
    out = {k: decision.get(k) for k in allowed if k in decision}
    pred = decision.get("kev_prediction")
    if isinstance(pred, dict):
        out["kev_prediction"] = {
            "pred_route": pred.get("pred_route"),
            "confidence": pred.get("confidence"),
            "clarification_probability": pred.get("clarification_probability"),
            "safety_probability": pred.get("safety_probability"),
        }
    return out


class Handler(base.Handler):
    def _delegate_base_post(self):
        with _bridge_base_globals():
            return base.Handler.do_POST(self)

    def do_GET(self):
        with _bridge_base_globals():
            return base.Handler.do_GET(self)

    def do_POST(self):
        # The captured harness imports this Handler directly in several
        # compatibility/native-schema fixtures without constructing Gate8
        # treatment state. In that context the wrapper must be behaviorally
        # identical to the original compat_gateway Handler.
        if not hasattr(self.server, "gate8_treatment"):
            return self._delegate_base_post()

        if self.path.rstrip("/") != "/v1/responses":
            return self._delegate_base_post()

        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 4 * 1024 * 1024:
            return self._delegate_base_post()
        raw = self.rfile.read(length)
        self.rfile = io.BytesIO(raw)

        try:
            payload = json.loads(raw)
            info = inspect_intent_request(payload, self.path)
        except TreatmentError as exc:
            _decision_event(
                self.server,
                "gate8_intent_detection_error",
                request_id=self.headers.get("X-Request-ID", "")[:128],
                arm=self.server.gate8_arm,
                error=type(exc).__name__,
                fallback_to_original=True,
            )
            return self._delegate_base_post()
        except Exception:
            return self._delegate_base_post()

        if info is None:
            return self._delegate_base_post()

        rid = self.headers.get("X-Request-ID", "")[:128]
        decision = self.server.gate8_treatment.decide(info)
        _decision_event(
            self.server,
            "gate8_intent_decision",
            request_id=rid,
            provider_port=self.server.provider_port,
            **_public_decision(decision),
        )

        if not decision.get("local_bypass"):
            return self._delegate_base_post()

        start = time.perf_counter()
        call_id = "call-" + uuid.uuid4().hex
        wire = decision["wire"]
        text = json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
        chat = {
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        body = base.responses_envelope(chat)
        self.send(200, body)
        event(
            self.server.audit_log,
            "model_call",
            request_id=rid,
            gateway_call_id=call_id,
            provider_port=self.server.provider_port,
            proxy_port=self.server.proxy_port,
            status=200,
            error=None,
            duration_ms=(time.perf_counter() - start) * 1000,
            requested_model=info.get("requested_model"),
            actual_model="gate8-" + str(decision.get("source")),
            request_api="responses",
            schema_mode="gate8_local_intent_bypass",
            output_format_type="json_schema",
            schema_validation="pass",
            native_schema_requested=False,
            native_bound_schema_validation=None,
            classification_labels_pinned=False,
            response_repaired=False,
            finish_reason="stop",
            usage=chat["usage"],
            gate8_intent_bypass=True,
            gate8_source=decision.get("source"),
            gate8_route=decision.get("route"),
            gate8_kev_called=bool(decision.get("kev_called")),
            provider_http_called=False,
        )


def serve(port, proxy_port, provider_port, local_key, audit_log, timeout,
          schema_mode="prompt_only", intent_output_token_floor=None, gate8=None):
    # Preserve the captured compat_gateway.serve contract exactly when this
    # module is used as a normal harness replacement. Gate8 behavior is opt-in
    # only when the launched spec explicitly supplies a gate8 object.
    if gate8 is None:
        with _bridge_base_globals():
            return base.serve(
                port, proxy_port, provider_port, local_key, audit_log, timeout,
                schema_mode, intent_output_token_floor,
            )

    from generation_budget import validate_floor

    validate_floor(intent_output_token_floor)
    if intent_output_token_floor is not None and (
        provider_port not in {8000, 8200}
        or schema_mode not in {"legacy_schema_fields", "vllm_native_raw_query"}
    ):
        raise base.Unsupported("Intent budget is restricted to the reviewed paired Shadow treatments")
    if schema_mode not in {"prompt_only", "vllm_native_raw_query", "legacy_explicit_json", "legacy_schema_fields"}:
        raise base.Unsupported("Unknown schema experiment mode")
    if schema_mode == "vllm_native_raw_query" and provider_port in {8000, 8001, 8002, 8003, 8010, 6379}:
        raise base.Unsupported("Native schema experiment cannot target a legacy/production endpoint")

    gate8 = gate8 or {}
    treatment = Treatment(
        arm=gate8["arm"],
        parser_path=Path(gate8["parser_file"]),
        policy_path=Path(gate8["policy_file"]),
        questions_path=Path(gate8["questions_file"]),
        kev_url=gate8.get("kev_url"),
        kev_timeout=float(gate8.get("kev_timeout_seconds", 10.0)),
    )

    with base.Server(("127.0.0.1", port), Handler) as server:
        server.schema_mode = schema_mode
        server.intent_output_token_floor = intent_output_token_floor
        server.upstream = f"http://127.0.0.1:{proxy_port}"
        server.provider_port = provider_port
        server.proxy_port = proxy_port
        server.local_key = local_key
        server.audit_log = audit_log
        server.request_timeout = timeout
        server.gate8_arm = gate8["arm"]
        server.gate8_treatment = treatment
        server.gate8_decision_audit = gate8["decision_audit"]
        server.gate2_decision_audit_session = gate8.get("decision_audit_session")
        server.serve_forever(poll_interval=0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    args = ap.parse_args()
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    serve(**spec)


if __name__ == "__main__":
    main()

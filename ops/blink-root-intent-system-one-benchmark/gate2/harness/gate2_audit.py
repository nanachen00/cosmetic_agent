from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from common import utc

SCHEMA = "gate2-cell-audit-v1"
ZERO = "0" * 64


def _canon(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(obj: dict) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_locked(stream) -> list[dict]:
    stream.seek(0)
    raw = stream.read()
    if not raw:
        return []
    if not raw.endswith("\n"):
        raise ValueError("audit_truncated_final_line")
    rows = []
    for index, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise ValueError(f"audit_blank_line:{index}")
        try:
            item = json.loads(line)
        except Exception as exc:
            raise ValueError(f"audit_malformed_json:{index}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"audit_non_object:{index}")
        rows.append(item)
    return rows


def _append(path: Path, session_id: str, kind: str, fields: dict[str, Any], *, create: bool = False) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("audit_symlink_refused")
    if create and path.exists():
        raise ValueError("audit_already_exists")
    mode = "x+" if create else "r+"
    with path.open(mode, encoding="utf-8") as f:
        os.chmod(path, 0o600)
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        rows = _load_locked(f)
        if create and rows:
            raise ValueError("audit_not_empty_on_open")
        if not create and not rows:
            raise ValueError("audit_missing_open")
        if rows:
            prior = rows[-1]
            if prior.get("event") == "gate2_audit_close":
                raise ValueError("audit_already_closed")
            if prior.get("session_id") != session_id:
                raise ValueError("audit_session_changed")
            seq = int(prior.get("seq", -1)) + 1
            prev = str(prior.get("event_sha256") or "")
        else:
            seq = 0
            prev = ZERO
        row = {
            "schema": SCHEMA,
            "timestamp": utc(),
            "event": kind,
            "session_id": session_id,
            "seq": seq,
            "prev_sha256": prev,
            **fields,
        }
        material = dict(row)
        row["event_sha256"] = _digest(material)
        f.seek(0, os.SEEK_END)
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return row


def open_cell(path: Path, *, session_id: str, arm: str, phase: str, scenario: str, provider_port: int) -> dict:
    return _append(
        path,
        session_id,
        "gate2_audit_open",
        {
            "arm": arm,
            "phase": phase,
            "scenario": scenario,
            "provider_port": int(provider_port),
            "gateway_owned": True,
        },
        create=True,
    )


def append_gateway_event(path: Path, *, session_id: str, kind: str, fields: dict[str, Any]) -> dict:
    return _append(path, session_id, kind, dict(fields), create=False)


def close_cell(path: Path, *, session_id: str, arm: str, phase: str, scenario: str, provider_port: int) -> dict:
    return _append(
        path,
        session_id,
        "gate2_audit_close",
        {
            "arm": arm,
            "phase": phase,
            "scenario": scenario,
            "provider_port": int(provider_port),
            "gateway_owned_stopped": True,
            "no_more_gateway_events_possible": True,
        },
        create=False,
    )


def replay(path: Path, *, arm: str | None = None, phase: str | None = None,
           scenario: str | None = None, provider_port: int | None = None) -> dict:
    path = Path(path)
    result = {
        "schema": SCHEMA,
        "path": str(path),
        "complete": False,
        "blockers": [],
        "record_count": 0,
        "decision_count": 0,
        "kev_attempt_count": 0,
        "treatment_error_count": 0,
        "events": [],
    }
    if not path.exists():
        result["blockers"].append("audit_missing")
        return result
    if path.is_symlink() or not path.is_file():
        result["blockers"].append("audit_not_regular_file")
        return result
    try:
        raw = path.read_bytes()
    except Exception:
        result["blockers"].append("audit_unreadable")
        return result
    result["bytes"] = len(raw)
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    if not raw:
        result["blockers"].append("audit_empty")
        return result
    if not raw.endswith(b"\n"):
        result["blockers"].append("audit_truncated_final_line")
        return result
    rows = []
    for index, line in enumerate(raw.splitlines(), 1):
        if not line:
            result["blockers"].append(f"audit_blank_line:{index}")
            return result
        try:
            item = json.loads(line)
        except Exception:
            result["blockers"].append(f"audit_malformed_json:{index}")
            return result
        if not isinstance(item, dict):
            result["blockers"].append(f"audit_non_object:{index}")
            return result
        rows.append(item)
    result["record_count"] = len(rows)
    result["events"] = rows
    session = rows[0].get("session_id") if rows else None
    prev = ZERO
    for index, row in enumerate(rows):
        if row.get("schema") != SCHEMA:
            result["blockers"].append(f"audit_schema_mismatch:{index}")
            break
        if row.get("session_id") != session or not session:
            result["blockers"].append(f"audit_session_mismatch:{index}")
            break
        if row.get("seq") != index:
            result["blockers"].append(f"audit_sequence_mismatch:{index}")
            break
        if row.get("prev_sha256") != prev:
            result["blockers"].append(f"audit_prev_hash_mismatch:{index}")
            break
        supplied = row.get("event_sha256")
        material = dict(row)
        material.pop("event_sha256", None)
        computed = _digest(material)
        if supplied != computed:
            result["blockers"].append(f"audit_event_hash_mismatch:{index}")
            break
        prev = computed
    if not result["blockers"]:
        if rows[0].get("event") != "gate2_audit_open":
            result["blockers"].append("audit_open_missing")
        if rows[-1].get("event") != "gate2_audit_close":
            result["blockers"].append("audit_close_missing")
        if sum(r.get("event") == "gate2_audit_open" for r in rows) != 1:
            result["blockers"].append("audit_open_count_invalid")
        if sum(r.get("event") == "gate2_audit_close" for r in rows) != 1:
            result["blockers"].append("audit_close_count_invalid")
        close = rows[-1]
        if close.get("gateway_owned_stopped") is not True or close.get("no_more_gateway_events_possible") is not True:
            result["blockers"].append("audit_close_not_sealed_after_gateway_stop")
        expected = {
            "arm": arm,
            "phase": phase,
            "scenario": scenario,
            "provider_port": provider_port,
        }
        for key, value in expected.items():
            if value is None:
                continue
            for endpoint in (rows[0], rows[-1]):
                actual = endpoint.get(key)
                if key == "provider_port":
                    try:
                        actual = int(actual)
                    except Exception:
                        pass
                if actual != value:
                    result["blockers"].append(f"audit_context_mismatch:{key}")
                    break
    decisions = [r for r in rows if r.get("event") == "gate8_intent_decision"]
    result["decision_count"] = len(decisions)
    result["kev_attempt_count"] = sum(r.get("kev_called") is True for r in decisions)
    result["treatment_error_count"] = sum(
        r.get("event") == "gate8_intent_detection_error" or bool(r.get("error"))
        for r in rows
    )
    result["session_id"] = session
    result["complete"] = not result["blockers"]
    return result


def _strict_jsonl(path: Path) -> dict:
    path = Path(path)
    out = {"path": str(path), "complete": False, "blockers": [], "rows": []}
    if not path.exists():
        out["blockers"].append("missing")
        return out
    if path.is_symlink() or not path.is_file():
        out["blockers"].append("not_regular_file")
        return out
    raw = path.read_bytes()
    out["bytes"] = len(raw)
    out["sha256"] = hashlib.sha256(raw).hexdigest()
    if raw and not raw.endswith(b"\n"):
        out["blockers"].append("truncated_final_line")
        return out
    rows = []
    for index, line in enumerate(raw.splitlines(), 1):
        if not line:
            out["blockers"].append(f"blank_line:{index}")
            return out
        try:
            row = json.loads(line)
        except Exception:
            out["blockers"].append(f"malformed_json:{index}")
            return out
        if not isinstance(row, dict):
            out["blockers"].append(f"non_object:{index}")
            return out
        rows.append(row)
    out["rows"] = rows
    out["record_count"] = len(rows)
    out["complete"] = not out["blockers"]
    return out


def request_proof(run_dir: Path, row: dict, arm: str) -> dict:
    run_dir = Path(run_dir)
    rid = str(row.get("request_id") or "")
    scenario = str(row.get("scenario") or "")
    try:
        provider = int(row.get("provider_port"))
    except Exception:
        provider = -1
    trace = run_dir / f"provider-{provider}" / scenario / "trace"
    decision_path = trace / "gate8-intent-decisions.jsonl"
    audit = replay(decision_path, arm=arm, phase="smoke", scenario=scenario, provider_port=provider)
    model = _strict_jsonl(trace / "model-calls.jsonl")
    decisions_for_request = [
        r for r in audit.get("events", [])
        if r.get("event") == "gate8_intent_decision" and str(r.get("request_id") or "") == rid
    ]
    detection_errors_for_request = [
        r for r in audit.get("events", [])
        if r.get("event") == "gate8_intent_detection_error" and str(r.get("request_id") or "") == rid
    ]
    model_for_request = [
        r for r in model.get("rows", [])
        if r.get("event") == "model_call" and str(r.get("request_id") or "") == rid
    ]
    blockers = []
    if not rid:
        blockers.append("request_id_missing")
    if not audit.get("complete"):
        blockers.extend("decision_" + x for x in audit.get("blockers", []))
    if not model.get("complete"):
        blockers.extend("model_" + x for x in model.get("blockers", []))
    checks = {}
    try:
        checks = json.loads(row.get("checks") or "{}")
    except Exception:
        blockers.append("row_checks_unparseable")
    if checks.get("model_call_observed") is True and not model_for_request:
        blockers.append("model_call_witness_missing")
    proof = {
        "schema": "gate2-request-audit-proof-v1",
        "arm": arm,
        "phase": "smoke",
        "scenario": scenario,
        "provider_port": provider,
        "request_id": rid,
        "audit_complete": not blockers,
        "blockers": blockers,
        "decision_audit": {
            "path": str(decision_path.relative_to(run_dir)) if decision_path.is_relative_to(run_dir) else str(decision_path),
            "sha256": audit.get("sha256"),
            "bytes": audit.get("bytes"),
            "record_count": audit.get("record_count"),
            "session_id": audit.get("session_id"),
        },
        "model_audit": {
            "path": str((trace / "model-calls.jsonl").relative_to(run_dir)) if (trace / "model-calls.jsonl").is_relative_to(run_dir) else str(trace / "model-calls.jsonl"),
            "sha256": model.get("sha256"),
            "bytes": model.get("bytes"),
            "record_count": model.get("record_count"),
            "request_record_count": len(model_for_request),
        },
        "decision_count": len(decisions_for_request),
        "kev_attempt_count": sum(r.get("kev_called") is True for r in decisions_for_request),
        "treatment_error_count": len(detection_errors_for_request) + sum(bool(r.get("error")) for r in decisions_for_request),
    }
    material = dict(proof)
    proof["proof_sha256"] = _digest(material)
    return proof


def verify_stored_proof(run_dir: Path, row: dict, arm: str, stored: dict) -> dict:
    current = request_proof(run_dir, row, arm)
    return {
        "pass": current == stored,
        "current": current,
        "stored": stored,
        "blockers": [] if current == stored else ["stored_request_audit_proof_mismatch"],
    }

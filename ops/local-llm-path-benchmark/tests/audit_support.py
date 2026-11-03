"""Test-only reader for concurrently appended audit JSONL.

An LF-terminated line is a complete frame; a trailing byte fragment is pending.
Never use str.splitlines(): Unicode separators can occur inside JSON strings.
This does not repair JSON, skip corrupt records, retry HTTP or change a verdict.
It is NOT imported by the production/Shadow benchmark runtime.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
import time

class AuditFormatError(AssertionError):
    """A completed audit record is corrupt (payload is not printed)."""

class AuditWaitTimeout(AssertionError):
    """No complete, unique expected event was available by the deadline."""

MAX_AUDIT_BYTES = 4 * 1024 * 1024


def parse_audit_prefix(data: bytes) -> tuple[list[dict], bytes]:
    """Parse all LF-terminated frames, returning an untouched trailing fragment.

    A malformed *complete* record is an immediate failure even when another
    record already matches. Only an unterminated final frame is deferred.
    """
    if not isinstance(data, bytes):
        raise TypeError('audit snapshot must be bytes')
    complete, sep, pending = data.rpartition(b'\n')
    if not sep:
        return [], data
    rows = []
    for number, line in enumerate(complete.split(b'\n'), 1):
        if not line.strip():
            raise AuditFormatError(f'Blank complete audit record at line {number}')
        try:
            value = json.loads(line.decode('utf-8'))
        except UnicodeDecodeError:
            raise AuditFormatError(f'Invalid UTF-8 in complete audit record at line {number}') from None
        except json.JSONDecodeError as exc:
            raise AuditFormatError(
                f'Malformed complete audit JSON at line {number}, column {exc.colno}, offset {exc.pos}'
            ) from None
        if not isinstance(value, dict):
            raise AuditFormatError(f'Audit record at line {number} must be a JSON object')
        rows.append(value)
    return rows, pending


def read_audit_snapshot(path: Path) -> bytes:
    """One bounded read: no separate size/newline check followed by a reread."""
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(MAX_AUDIT_BYTES + 1)
    except FileNotFoundError:
        return b''
    if len(data) > MAX_AUDIT_BYTES:
        raise AuditFormatError('Audit fixture exceeds bounded read size')
    return data


def wait_for_audit_rows(path: Path | str, *, request_id: str,
                        event: str = 'model_call', timeout: float = 3.0,
                        poll_interval: float = 0.01) -> list[dict]:
    """Wait for exactly one complete matching event with no pending tail.

    Only file observation is repeated. No network request is retried. A timeout
    (including a valid JSON value with no terminating LF) is a test failure.
    Extra unrelated records remain visible to the caller; duplicate matching
    records are an error. An empty request_id is allowed for a proxy that has
    explicitly recorded an empty ID; a missing ID is not treated as empty.
    """
    if not isinstance(request_id, str) or not isinstance(event, str) or not event:
        raise ValueError('Expected a string request_id and a nonempty event')
    for name, value in (('timeout', timeout), ('poll_interval', poll_interval)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(name + ' must be finite and positive')
    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        data = read_audit_snapshot(Path(path))
        polls += 1
        rows, pending = parse_audit_prefix(data)
        matches = [row for row in rows
                   if row.get('event') == event and row.get('request_id') == request_id]
        if len(matches) > 1:
            raise AuditFormatError('Duplicate complete events for the expected audit request')
        now = time.monotonic()
        if len(matches) == 1 and not pending and now <= deadline:
            return rows
        remaining = deadline - now
        if remaining <= 0:
            raise AuditWaitTimeout(
                f'Audit wait timed out: polls={polls}, complete_records={len(rows)}, '
                f'matching_records={len(matches)}, trailing_bytes={len(pending)}; '
                'no incomplete record was treated as valid'
            )
        time.sleep(min(poll_interval, remaining))

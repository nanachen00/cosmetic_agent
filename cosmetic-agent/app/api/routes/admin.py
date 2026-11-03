"""Protected administration API for BLINK operations and user resources.

All expensive work is isolated from chat handlers. GPU and database snapshots are
collected only when an administrator opens or refreshes the admin dashboard.
"""

from __future__ import annotations

import hashlib
import json
def _resolve_process_name(pid: int | None, amd_name: str) -> str:
    if amd_name and amd_name not in {"N/A", "unknown"}:
        return amd_name
    if pid is not None:
        for candidate in (Path(f"/proc/{pid}/comm"), Path(f"/proc/{pid}/exe")):
            try:
                value = candidate.read_text(encoding="utf-8").strip() if candidate.name == "comm" else candidate.resolve().name
                if value:
                    return value
            except (OSError, UnicodeError):
                continue
    return "主機 GPU 程序"


import os
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core.observability import log_event
from database.postgres.connection import postgres_connection
from app.services.product_review_service import (
    REVIEW_STATUSES,
    ProductImportError,
    list_product_reviews,
    product_review_stats,
    update_product_review,
)


def _configured_admin_user_ids() -> set[str]:
    return {
        value.strip()
        for value in os.getenv("ADMIN_USER_IDS", "").split(",")
        if value.strip()
    }


def _safe_dashboard_url(env_name: str) -> str | None:
    value = os.getenv(env_name, "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return value


def _hash_user_id(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _metric_number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _metric_number(value)
        if number is not None:
            return number
    return None


def _gpu_processes(executable: str) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            [executable, "process", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=4,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    processes: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            process_info = value.get("process_info")
            if isinstance(process_info, dict):
                memory_usage = process_info.get("memory_usage")
                if not isinstance(memory_usage, dict):
                    memory_usage = {}
                vram = memory_usage.get("vram_mem")
                vram_bytes = _metric_number(vram)
                name = str(process_info.get("name") or "").strip()
                pid_number = _metric_number(process_info.get("pid"))
                processes.append(
                    {
                        "pid": int(pid_number) if pid_number is not None else None,
                        "name": name if name and name != "N/A" else "主機 GPU 程序",
                        "vram_mb": round(vram_bytes / 1024 / 1024, 1) if vram_bytes is not None else None,
                        "namespace_limited": not name or name == "N/A",
                    }
                )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    deduplicated: dict[tuple[Any, Any], dict[str, Any]] = {}
    for process in processes:
        deduplicated[(process.get("pid"), process.get("vram_mb"))] = process
    return sorted(
        deduplicated.values(),
        key=lambda process: float(process.get("vram_mb") or 0),
        reverse=True,
    )


def _gpu_snapshot() -> dict[str, Any]:
    executable = next(
        (
            str(path)
            for path in (Path("/opt/rocm/bin/amd-smi"), Path("/usr/bin/amd-smi"))
            if path.exists()
        ),
        None,
    )
    if not executable:
        return {"available": False, "source": "amd-smi", "devices": []}

    try:
        result = subprocess.run(
            [executable, "metric", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=6,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {"available": False, "source": "amd-smi", "devices": []}

    devices: list[dict[str, Any]] = []
    for index, item in enumerate(payload if isinstance(payload, list) else []):
        if not isinstance(item, dict):
            continue
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        power = item.get("power") if isinstance(item.get("power"), dict) else {}
        temperature = item.get("temperature") if isinstance(item.get("temperature"), dict) else {}
        memory = item.get("mem_usage") if isinstance(item.get("mem_usage"), dict) else {}
        ecc = item.get("ecc") if isinstance(item.get("ecc"), dict) else {}

        total_vram = _metric_number(memory.get("total_vram"))
        used_vram = _metric_number(memory.get("used_vram"))
        vram_percent = None
        if total_vram and used_vram is not None:
            vram_percent = round((used_vram / total_vram) * 100, 1)

        devices.append(
            {
                "id": str(item.get("gpu", index)),
                "utilization_percent": _metric_number(usage.get("gfx_activity")),
                "vram_used_mb": used_vram,
                "vram_total_mb": total_vram,
                "vram_percent": vram_percent,
                "temperature_c": _first_number(
                    temperature.get("hotspot"),
                    temperature.get("edge"),
                    temperature.get("mem"),
                ),
                "memory_temperature_c": _metric_number(temperature.get("mem")),
                "power_w": _metric_number(power.get("socket_power")),
                "throttle_status": power.get("throttle_status") or "N/A",
                "ecc_correctable": int(_metric_number(ecc.get("total_correctable_count")) or 0),
                "ecc_uncorrectable": int(_metric_number(ecc.get("total_uncorrectable_count")) or 0),
            }
        )

    processes = _gpu_processes(executable)
    for process in processes:
        process["name"] = _resolve_process_name(process.get("pid"), str(process.get("name") or ""))
    return {
        "available": bool(devices),
        "source": "amd-smi",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "devices": devices,
        "processes": processes,
        "process_names_available": any(not item.get("namespace_limited") for item in processes),
    }


_GPU_SAMPLE_INTERVAL_SECONDS = 5
_GPU_RETENTION_DAYS = 7
_GPU_WINDOWS: dict[str, tuple[int, int]] = {
    "15m": (15 * 60, 5),
    "1h": (60 * 60, 5),
    "6h": (6 * 60 * 60, 30),
    "24h": (24 * 60 * 60, 120),
}
_gpu_sampler_lock = threading.Lock()
_gpu_sampler_started = False


def _ensure_gpu_history_schema() -> None:
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_gpu_samples (
                    id BIGSERIAL PRIMARY KEY,
                    sampled_at TIMESTAMPTZ NOT NULL,
                    device_id TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    utilization_percent DOUBLE PRECISION,
                    vram_percent DOUBLE PRECISION,
                    vram_used_mb DOUBLE PRECISION,
                    vram_total_mb DOUBLE PRECISION,
                    temperature_c DOUBLE PRECISION,
                    power_w DOUBLE PRECISION,
                    processes JSONB NOT NULL DEFAULT '[]'::jsonb
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_admin_gpu_samples_device_time ON admin_gpu_samples (device_id, sampled_at DESC)"
            )
        connection.commit()


def _record_gpu_snapshot(snapshot: dict[str, Any]) -> None:
    if not snapshot.get("available"):
        return
    sampled_at = snapshot.get("collected_at") or datetime.now(timezone.utc).isoformat()
    processes = snapshot.get("processes") if isinstance(snapshot.get("processes"), list) else []
    devices = snapshot.get("devices") if isinstance(snapshot.get("devices"), list) else []
    if not devices:
        return
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            for device in devices:
                device_id = str(device.get("id", "0"))
                cursor.execute(
                    """
                    INSERT INTO admin_gpu_samples (
                        sampled_at, device_id, device_name, utilization_percent, vram_percent,
                        vram_used_mb, vram_total_mb, temperature_c, power_w, processes
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        sampled_at, device_id, f"AMD GPU {device_id}",
                        device.get("utilization_percent"), device.get("vram_percent"),
                        device.get("vram_used_mb"), device.get("vram_total_mb"),
                        device.get("temperature_c"), device.get("power_w"),
                        json.dumps(processes, ensure_ascii=False),
                    ),
                )
        connection.commit()


def _gpu_history(window: str) -> dict[str, Any]:
    window_seconds, bucket_seconds = _GPU_WINDOWS[window]
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH ranked AS (
                    SELECT sampled_at, device_id, device_name, utilization_percent, vram_percent,
                           vram_used_mb, vram_total_mb, temperature_c, power_w, processes,
                           ROW_NUMBER() OVER (
                               PARTITION BY device_id, FLOOR(EXTRACT(EPOCH FROM sampled_at) / %s)
                               ORDER BY sampled_at DESC
                           ) AS bucket_rank
                    FROM admin_gpu_samples
                    WHERE sampled_at >= now() - (%s * interval '1 second')
                )
                SELECT sampled_at, device_id, device_name, utilization_percent, vram_percent,
                       vram_used_mb, vram_total_mb, temperature_c, power_w, processes
                FROM ranked
                WHERE bucket_rank = 1
                ORDER BY sampled_at ASC
                """,
                (bucket_seconds, window_seconds),
            )
            rows = [dict(row) for row in cursor.fetchall()]

    points: list[dict[str, Any]] = []
    process_summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_processes = row.get("processes")
        if isinstance(raw_processes, str):
            try:
                raw_processes = json.loads(raw_processes)
            except json.JSONDecodeError:
                raw_processes = []
        processes = raw_processes if isinstance(raw_processes, list) else []
        timestamp = row.get("sampled_at")
        timestamp_text = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
        points.append(
            {
                "timestamp": timestamp_text,
                "device_id": str(row.get("device_id") or "0"),
                "device_name": row.get("device_name") or "AMD GPU",
                "utilization_percent": _metric_number(row.get("utilization_percent")),
                "vram_percent": _metric_number(row.get("vram_percent")),
                "vram_used_mb": _metric_number(row.get("vram_used_mb")),
                "vram_total_mb": _metric_number(row.get("vram_total_mb")),
                "temperature_c": _metric_number(row.get("temperature_c")),
                "power_w": _metric_number(row.get("power_w")),
                "processes": processes,
            }
        )
        for process in processes:
            if not isinstance(process, dict):
                continue
            key = str(process.get("pid") or process.get("name") or "unknown")
            current_vram = _metric_number(process.get("vram_mb")) or 0.0
            item = process_summary.setdefault(
                key,
                {
                    "pid": process.get("pid"),
                    "name": process.get("name") or "未知程序",
                    "first_seen": timestamp_text,
                    "last_seen": timestamp_text,
                    "latest_vram_mb": current_vram,
                    "max_vram_mb": current_vram,
                    "namespace_limited": bool(process.get("namespace_limited")),
                },
            )
            item["last_seen"] = timestamp_text
            item["latest_vram_mb"] = current_vram
            item["max_vram_mb"] = max(float(item.get("max_vram_mb") or 0), current_vram)

    return {
        "window": window,
        "sample_interval_seconds": _GPU_SAMPLE_INTERVAL_SECONDS,
        "retention_days": _GPU_RETENTION_DAYS,
        "points": points,
        "processes": sorted(
            process_summary.values(),
            key=lambda item: float(item.get("latest_vram_mb") or 0),
            reverse=True,
        ),
    }


def _gpu_sampler_loop() -> None:
    failures = 0
    cleanup_counter = 0
    while True:
        time.sleep(_GPU_SAMPLE_INTERVAL_SECONDS)
        try:
            _record_gpu_snapshot(_gpu_snapshot())
            failures = 0
            cleanup_counter += 1
            if cleanup_counter >= 720:
                with postgres_connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM admin_gpu_samples WHERE sampled_at < now() - (%s * interval '1 day')",
                            (_GPU_RETENTION_DAYS,),
                        )
                    connection.commit()
                cleanup_counter = 0
        except Exception as exc:
            failures += 1
            if failures == 1 or failures % 60 == 0:
                log_event(
                    event="admin_gpu_sampler_failed",
                    message="GPU history sampler failed",
                    status="error",
                    error_type=type(exc).__name__,
                )


def _ensure_gpu_sampler(initial_snapshot: dict[str, Any]) -> None:
    global _gpu_sampler_started
    with _gpu_sampler_lock:
        if _gpu_sampler_started:
            return
        _ensure_gpu_history_schema()
        _record_gpu_snapshot(initial_snapshot)
        threading.Thread(target=_gpu_sampler_loop, name="blink-gpu-sampler", daemon=True).start()
        _gpu_sampler_started = True


def _platform_snapshot() -> dict[str, Any]:
    """Return decision-oriented product analytics from first-party PostgreSQL data."""

    def percent(part: int, total: int) -> float:
        return round((part / total) * 100, 1) if total else 0.0

    try:
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE status = 'active') AS login_enabled,
                        COUNT(*) FILTER (WHERE status = 'pending') AS pending_activation,
                        COUNT(*) FILTER (WHERE status = 'suspended') AS suspended,
                        COUNT(*) FILTER (WHERE created_at >= now() - interval '7 days') AS new_last_7_days,
                        COUNT(*) FILTER (
                            WHERE created_at >= now() - interval '14 days'
                              AND created_at < now() - interval '7 days'
                        ) AS new_previous_7_days
                    FROM users
                    WHERE status <> 'deleted'
                    """
                )
                accounts = dict(cursor.fetchone())

                cursor.execute(
                    """
                    WITH activity_events AS (
                        SELECT user_id, last_message_at AS event_at, 'ai'::text AS event_type
                        FROM conversations
                        UNION ALL
                        SELECT user_id, checkin_date::timestamptz, 'checkin'::text
                        FROM daily_checkins
                        UNION ALL
                        SELECT user_id, COALESCE(completed_at, started_at), 'quiz'::text
                        FROM quiz_attempts
                        UNION ALL
                        SELECT user_id, created_at, 'skin'::text
                        FROM skin_records
                    ), member_activity AS (
                        SELECT
                            u.id,
                            COUNT(*) FILTER (WHERE ae.event_at >= now() - interval '30 days') AS events_30d,
                            COUNT(DISTINCT ae.event_at::date) FILTER (WHERE ae.event_at >= now() - interval '30 days') AS active_days_30d,
                            COUNT(*) FILTER (WHERE ae.event_at >= now() - interval '7 days') AS events_7d,
                            COUNT(*) FILTER (
                                WHERE ae.event_type = 'ai'
                                  AND ae.event_at >= now() - interval '30 days'
                            ) AS ai_events_30d,
                            COUNT(DISTINCT ae.event_at::date) FILTER (
                                WHERE ae.event_type = 'checkin'
                                  AND ae.event_at >= now() - interval '30 days'
                            ) AS checkin_days_30d,
                            COUNT(*) FILTER (
                                WHERE ae.event_type = 'checkin'
                                  AND ae.event_at >= now() - interval '7 days'
                            ) AS checkins_7d
                        FROM users u
                        LEFT JOIN activity_events ae ON ae.user_id = u.id
                        WHERE u.status = 'active'
                        GROUP BY u.id
                    )
                    SELECT
                        COUNT(*) FILTER (WHERE events_30d > 0) AS active_30d,
                        COUNT(*) FILTER (WHERE events_30d = 0) AS inactive_30d,
                        COUNT(*) FILTER (WHERE events_7d > 0) AS active_7d,
                        COUNT(*) FILTER (WHERE active_days_30d >= 2) AS returning_30d,
                        COUNT(*) FILTER (WHERE ai_events_30d > 0) AS ai_users_30d,
                        COUNT(*) FILTER (WHERE checkins_7d > 0) AS checkin_users_7d
                    FROM member_activity
                    """
                )
                engagement = dict(cursor.fetchone())

                cursor.execute(
                    """
                    SELECT created_at::date AS day, COUNT(*) AS count
                    FROM users
                    WHERE status <> 'deleted'
                      AND created_at >= current_date - interval '6 days'
                    GROUP BY created_at::date
                    ORDER BY day
                    """
                )
                signup_rows = cursor.fetchall()

                cursor.execute(
                    """
                    WITH dated AS (
                        SELECT DISTINCT user_id, checkin_date
                        FROM daily_checkins
                    ), ranked AS (
                        SELECT
                            user_id,
                            checkin_date,
                            checkin_date + (ROW_NUMBER() OVER (
                                PARTITION BY user_id ORDER BY checkin_date DESC
                            ))::int AS island_key
                        FROM dated
                    ), islands AS (
                        SELECT user_id, island_key, COUNT(*)::int AS streak, MAX(checkin_date) AS last_date
                        FROM ranked
                        GROUP BY user_id, island_key
                    ), latest AS (
                        SELECT DISTINCT ON (user_id)
                            user_id,
                            CASE WHEN last_date >= current_date - 1 THEN streak ELSE 0 END AS streak
                        FROM islands
                        ORDER BY user_id, last_date DESC
                    ), eligible AS (
                        SELECT u.id, COALESCE(latest.streak, 0)::int AS streak
                        FROM users u
                        LEFT JOIN latest ON latest.user_id = u.id
                        WHERE u.status = 'active'
                    )
                    SELECT
                        CASE
                            WHEN streak = 0 THEN 'none'
                            WHEN streak <= 2 THEN 'days_1_2'
                            WHEN streak <= 6 THEN 'days_3_6'
                            WHEN streak <= 13 THEN 'days_7_13'
                            ELSE 'days_14_plus'
                        END AS key,
                        COUNT(*) AS count
                    FROM eligible
                    GROUP BY 1
                    """
                )
                streak_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(result_data->>'primary_type', ''), 'unclassified') AS key,
                        COUNT(*) AS count
                    FROM quiz_attempts
                    WHERE status = 'completed' AND quiz_key = 'customer_type'
                    GROUP BY 1
                    ORDER BY count(*) DESC
                    """
                )
                personality_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT
                        LOWER(BTRIM(concern)) AS key,
                        COUNT(DISTINCT user_id) AS count,
                        COUNT(*) AS records
                    FROM skin_records
                    WHERE concern IS NOT NULL AND BTRIM(concern) <> ''
                    GROUP BY 1
                    ORDER BY count DESC, records DESC
                    LIMIT 8
                    """
                )
                concern_rows = cursor.fetchall()

                cursor.execute(
                    """
                    WITH activity_events AS (
                        SELECT user_id, last_message_at AS event_at FROM conversations
                        UNION ALL SELECT user_id, checkin_date::timestamptz FROM daily_checkins
                        UNION ALL SELECT user_id, COALESCE(completed_at, started_at) FROM quiz_attempts
                        UNION ALL SELECT user_id, created_at FROM skin_records
                    ), features AS (
                        SELECT
                            u.id,
                            u.created_at,
                            COUNT(*) FILTER (WHERE ae.event_at >= now() - interval '30 days') AS events_30d,
                            COUNT(DISTINCT ae.event_at::date) FILTER (WHERE ae.event_at >= now() - interval '30 days') AS active_days_30d
                        FROM users u
                        LEFT JOIN activity_events ae ON ae.user_id = u.id
                        WHERE u.status = 'active'
                        GROUP BY u.id, u.created_at
                    )
                    SELECT
                        CASE
                            WHEN active_days_30d >= 7 THEN 'habit'
                            WHEN active_days_30d BETWEEN 2 AND 6 THEN 'growing'
                            WHEN active_days_30d = 1 THEN 'first_touch'
                            WHEN created_at >= now() - interval '30 days' THEN 'unactivated'
                            ELSE 'dormant'
                        END AS key,
                        COUNT(*) AS count
                    FROM features
                    GROUP BY 1
                    ORDER BY count DESC
                    """
                )
                segment_rows = cursor.fetchall()
    except Exception:
        log_event(
            event="admin_database_snapshot_failed",
            message="Admin business analytics snapshot failed",
            status="error",
        )
        return {
            "available": False,
            "accounts": {},
            "engagement": {},
            "signups": [],
            "checkin_streaks": [],
            "personality_distribution": [],
            "concern_distribution": [],
            "customer_segments": [],
        }

    accounts = {key: int(value or 0) for key, value in accounts.items()}
    engagement = {key: int(value or 0) for key, value in engagement.items()}
    login_enabled = accounts["login_enabled"]
    active_30d = engagement["active_30d"]
    engagement.update(
        {
            "active_rate_30d": percent(active_30d, login_enabled),
            "returning_rate_30d": percent(engagement["returning_30d"], active_30d),
            "ai_adoption_rate_30d": percent(engagement["ai_users_30d"], login_enabled),
            "checkin_rate_7d": percent(engagement["checkin_users_7d"], login_enabled),
        }
    )
    previous = accounts["new_previous_7_days"]
    current = accounts["new_last_7_days"]
    accounts["signup_growth_percent"] = (
        round(((current - previous) / previous) * 100, 1)
        if previous
        else (100.0 if current else 0.0)
    )

    by_day = {str(row["day"]): int(row["count"]) for row in signup_rows}
    today = datetime.now(timezone.utc).date()
    signups = [
        {
            "day": str(today - timedelta(days=offset)),
            "count": by_day.get(str(today - timedelta(days=offset)), 0),
        }
        for offset in range(6, -1, -1)
    ]

    streak_counts = {str(row["key"]): int(row["count"]) for row in streak_rows}
    streak_order = ["none", "days_1_2", "days_3_6", "days_7_13", "days_14_plus"]
    return {
        "available": True,
        "definitions": {
            "login_enabled": "Account status is active and the member is allowed to sign in.",
            "active_30d": "Member used AI chat, check-in, quiz, or skin tracking at least once in the last 30 days.",
            "returning_30d": "Member had meaningful product activity on at least two distinct days in the last 30 days.",
            "checkin_rate_7d": "Share of login-enabled accounts with at least one check-in in the last 7 days.",
        },
        "accounts": accounts,
        "engagement": engagement,
        "signups": signups,
        "checkin_streaks": [
            {"key": key, "count": streak_counts.get(key, 0)} for key in streak_order
        ],
        "personality_distribution": [
            {"key": str(row["key"]), "count": int(row["count"])}
            for row in personality_rows
        ],
        "concern_distribution": [
            {
                "key": str(row["key"]),
                "count": int(row["count"]),
                "records": int(row["records"]),
            }
            for row in concern_rows
        ],
        "customer_segments": [
            {"key": str(row["key"]), "count": int(row["count"])}
            for row in segment_rows
        ],
    }


def _list_users(*, search: str, page: int, limit: int) -> dict[str, Any]:
    offset = (page - 1) * limit
    pattern = f"%{search.strip()[:80]}%"
    filter_sql = """
        u.status <> 'deleted'
        AND (
            %s = '%%'
            OR COALESCE(un.username, '') ILIKE %s
            OR COALESCE(up.display_name, '') ILIKE %s
            OR COALESCE(ue.email, '') ILIKE %s
        )
    """
    params = (pattern, pattern, pattern, pattern)

    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM users u
                LEFT JOIN usernames un ON un.user_id = u.id
                LEFT JOIN user_profiles up ON up.user_id = u.id
                LEFT JOIN user_emails ue ON ue.user_id = u.id AND ue.is_primary
                WHERE {filter_sql}
                """,
                params,
            )
            total = int(cursor.fetchone()["total"])

            cursor.execute(
                f"""
                WITH selected AS (
                    SELECT
                        u.id,
                        u.status,
                        u.created_at,
                        un.username,
                        up.display_name,
                        ue.email
                    FROM users u
                    LEFT JOIN usernames un ON un.user_id = u.id
                    LEFT JOIN user_profiles up ON up.user_id = u.id
                    LEFT JOIN user_emails ue ON ue.user_id = u.id AND ue.is_primary
                    WHERE {filter_sql}
                    ORDER BY u.created_at DESC
                    LIMIT %s OFFSET %s
                )
                SELECT
                    s.*,
                    COALESCE((SELECT array_agg(ai.provider ORDER BY ai.provider) FROM auth_identities ai WHERE ai.user_id = s.id), ARRAY[]::text[]) AS providers,
                    (SELECT MAX(ai.last_login_at) FROM auth_identities ai WHERE ai.user_id = s.id) AS last_login_at,
                    (SELECT COUNT(*) FROM conversations c WHERE c.user_id = s.id) AS conversations,
                    (SELECT COUNT(*) FROM quiz_attempts q WHERE q.user_id = s.id) AS quiz_attempts,
                    (SELECT COUNT(*) FROM daily_checkins d WHERE d.user_id = s.id) AS daily_checkins,
                    (SELECT COUNT(*) FROM skin_records sr WHERE sr.user_id = s.id) AS skin_records,
                    (SELECT COUNT(*) FROM uploaded_assets ua WHERE ua.user_id = s.id AND ua.deleted_at IS NULL) AS uploaded_assets,
                    (SELECT COUNT(*) FROM auth_sessions ses WHERE ses.user_id = s.id AND ses.revoked_at IS NULL AND ses.expires_at > now()) AS live_sessions
                FROM selected s
                ORDER BY s.created_at DESC
                """,
                (*params, limit, offset),
            )
            rows = cursor.fetchall()

    items = []
    for row in rows:
        items.append(
            {
                "id": str(row["id"]),
                "username": row["username"],
                "display_name": row["display_name"],
                "email": row["email"],
                "status": row["status"],
                "providers": list(row["providers"] or []),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "last_login_at": row["last_login_at"].isoformat() if row["last_login_at"] else None,
                "resources": {
                    "conversations": int(row["conversations"] or 0),
                    "quiz_attempts": int(row["quiz_attempts"] or 0),
                    "daily_checkins": int(row["daily_checkins"] or 0),
                    "skin_records": int(row["skin_records"] or 0),
                    "uploaded_assets": int(row["uploaded_assets"] or 0),
                    "live_sessions": int(row["live_sessions"] or 0),
                },
            }
        )
    return {"items": items, "total": total, "page": page, "limit": limit}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if "embedding" not in str(key).lower() and "vector" not in str(key).lower()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _database_health_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "postgres": {"available": False, "tables": []},
        "neo4j": {"available": False, "nodes": [], "relations": []},
    }

    try:
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        current_database() AS database_name,
                        pg_database_size(current_database()) AS size_bytes,
                        numbackends AS connections,
                        xact_commit,
                        xact_rollback,
                        CASE WHEN (blks_hit + blks_read) = 0 THEN NULL
                             ELSE ROUND((blks_hit::numeric / (blks_hit + blks_read)) * 100, 2)
                        END AS cache_hit_percent
                    FROM pg_stat_database
                    WHERE datname = current_database()
                    """
                )
                summary = dict(cursor.fetchone() or {})
                cursor.execute(
                    """
                    SELECT
                        relname AS name,
                        n_live_tup::bigint AS estimated_rows,
                        pg_total_relation_size(relid) AS size_bytes
                    FROM pg_stat_user_tables
                    ORDER BY pg_total_relation_size(relid) DESC, relname
                    LIMIT 30
                    """
                )
                tables = [dict(row) for row in cursor.fetchall()]
        result["postgres"] = {
            "available": True,
            "summary": _json_safe(summary),
            "tables": _json_safe(tables),
        }
    except Exception:
        log_event(event="admin_postgres_health_failed", message="Admin PostgreSQL health failed", status="error")

    try:
        from tools.search.graph_search import get_driver

        driver = get_driver()
        with driver.session() as session:
            nodes = [
                dict(row)
                for row in session.run(
                    "MATCH (n) UNWIND labels(n) AS label "
                    "RETURN label, count(*) AS count ORDER BY count DESC"
                )
            ]
            relations = [
                dict(row)
                for row in session.run(
                    "MATCH ()-[r]->() RETURN type(r) AS relation, "
                    "count(*) AS count ORDER BY count DESC"
                )
            ]
        result["neo4j"] = {
            "available": True,
            "total_nodes": sum(int(item["count"]) for item in nodes),
            "total_relations": sum(int(item["count"]) for item in relations),
            "nodes": _json_safe(nodes),
            "relations": _json_safe(relations),
        }
    except Exception:
        log_event(event="admin_neo4j_health_failed", message="Admin Neo4j health failed", status="error")

    return result


_KNOWLEDGE_LABELS = {
    "products": "Product",
    "ingredients": "Ingredient",
    "regulations": "Regulation",
    "risk_rules": "RiskRule",
    "faqs": "FAQEntry",
    "skin_concerns": "SkinConcern",
    "categories": "Category",
}


def _knowledge_entries(*, entity: str, search: str, page: int, limit: int) -> dict[str, Any]:
    from tools.search.graph_search import get_driver

    offset = (page - 1) * limit
    driver = get_driver()
    with driver.session() as session:
        if entity == "relations":
            count_row = session.run("MATCH ()-[r]->() RETURN count(r) AS total").single()
            rows = session.run(
                """
                MATCH (source)-[relation]->(target)
                WITH source, relation, target,
                     properties(source) AS source_data, properties(target) AS target_data
                WITH source, relation, target,
                     coalesce(source_data.name, source_data.name_zh, source_data.name_en, source_data.law_name, source_data.risk_label, source_data.question, source_data.title, elementId(source)) AS source_name,
                     coalesce(target_data.name, target_data.name_zh, target_data.name_en, target_data.law_name, target_data.risk_label, target_data.question, target_data.title, elementId(target)) AS target_name
                WHERE $search = ''
                   OR toLower(toString(source_name)) CONTAINS toLower($search)
                   OR toLower(toString(target_name)) CONTAINS toLower($search)
                   OR toLower(type(relation)) CONTAINS toLower($search)
                RETURN elementId(relation) AS id,
                       type(relation) AS relation,
                       labels(source) AS source_labels,
                       source_name,
                       labels(target) AS target_labels,
                       target_name
                ORDER BY relation, source_name
                SKIP $offset LIMIT $limit
                """,
                search=search.strip(),
                offset=offset,
                limit=limit,
            )
            items = [_json_safe(dict(row)) for row in rows]
            return {
                "entity": entity,
                "items": items,
                "total": int((count_row or {}).get("total", 0)),
                "page": page,
                "limit": limit,
            }

        label = _KNOWLEDGE_LABELS.get(entity)
        if label is None:
            raise HTTPException(status_code=400, detail="Unsupported knowledge entity")
        query = f"""
            MATCH (node:{label})
            WITH node, properties(node) AS data
            WITH node, data,
                 coalesce(data.name, data.name_zh, data.name_en, data.normalized_name,
                          data.law_name, data.article, data.risk_label, data.question,
                          data.title, elementId(node)) AS display_name
            WHERE $search = ''
               OR toLower(toString(display_name)) CONTAINS toLower($search)
            RETURN elementId(node) AS id, display_name, data
            ORDER BY display_name
            SKIP $offset LIMIT $limit
        """
        count_query = f"""
            MATCH (node:{label})
            WITH node, properties(node) AS data
            WITH node, coalesce(data.name, data.name_zh, data.name_en, data.normalized_name,
                                data.law_name, data.article, data.risk_label, data.question,
                                data.title, elementId(node)) AS display_name
            WHERE $search = ''
               OR toLower(toString(display_name)) CONTAINS toLower($search)
            RETURN count(node) AS total
        """
        count_row = session.run(count_query, search=search.strip()).single()
        rows = session.run(
            query,
            search=search.strip(),
            offset=offset,
            limit=limit,
        )
        items = [_json_safe(dict(row)) for row in rows]
    return {
        "entity": entity,
        "items": items,
        "total": int((count_row or {}).get("total", 0)),
        "page": page,
        "limit": limit,
    }


def _get_user_detail(user_id: UUID) -> dict[str, Any] | None:
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    u.id, u.status, u.created_at, u.updated_at,
                    un.username, up.display_name, ue.email,
                    COALESCE((SELECT array_agg(ai.provider ORDER BY ai.provider) FROM auth_identities ai WHERE ai.user_id = u.id), ARRAY[]::text[]) AS providers,
                    (SELECT MAX(ai.last_login_at) FROM auth_identities ai WHERE ai.user_id = u.id) AS last_login_at,
                    (SELECT COUNT(*) FROM auth_sessions ses WHERE ses.user_id = u.id AND ses.revoked_at IS NULL AND ses.expires_at > now()) AS live_sessions
                FROM users u
                LEFT JOIN usernames un ON un.user_id = u.id
                LEFT JOIN user_profiles up ON up.user_id = u.id
                LEFT JOIN user_emails ue ON ue.user_id = u.id AND ue.is_primary
                WHERE u.id = %s AND u.status <> 'deleted'
                """,
                (user_id,),
            )
            profile = cursor.fetchone()
            if profile is None:
                return None

            cursor.execute(
                """
                SELECT id, title, flow_type, started_at, last_message_at, archived_at
                FROM conversations WHERE user_id = %s
                ORDER BY last_message_at DESC LIMIT 8
                """,
                (user_id,),
            )
            conversations = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT id, source, concern, severity, started_on, created_at, updated_at
                FROM skin_records WHERE user_id = %s
                ORDER BY created_at DESC LIMIT 8
                """,
                (user_id,),
            )
            skin_records = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT id, quiz_key, quiz_version, status, started_at, completed_at
                FROM quiz_attempts WHERE user_id = %s
                ORDER BY started_at DESC LIMIT 8
                """,
                (user_id,),
            )
            quizzes = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT id, checkin_date, mood_score, skin_status, notes, created_at
                FROM daily_checkins WHERE user_id = %s
                ORDER BY checkin_date DESC LIMIT 8
                """,
                (user_id,),
            )
            checkins = [dict(row) for row in cursor.fetchall()]

    return {
        "profile": _json_safe(dict(profile)),
        "recent": {
            "conversations": _json_safe(conversations),
            "skin_records": _json_safe(skin_records),
            "quiz_attempts": _json_safe(quizzes),
            "daily_checkins": _json_safe(checkins),
        },
    }




def _content_scheduler_status() -> dict[str, object]:
    status_path = Path("/var/lib/jenkins/tomod/cosmetic-agent/logs/firecrawl_scheduler_status.json")
    fallback: dict[str, object] = {
        "enabled": False,
        "frequency": "每週一 03:30（Asia/Taipei）",
        "next_run_at": None,
        "last_result": None,
        "categories": ["beauty_tip", "dermatologist_faq"],
    }
    try:
        status = json.loads(status_path.read_text())
        pid = int(status.get("pid") or 0)
        status["enabled"] = bool(status.get("enabled") and pid > 0 and Path(f"/proc/{pid}").exists())
        return {**fallback, **status}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return fallback


def create_admin_router(
    *,
    require_auth_session: Callable[..., Any],
    session_count: Callable[[], int],
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    def require_admin(request: Request) -> dict[str, Any]:
        account = require_auth_session(request)
        user_id = str(account.get("user_id", "")).strip()
        admin_user_ids = _configured_admin_user_ids()
        if not admin_user_ids:
            raise HTTPException(status_code=403, detail="Admin access is not configured")
        if not user_id or user_id not in admin_user_ids:
            raise HTTPException(status_code=403, detail="Admin access required")
        return account

    @router.get("/session")
    def admin_session(request: Request) -> JSONResponse:
        account = require_admin(request)
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content={
                "authorized": True,
                "role": "owner",
                "user_id_hash": _hash_user_id(account["user_id"]),
            },
        )

    @router.get("/overview")
    def admin_overview(request: Request) -> JSONResponse:
        account = require_admin(request)
        log_event(
            event="admin_overview_access",
            message="Admin viewed operations overview",
            user_id=account.get("user_id"),
            status="success",
        )
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content={
                "status": "ok",
                "service": "blink-api",
                "environment": os.getenv("APP_ENV", "development"),
                "release": os.getenv("APP_RELEASE") or "unversioned",
                "active_chat_sessions": max(0, int(session_count())),
                "platform": _platform_snapshot(),
                "gpu": _gpu_snapshot(),
                "endpoints": {
                    "live": "/api/v1/live",
                    "ready": "/api/v1/ready",
                    "metrics": "/metrics",
                },
                "telemetry": {
                    "errors_configured": bool(os.getenv("BETTER_STACK_ERRORS_DSN", "").strip()),
                    "logs_configured": bool(
                        os.getenv("BETTER_STACK_LOG_SOURCE_TOKEN", "").strip()
                        and os.getenv("BETTER_STACK_LOG_INGESTING_HOST", "").strip()
                    ),
                    "tracing_enabled": bool(getattr(request.app.state, "blink_tracing_enabled", False)),
                    "session_replay_enabled": False,
                },
                "links": {
                    "errors": _safe_dashboard_url("BETTER_STACK_ERRORS_DASHBOARD_URL"),
                    "logs": _safe_dashboard_url("BETTER_STACK_LOGS_DASHBOARD_URL"),
                    "metrics": _safe_dashboard_url("BETTER_STACK_METRICS_DASHBOARD_URL"),
                },
            },
        )

    @router.get("/gpu")
    def admin_gpu(
        request: Request,
        window: str = Query(default="1h", max_length=3),
    ) -> JSONResponse:
        require_admin(request)
        if window not in _GPU_WINDOWS:
            raise HTTPException(status_code=400, detail="Unsupported GPU history window")
        snapshot = _gpu_snapshot()
        try:
            _ensure_gpu_sampler(snapshot)
            history = _gpu_history(window)
            history["persistence_available"] = True
        except Exception as exc:
            history = {
                "window": window,
                "sample_interval_seconds": _GPU_SAMPLE_INTERVAL_SECONDS,
                "retention_days": _GPU_RETENTION_DAYS,
                "points": [],
                "processes": [],
                "persistence_available": False,
                "error_type": type(exc).__name__,
            }
        snapshot["history"] = history
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content=snapshot,
        )

    @router.get("/data-health")
    def admin_data_health(request: Request) -> JSONResponse:
        account = require_admin(request)
        log_event(
            event="admin_data_health_access",
            message="Admin viewed database and knowledge graph health",
            user_id=account.get("user_id"),
            status="success",
        )
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content=_database_health_snapshot(),
        )

    @router.get("/knowledge")
    def admin_knowledge(
        request: Request,
        entity: str = Query(default="products", max_length=40),
        search: str = Query(default="", max_length=120),
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> JSONResponse:
        account = require_admin(request)
        try:
            content = _knowledge_entries(
                entity=entity,
                search=search,
                page=page,
                limit=limit,
            )
        except HTTPException:
            raise
        except Exception:
            log_event(
                event="admin_knowledge_list_failed",
                message="Admin knowledge list failed",
                user_id=account.get("user_id"),
                status="error",
                entity=entity,
            )
            raise HTTPException(status_code=503, detail="Knowledge graph is temporarily unavailable")
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content=content,
        )

    @router.get("/users")
    def admin_users(
        request: Request,
        search: str = Query(default="", max_length=80),
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> JSONResponse:
        require_admin(request)
        try:
            content = _list_users(search=search, page=page, limit=limit)
        except Exception:
            log_event(
                event="admin_user_list_failed",
                message="Admin user list failed",
                status="error",
            )
            raise HTTPException(status_code=503, detail="User data is temporarily unavailable")
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content=content,
        )

    @router.get("/users/{user_id}")
    def admin_user_detail(user_id: str, request: Request) -> JSONResponse:
        require_admin(request)
        try:
            target_id = UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user id")
        try:
            content = _get_user_detail(target_id)
        except Exception:
            log_event(
                event="admin_user_detail_failed",
                message="Admin user detail failed",
                status="error",
                target_user_hash=_hash_user_id(user_id),
            )
            raise HTTPException(status_code=503, detail="User detail is temporarily unavailable")
        if content is None:
            raise HTTPException(status_code=404, detail="User not found")
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content=content,
        )

    @router.patch("/users/{user_id}/status")
    async def update_user_status(user_id: str, request: Request) -> JSONResponse:
        account = require_admin(request)
        try:
            target_id = UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user id")
        payload = await request.json()
        new_status = str(payload.get("status", "")).strip()
        if new_status not in {"active", "suspended"}:
            raise HTTPException(status_code=400, detail="Unsupported user status")
        if str(target_id) == str(account.get("user_id")) and new_status != "active":
            raise HTTPException(status_code=400, detail="You cannot suspend your own account")

        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE users
                    SET status = %s, updated_at = now()
                    WHERE id = %s AND status <> 'deleted'
                    RETURNING id, status
                    """,
                    (new_status, target_id),
                )
                updated = cursor.fetchone()
                if updated is None:
                    raise HTTPException(status_code=404, detail="User not found")
                if new_status == "suspended":
                    cursor.execute(
                        """
                        UPDATE auth_sessions
                        SET revoked_at = COALESCE(revoked_at, now())
                        WHERE user_id = %s AND revoked_at IS NULL
                        """,
                        (target_id,),
                    )

        log_event(
            event="admin_user_status_updated",
            message="Admin updated user status",
            user_id=account.get("user_id"),
            status="success",
            target_user_hash=_hash_user_id(target_id),
            new_status=new_status,
        )
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content={"id": str(target_id), "status": new_status},
        )

    @router.get("/product-reviews/stats")
    def admin_product_review_stats(request: Request) -> JSONResponse:
        require_admin(request)
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content=product_review_stats(),
        )

    @router.get("/product-reviews")
    def admin_product_reviews(
        request: Request,
        search: str = Query(default="", max_length=120),
        review_status: str = Query(default="", max_length=32),
        functional_category: str = Query(default="", max_length=40),
        cohort: str = Query(default="", max_length=32),
        brand_market: str = Query(default="", max_length=32),
        brand: str = Query(default="", max_length=120),
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> JSONResponse:
        require_admin(request)
        if review_status and review_status not in REVIEW_STATUSES:
            raise HTTPException(status_code=400, detail="Unsupported review status")
        content = list_product_reviews(
            search=search,
            review_status=review_status,
            functional_category=functional_category,
            cohort=cohort,
            brand_market=brand_market,
            brand=brand,
            page=page,
            limit=limit,
        )
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content=content,
        )

    @router.patch("/product-reviews/{canonical_key}")
    async def update_admin_product_review(
        canonical_key: str,
        request: Request,
    ) -> JSONResponse:
        account = require_admin(request)
        body = await request.json()
        status = str(body.get("review_status") or "").strip()
        try:
            updated = update_product_review(
                canonical_key,
                review_status=status,
                reviewer_notes=str(body.get("reviewer_notes") or ""),
                duplicate_of=(str(body.get("duplicate_of") or "").strip() or None),
                reviewer_id_hash=_hash_user_id(account.get("user_id")),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Product review candidate not found") from exc
        except ProductImportError as exc:
            log_event(
                event="admin_product_import_failed",
                message="Approved product could not be imported to Neo4j",
                user_id=account.get("user_id"),
                status="error",
                canonical_key=canonical_key,
                import_status=exc.product.get("import_status"),
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log_event(
            event="admin_product_review_updated",
            message="Admin updated a product review candidate",
            user_id=account.get("user_id"),
            status="success",
            canonical_key=canonical_key,
            review_status=status,
            import_status=updated.get("import_status"),
        )
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content=updated,
        )

    @router.get("/daily-contents")
    def admin_daily_contents(
        request: Request,
        review_status: str = Query(default="", max_length=32),
        content_type: str = Query(default="", max_length=32),
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> JSONResponse:
        require_admin(request)
        allowed_statuses = {"pending_review", "in_review", "published", "rejected", "archived"}
        allowed_types = {"beauty_tip", "dermatologist_faq"}
        if review_status and review_status not in allowed_statuses:
            raise HTTPException(status_code=400, detail="Unsupported review status")
        if content_type and content_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Unsupported content type")
        clauses, params = [], []
        if review_status:
            clauses.append("review_status = %s")
            params.append(review_status)
        if content_type:
            clauses.append("content_type = %s")
            params.append(content_type)
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        offset = (page - 1) * limit
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS total FROM daily_contents {where_sql}", tuple(params))
                total = int((cursor.fetchone() or {}).get("total", 0))
                cursor.execute(
                    f"""
                    SELECT id, content_type, title, summary, source_kind, source_name,
                           source_url, review_status, reviewer_notes, is_active,
                           display_priority, published_at, created_at, updated_at, metadata
                    FROM daily_contents {where_sql}
                    ORDER BY CASE review_status WHEN 'pending_review' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END,
                             created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    tuple([*params, limit, offset]),
                )
                items = [_json_safe(dict(row)) for row in cursor.fetchall()]
        return JSONResponse(status_code=200, headers={"Cache-Control": "no-store"}, content={"items": items, "total": total, "page": page, "limit": limit})

    @router.get("/daily-contents/stats")
    def admin_daily_content_stats(request: Request) -> JSONResponse:
        require_admin(request)
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT review_status, content_type, COUNT(*) AS count
                    FROM daily_contents
                    GROUP BY review_status, content_type
                    ORDER BY review_status, content_type
                """)
                rows = [_json_safe(dict(row)) for row in cursor.fetchall()]
                cursor.execute("""
                    SELECT COUNT(*) AS total,
                           COUNT(DISTINCT source_url) FILTER (WHERE source_kind = 'crawler') AS crawler_sources,
                           MAX(updated_at) FILTER (WHERE source_kind = 'crawler') AS latest_crawl_at
                    FROM daily_contents
                """)
                overview = _json_safe(dict(cursor.fetchone() or {}))
        return JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content={
                "counts": rows,
                "total": int(overview.get("total") or 0),
                "crawler_sources": int(overview.get("crawler_sources") or 0),
                "latest_crawl_at": overview.get("latest_crawl_at"),
                "automation": _content_scheduler_status(),
            },
        )

    @router.patch("/daily-contents/{content_id}")
    async def update_admin_daily_content(content_id: str, request: Request) -> JSONResponse:
        account = require_admin(request)
        try:
            target_id = UUID(content_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid content id") from exc
        body = await request.json()
        allowed_statuses = {"pending_review", "in_review", "published", "rejected", "archived"}
        updates, params = [], []
        for field in ("title", "summary", "reviewer_notes"):
            if field in body:
                value = str(body[field]).strip()
                if field in {"title", "summary"} and not value:
                    raise HTTPException(status_code=400, detail=f"{field} cannot be empty")
                updates.append(f"{field} = %s")
                params.append(value)
        if "display_priority" in body:
            priority = int(body["display_priority"])
            if priority < 0 or priority > 1000:
                raise HTTPException(status_code=400, detail="display_priority must be between 0 and 1000")
            updates.append("display_priority = %s")
            params.append(priority)
        if "review_status" in body:
            status = str(body["review_status"])
            if status not in allowed_statuses:
                raise HTTPException(status_code=400, detail="Unsupported review status")
            updates.extend(["review_status = %s", "reviewed_at = now()"])
            params.append(status)
            if status == "published":
                updates.extend(["is_active = TRUE", "published_at = COALESCE(published_at, now())"])
            elif status in {"pending_review", "in_review", "rejected", "archived"}:
                updates.append("is_active = FALSE")
        if not updates:
            raise HTTPException(status_code=400, detail="No supported fields supplied")
        updates.append("updated_at = now()")
        params.append(target_id)
        idempotent_replay = False
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                # Serialize simultaneous clicks for this row. The second request sees
                # the committed status and becomes a no-op instead of another transition.
                cursor.execute("SELECT * FROM daily_contents WHERE id = %s FOR UPDATE", (target_id,))
                existing = cursor.fetchone()
                if existing is None:
                    raise HTTPException(status_code=404, detail="Daily content not found")
                status_only_request = set(body) == {"review_status"}
                same_status = body.get("review_status") == existing.get("review_status")
                if status_only_request and same_status:
                    row = existing
                    idempotent_replay = True
                else:
                    cursor.execute(
                        f"UPDATE daily_contents SET {', '.join(updates)} WHERE id = %s RETURNING *",
                        tuple(params),
                    )
                    row = cursor.fetchone()
            connection.commit()
        if not idempotent_replay:
            log_event(
                event="admin_daily_content_updated",
                message="Admin updated daily content review state",
                user_id=account.get("user_id"),
                status="success",
                content_id=str(target_id),
                review_status=body.get("review_status"),
            )
        return JSONResponse(
            status_code=200,
            headers={
                "Cache-Control": "no-store",
                "X-Idempotent-Replay": "true" if idempotent_replay else "false",
            },
            content=_json_safe(dict(row)),
        )



    @router.get("/costs/summary")
    def get_cost_summary(request: Request, days: int = Query(default=30, ge=1, le=365)):
        require_admin(request)
        cost_expr = """CASE WHEN price.id IS NULL THEN NULL ELSE
            ((GREATEST(e.input_tokens-e.cached_input_tokens,0)::numeric/1000000)*COALESCE(price.input_usd_per_million,0)) +
            ((e.cached_input_tokens::numeric/1000000)*COALESCE(price.cached_input_usd_per_million,price.input_usd_per_million,0)) +
            ((e.output_tokens::numeric/1000000)*COALESCE(price.output_usd_per_million,0)) +
            (e.quantity*COALESCE(price.usd_per_unit,0)) END"""
        lateral = """LEFT JOIN LATERAL (
            SELECT p.* FROM api_usage_pricing p
            WHERE p.active AND p.category=e.category AND p.provider=e.provider
              AND p.service_pattern IN ('*', e.service)
              AND p.operation_pattern IN ('*', e.operation)
              AND p.effective_from <= e.occurred_at
            ORDER BY (p.service_pattern=e.service)::int DESC,
                     (p.operation_pattern=e.operation)::int DESC, p.effective_from DESC
            LIMIT 1
        ) price ON TRUE"""
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"""SELECT e.category, COUNT(*) calls,
                    COUNT(*) FILTER (WHERE e.status='success') success_calls,
                    SUM(e.total_tokens) total_tokens, SUM(e.quantity) quantity,
                    SUM({cost_expr}) cost_usd,
                    COUNT(*) FILTER (WHERE price.id IS NULL) unpriced_calls
                    FROM api_usage_events e {lateral}
                    WHERE e.occurred_at >= NOW()-(%s || ' days')::interval
                    GROUP BY e.category ORDER BY e.category""", (days,))
                categories = [dict(row) for row in cursor.fetchall()]
                cursor.execute(f"""SELECT e.provider,e.service,e.operation,e.category,COUNT(*) calls,
                    SUM(e.total_tokens) total_tokens,SUM(e.quantity) quantity,
                    SUM({cost_expr}) cost_usd,COUNT(*) FILTER (WHERE price.id IS NULL) unpriced_calls,
                    ROUND(AVG(e.latency_ms),1) avg_latency_ms
                    FROM api_usage_events e {lateral}
                    WHERE e.occurred_at >= NOW()-(%s || ' days')::interval
                    GROUP BY e.provider,e.service,e.operation,e.category
                    ORDER BY COALESCE(SUM({cost_expr}),0) DESC, COUNT(*) DESC LIMIT 100""", (days,))
                services = [dict(row) for row in cursor.fetchall()]
                cursor.execute(f"""SELECT DATE(e.occurred_at AT TIME ZONE 'Asia/Taipei') AS "day",
                    COUNT(*) calls,SUM(e.total_tokens) total_tokens,SUM({cost_expr}) cost_usd,
                    COUNT(*) FILTER (WHERE price.id IS NULL) unpriced_calls
                    FROM api_usage_events e {lateral}
                    WHERE e.occurred_at >= NOW()-(%s || ' days')::interval
                    GROUP BY 1 ORDER BY 1""", (days,))
                daily = [dict(row) for row in cursor.fetchall()]
                cursor.execute("""SELECT id,category,provider,service_pattern,operation_pattern,currency,
                    input_usd_per_million,cached_input_usd_per_million,output_usd_per_million,
                    usd_per_unit,effective_from,updated_at FROM api_usage_pricing
                    WHERE active ORDER BY category,provider,service_pattern,operation_pattern""")
                pricing = [dict(row) for row in cursor.fetchall()]
                cursor.execute("""SELECT occurred_at,category,provider,service,operation,status,
                    input_tokens,cached_input_tokens,output_tokens,total_tokens,quantity,
                    unit_type,latency_ms,error_type FROM api_usage_events
                    ORDER BY occurred_at DESC LIMIT 50""")
                recent = [dict(row) for row in cursor.fetchall()]
        return JSONResponse(content=_json_safe({"days":days,"currency":"USD","categories":categories,
            "services":services,"daily":daily,"pricing":pricing,"recent":recent}),
            headers={"Cache-Control":"no-store"})

    @router.post("/costs/pricing")
    async def upsert_cost_pricing(request: Request):
        admin = require_admin(request)
        body = await request.json()
        category = str(body.get("category", "")).strip()
        provider = str(body.get("provider", "")).strip().lower()
        service = str(body.get("service_pattern", "*")).strip() or "*"
        operation = str(body.get("operation_pattern", "*")).strip() or "*"
        if category not in {"ai_model","memory","crawler","other"} or not provider:
            raise HTTPException(status_code=422, detail="Invalid category/provider")
        fields = ["input_usd_per_million","cached_input_usd_per_million","output_usd_per_million","usd_per_unit"]
        values = {}
        for field in fields:
            raw = body.get(field)
            if raw in (None, ""): values[field] = None
            else:
                try: values[field] = float(raw)
                except (TypeError,ValueError): raise HTTPException(status_code=422,detail=f"Invalid {field}")
                if values[field] < 0: raise HTTPException(status_code=422,detail=f"Invalid {field}")
        if all(values[field] is None for field in fields):
            raise HTTPException(status_code=422,detail="At least one price is required")
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""INSERT INTO api_usage_pricing
                    (category,provider,service_pattern,operation_pattern,input_usd_per_million,
                     cached_input_usd_per_million,output_usd_per_million,usd_per_unit)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (category,provider,service_pattern,operation_pattern) WHERE active=TRUE
                    DO UPDATE SET input_usd_per_million=EXCLUDED.input_usd_per_million,
                      cached_input_usd_per_million=EXCLUDED.cached_input_usd_per_million,
                      output_usd_per_million=EXCLUDED.output_usd_per_million,
                      usd_per_unit=EXCLUDED.usd_per_unit,updated_at=NOW()
                    RETURNING *""", (category,provider,service,operation,values[fields[0]],values[fields[1]],values[fields[2]],values[fields[3]]))
                row = dict(cursor.fetchone()); connection.commit()
        log_event("admin.cost_pricing_updated", message="Admin updated API/tool pricing", user_id=str(admin.get("user_id","")), provider=provider, service=service)
        return JSONResponse(content=_json_safe(row),headers={"Cache-Control":"no-store"})

    return router

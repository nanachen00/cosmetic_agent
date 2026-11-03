import pytest

from app.services import chat_job_store as job_store_module
from core import admission as admission_module


def test_admission_required_mode_rejects_missing_url(monkeypatch):
    monkeypatch.setenv("CHAT_ADMISSION_REQUIRE_REDIS", "1")
    monkeypatch.delenv("CHAT_ADMISSION_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(RuntimeError, match="no Redis URL"):
        admission_module.create_chat_admission_controller(
            max_running=1,
            max_queued=1,
        )


def test_job_store_required_mode_rejects_missing_url(monkeypatch):
    monkeypatch.setenv("CHAT_JOB_STORE_REQUIRE_REDIS", "1")
    monkeypatch.delenv("CHAT_JOB_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(RuntimeError, match="requires CHAT_JOB_REDIS_URL"):
        job_store_module.create_chat_job_store()


def test_admission_required_mode_does_not_fallback_on_missing_async_client(monkeypatch):
    monkeypatch.setenv("CHAT_ADMISSION_REQUIRE_REDIS", "1")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")

    def missing_async_client(*args, **kwargs):
        raise ImportError("redis.asyncio unavailable")

    monkeypatch.setattr(
        admission_module,
        "RedisChatAdmissionController",
        missing_async_client,
    )
    with pytest.raises(RuntimeError, match="asyncio support is required"):
        admission_module.create_chat_admission_controller(
            max_running=1,
            max_queued=1,
        )


def test_job_store_required_mode_does_not_fallback_on_missing_async_client(monkeypatch):
    monkeypatch.setenv("CHAT_JOB_STORE_REQUIRE_REDIS", "1")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")

    def missing_async_client(*args, **kwargs):
        raise ImportError("redis.asyncio unavailable")

    monkeypatch.setattr(
        job_store_module,
        "RedisChatJobStore",
        missing_async_client,
    )
    with pytest.raises(RuntimeError, match="asyncio support is required"):
        job_store_module.create_chat_job_store()

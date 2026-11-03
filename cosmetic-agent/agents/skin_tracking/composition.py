"""Skin Tracking settings and dependency wiring.

RECONSTRUCTED 2026-10-21 from cosmetic-agent-stable/agents/skin_tracking/composition.py,
updated for the current tree:
  - AUTH_STORAGE_BACKEND=postgres uses the PostgreSQL skin-record store
    (same store api_server.get_store() uses), so Neo4j credentials become optional.
  - An optional OpenAI-compatible report model (local vLLM) can be injected via
    SKIN_REPORT_MODEL_BASE_URL.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from agents.skin_tracking.extraction import (
    DEFAULT_ENV_FILE,
    DEFAULT_EXTRACTION_MODEL,
    OpenAISkinExtractor,
    SkinExtractionConfigurationError,
    SkinExtractionProvider,
)
from agents.skin_tracking.service import SkinRecordService, SkinTrackingService
from agents.skin_tracking.session_store import SkinSessionStore
from agents.skin_tracking.tracking_agent import ChatModel, SkinRecordRepository, SkinTrackingAgent

_KEYS = {
    "OPENAI_API_KEY",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "OPENAI_SKIN_TRACKING_MODEL",
    "SKIN_TRACKING_SESSION_TTL_SECONDS",
    "AUTH_STORAGE_BACKEND",
    "SKIN_EXTRACTION_BASE_URL",
    "SKIN_REPORT_MODEL_BASE_URL",
    "SKIN_REPORT_MODEL",
}


class SkinTrackingConfigurationError(RuntimeError):
    """Raised when Skin Tracking cannot be assembled from local configuration."""


@dataclass(frozen=True)
class SkinTrackingSettings:
    openai_api_key: str = field(repr=False)
    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = field(default="", repr=False)
    extraction_model: str = DEFAULT_EXTRACTION_MODEL
    session_ttl_seconds: int = 3600
    storage_backend: str = "postgres"
    extraction_base_url: str = ""
    report_model_base_url: str = ""
    report_model: str = "taide-12b"

    @classmethod
    def from_env_file(
        cls,
        env_file: str | Path | None = DEFAULT_ENV_FILE,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "SkinTrackingSettings":
        values: dict[str, str] = {}
        if env_file is not None and Path(env_file).exists():
            try:
                from dotenv import dotenv_values
            except ImportError as exc:
                raise SkinTrackingConfigurationError(
                    "Reading a Skin Tracking env file requires python-dotenv."
                ) from exc
            values.update(
                {k: v for k, v in dotenv_values(env_file).items() if k in _KEYS and isinstance(v, str)}
            )
        runtime = os.environ if environ is None else environ
        values.update({k: runtime[k] for k in _KEYS if k in runtime})

        backend = values.get("AUTH_STORAGE_BACKEND", "postgres").strip().lower() or "postgres"
        required = ["NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"] if backend != "postgres" else []
        if not values.get("SKIN_EXTRACTION_BASE_URL", "").strip():
            required.insert(0, "OPENAI_API_KEY")
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise SkinTrackingConfigurationError("Missing Skin Tracking settings: " + ", ".join(missing))

        try:
            ttl_seconds = int(values.get("SKIN_TRACKING_SESSION_TTL_SECONDS", "3600"))
            if ttl_seconds <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise SkinTrackingConfigurationError(
                "SKIN_TRACKING_SESSION_TTL_SECONDS must be a positive integer."
            ) from exc

        return cls(
            openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
            neo4j_uri=values.get("NEO4J_URI", "").strip(),
            neo4j_user=values.get("NEO4J_USER", "").strip(),
            neo4j_password=values.get("NEO4J_PASSWORD", "").strip(),
            extraction_model=values.get("OPENAI_SKIN_TRACKING_MODEL", "").strip() or DEFAULT_EXTRACTION_MODEL,
            session_ttl_seconds=ttl_seconds,
            storage_backend=backend,
            extraction_base_url=values.get("SKIN_EXTRACTION_BASE_URL", "").strip(),
            report_model_base_url=values.get("SKIN_REPORT_MODEL_BASE_URL", "").strip(),
            report_model=values.get("SKIN_REPORT_MODEL", "").strip() or "taide-12b",
        )


class OpenAICompatibleChatModel:
    """ChatModel adapter for an OpenAI-compatible server such as local vLLM."""

    def __init__(self, *, base_url: str, model: str, api_key: str = "EMPTY", timeout: float = 30.0):
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)

    def chat(self, messages: list[dict], **kwargs: Any) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 800),
        )
        return response.choices[0].message.content or ""


@dataclass
class SkinTrackingComponents:
    extractor: SkinExtractionProvider
    store: SkinRecordRepository
    sessions: SkinSessionStore
    agent: SkinTrackingAgent
    tracking_service: SkinTrackingService
    record_service: SkinRecordService
    owns_store: bool = field(default=False, repr=False)

    def health(self) -> dict[str, Any]:
        dependencies = {
            "extractor": {"status": "configured", "model": getattr(self.extractor, "model", "unknown")},
            "store": {"status": "reachable"},
            "sessions": {"status": "ready", "backend": "memory"},
        }
        try:
            getattr(self.store, "verify_connectivity")()
        except Exception:
            dependencies["store"] = {"status": "unreachable"}
            return {"status": "not_ready", "dependencies": dependencies}
        return {"status": "ready", "dependencies": dependencies}

    def close(self) -> None:
        if self.owns_store:
            self.store.close()


def assemble_skin_tracking(
    extractor: SkinExtractionProvider,
    store: SkinRecordRepository,
    *,
    sessions: SkinSessionStore | None = None,
    report_model: ChatModel | None = None,
    session_ttl_seconds: int = 3600,
    owns_store: bool = False,
) -> SkinTrackingComponents:
    session_store = sessions or SkinSessionStore(ttl_seconds=session_ttl_seconds)
    agent = SkinTrackingAgent(extractor, store, report_model=report_model)
    return SkinTrackingComponents(
        extractor=extractor,
        store=store,
        sessions=session_store,
        agent=agent,
        tracking_service=SkinTrackingService(agent, session_store),
        record_service=SkinRecordService(store, llm=report_model),
        owns_store=owns_store,
    )


def _build_store(settings: SkinTrackingSettings) -> tuple[SkinRecordRepository, bool]:
    if settings.storage_backend == "postgres":
        from database.postgres.repositories.skin_record import postgres_skin_record_store

        return postgres_skin_record_store, False
    from tools.agent.tracking.skin_record_tool import SkinRecordStore

    return (
        SkinRecordStore(uri=settings.neo4j_uri, user=settings.neo4j_user, password=settings.neo4j_password),
        True,
    )


def build_skin_tracking(
    settings: SkinTrackingSettings,
    *,
    report_model: ChatModel | None = None,
) -> SkinTrackingComponents:
    try:
        extractor = OpenAISkinExtractor(
            api_key=settings.openai_api_key or None,
            model=settings.extraction_model,
            env_file=None,
            base_url=settings.extraction_base_url or None,
        )
    except SkinExtractionConfigurationError as exc:
        raise SkinTrackingConfigurationError(str(exc)) from None

    if report_model is None and settings.report_model_base_url:
        report_model = OpenAICompatibleChatModel(
            base_url=settings.report_model_base_url, model=settings.report_model
        )

    store, owns_store = _build_store(settings)
    try:
        return assemble_skin_tracking(
            extractor,
            store,
            report_model=report_model,
            session_ttl_seconds=settings.session_ttl_seconds,
            owns_store=owns_store,
        )
    except Exception:
        if owns_store:
            store.close()
        raise


def build_skin_tracking_from_env(
    env_file: str | Path | None = DEFAULT_ENV_FILE,
    *,
    report_model: ChatModel | None = None,
) -> SkinTrackingComponents:
    settings = SkinTrackingSettings.from_env_file(env_file)
    return build_skin_tracking(settings, report_model=report_model)

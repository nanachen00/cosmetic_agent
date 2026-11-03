"""Skin Tracking process lifecycle without owning the Root application."""
from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Callable

from fastapi import APIRouter

from agents.skin_tracking.composition import (
    SkinTrackingComponents,
    SkinTrackingConfigurationError,
    SkinTrackingSettings,
    build_skin_tracking,
    build_skin_tracking_from_env,
)
from agents.skin_tracking.extraction import DEFAULT_ENV_FILE
from agents.skin_tracking.http_api import create_router
from agents.skin_tracking.tracking_agent import (
    ChatModel,
    configure,
    reset_configuration,
)


# A process shares one driver, extractor and session store across Root and HTTP calls.
_components: SkinTrackingComponents | None = None
_lifecycle_lock = RLock()


def initialize_skin_tracking(
    settings: SkinTrackingSettings | None = None,
    *,
    env_file: str | Path | None = DEFAULT_ENV_FILE,
    report_model: ChatModel | None = None,
) -> SkinTrackingComponents:
    """Build dependencies once and connect the legacy Root entrypoint to them."""
    global _components
    with _lifecycle_lock:
        if _components is not None:
            return _components

        components: SkinTrackingComponents | None = None
        try:
            components = (
                build_skin_tracking(settings, report_model=report_model)
                if settings is not None
                else build_skin_tracking_from_env(env_file, report_model=report_model)
            )
            # Root keeps its existing run(...) contract; only its dependencies change.
            configure(components.extractor, components.store, report_model)
        except SkinTrackingConfigurationError:
            if components is not None:
                _close_after_failed_initialization(components)
            raise
        except Exception:
            if components is not None:
                _close_after_failed_initialization(components)
            # Provider exceptions can include private endpoints or credentials.
            raise SkinTrackingConfigurationError(
                "Unable to initialize Skin Tracking runtime."
            ) from None

        _components = components
        return components


def get_skin_tracking_components() -> SkinTrackingComponents:
    """Return the initialized runtime or fail before a request touches a dependency."""
    with _lifecycle_lock:
        if _components is None:
            raise SkinTrackingConfigurationError(
                "Skin Tracking is not initialized. Call initialize_skin_tracking() "
                "during application startup."
            )
        return _components


def shutdown_skin_tracking() -> None:
    """Release owned resources once; repeated application shutdown is safe."""
    global _components
    with _lifecycle_lock:
        components = _components
        if components is None:
            return
        _components = None
        reset_configuration()

    components.close()


def create_skin_tracking_router(get_user_id: Callable) -> APIRouter:
    """Create a router that resolves shared services at request time."""
    return create_router(
        get_user_id=get_user_id,
        get_tracking_service=lambda: get_skin_tracking_components().tracking_service,
        get_record_service=lambda: get_skin_tracking_components().record_service,
    )


def _close_after_failed_initialization(components: SkinTrackingComponents) -> None:
    """Best-effort cleanup without replacing a sanitized startup error."""
    try:
        components.close()
    except Exception:
        pass

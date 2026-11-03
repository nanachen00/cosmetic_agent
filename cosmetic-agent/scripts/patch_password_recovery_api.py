"""Add the isolated PostgreSQL recovery router without editing existing routes."""

from pathlib import Path


path = Path("api/api_server.py")
content = path.read_text(encoding="utf-8")
if "register_password_recovery_routes(" in content:
    raise SystemExit("password recovery API already registered")

import_anchor = """from app.api.routes.auth import (
    router as auth_router,
    register_onboarding_status_route,
    register_password_entry_routes,
    register_password_management_routes,
    register_session_routes,
    register_username_routes,
)
"""
imports = """from app.api.routes.password_recovery import (
    register_password_recovery_routes,
    router as password_recovery_router,
)
from database.repositories.password_auth import (
    PasswordAuthValidationError as PostgresPasswordValidationError,
)
from database.repositories.password_reset import (
    InvalidPasswordResetCodeError as PostgresInvalidPasswordResetCodeError,
    PasswordResetError as PostgresPasswordResetError,
    PasswordResetRateLimitedError as PostgresPasswordResetRateLimitedError,
)
from tools.auth.postgres_password_recovery import (
    create_postgres_password_recovery_service,
)
"""
if content.count(import_anchor) != 1:
    raise SystemExit("unexpected auth import block")
content = content.replace(import_anchor, import_anchor + imports, 1)

include_anchor = "app.include_router(auth_router)\n"
registration = """register_password_recovery_routes(
    recovery_service_factory=create_postgres_password_recovery_service,
    issue_auth_session=_issue_auth_session,
    invalid_code_error=PostgresInvalidPasswordResetCodeError,
    rate_limited_error=PostgresPasswordResetRateLimitedError,
    password_validation_error=PostgresPasswordValidationError,
    recovery_error=PostgresPasswordResetError,
)
app.include_router(password_recovery_router)
"""
if content.count(include_anchor) != 1:
    raise SystemExit("unexpected auth router include")
content = content.replace(include_anchor, include_anchor + registration, 1)
path.write_text(content, encoding="utf-8")

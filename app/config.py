"""Environment-validated application configuration — ARC-01 / ARC-12.

Three configs: Development, Testing, Production. Selected by
:func:`resolve_config` from ``FLASK_ENV`` / explicit test overrides.
Production fails fast at boot when required secrets are missing instead of
running half-configured.
"""

import os
from datetime import timedelta

_MISSING = object()

#: Env vars that MUST be present for production to boot. Each maps to the
#: config key (and the human-readable hint shown in the error).
REQUIRED_PROD_SECRETS = {
    "SECRET_KEY": "a long random secret (generate: python -c "
    '"import secrets; print(secrets.token_hex(32))")',
}


class BaseConfig:
    """Defaults shared by every environment."""

    TESTING = False
    DEBUG = False

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Remember-me hardening (audit fix, 2026-10-28): Flask-Login
    # defaults were 365 days, Secure False, SameSite None — a stolen
    # remember cookie stayed valid up to a year and was more exposed
    # than the session cookie. 30 days + Lax everywhere; Secure only in
    # ProductionConfig (dev runs over plain http).
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_DURATION = timedelta(days=30)

    RATELIMIT_DEFAULTS = ["200 per day", "60 per hour"]

    # Feature flags (ARC-12)
    FEATURE_LABS = True
    FEATURE_COMMUNITY = True
    FEATURE_PEER_MOCKS = True
    LLM_MODE = os.environ.get("LLM_MODE", "server")  # off | byok | server

    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///studybuddy_dev.db"
    )


class TestingConfig(BaseConfig):
    TESTING = True
    SESSION_COOKIE_SECURE = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


class ProductionConfig(BaseConfig):
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

    @classmethod
    def validate(cls):
        """Fail fast when production secrets are missing.

        Raises:
            RuntimeError: listing every missing required env var.
        """
        insecure = {
            None,
            "",
            "change-me-to-a-long-random-secret",
            "unified-ai-platform-secret-2026",
        }
        secret = os.environ.get("SECRET_KEY")
        missing = []
        if secret in insecure:
            missing.append(f"SECRET_KEY ({REQUIRED_PROD_SECRETS['SECRET_KEY']})")
        for var in REQUIRED_PROD_SECRETS:
            if var == "SECRET_KEY":
                continue  # handled above (placeholder counts as missing)
            if not os.environ.get(var):
                missing.append(f"{var} ({REQUIRED_PROD_SECRETS[var]})")
        if missing:
            raise RuntimeError(
                "Refusing to boot in production with missing/insecure "
                f"secrets: {', '.join(missing)}. Set them as environment variables."
            )


def resolve_config(config_overrides=None):
    """Pick the config class for this boot.

    Explicit ``TESTING`` overrides (the pytest convention) always win; then
    ``FLASK_ENV``; anything else is development.
    """
    overrides = config_overrides or {}
    if overrides.get("TESTING") or os.environ.get("FLASK_ENV") == "testing":
        return TestingConfig
    if os.environ.get("FLASK_ENV") == "production":
        return ProductionConfig
    return DevelopmentConfig


def validate_for_env(config_cls):
    """Run fail-fast validation. Only ProductionConfig validates."""
    if config_cls is ProductionConfig:
        config_cls.validate()


__all__ = [
    "BaseConfig",
    "DevelopmentConfig",
    "TestingConfig",
    "ProductionConfig",
    "REQUIRED_PROD_SECRETS",
    "resolve_config",
    "validate_for_env",
]

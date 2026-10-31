"""ARC-12: validated application configuration and feature flags.

All environment variables are declared here with types and defaults.
Nothing reads os.environ for config outside this module (and create_app's
legacy wiring, which is migrating here).

Feature flags let unfinished or low-supply features hide in production:
  FEATURE_LABS=0        Ultimate Lab dispatcher (/ultimate/*)
  FEATURE_PEER_MOCKS=0  peer mock interviews
  FEATURE_COMMUNITY=0   community mentors board
  RUBRIC_EMBEDDINGS=0   interview rubric: Gemini embeddings instead of
                        TF-IDF cosine (default OFF; TF-IDF stays default)
  LLM_MODE=off|byok|server  'off' disables all LLM calls (deterministic
                            fallbacks only); 'server' uses server keys.
"""
import os
from dataclasses import dataclass, field


def _bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AppConfig:
    # --- core ---
    secret_key: str = field(default="")
    database_url: str = field(default="")
    flask_env: str = field(default="production")

    # --- LLM providers (server keys; empty = feature degrades gracefully) ---
    gemini_api_key: str = field(default="")
    groq_api_key: str = field(default="")
    openrouter_api_key: str = field(default="")
    llm_mode: str = field(default="server")  # off|byok|server

    # --- OAuth ---
    google_client_id: str = field(default="")
    google_client_secret: str = field(default="")

    # --- limits ---
    ultimate_run_limit: str = field(default="30 per hour")
    max_upload_mb: int = field(default=16)

    # --- feature flags ---
    feature_labs: bool = field(default=True)
    feature_peer_mocks: bool = field(default=True)
    feature_community: bool = field(default=True)
    rubric_embeddings: bool = field(default=False)

    @classmethod
    def from_env(cls):
        llm_mode = os.environ.get("LLM_MODE", "server").strip().lower()
        if llm_mode not in ("off", "byok", "server"):
            llm_mode = "server"
        return cls(
            secret_key=os.environ.get("SECRET_KEY", ""),
            database_url=os.environ.get("DATABASE_URL", ""),
            flask_env=os.environ.get("FLASK_ENV", "production"),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            llm_mode=llm_mode,
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            ultimate_run_limit=os.environ.get("ULTIMATE_RUN_LIMIT",
                                              "30 per hour"),
            max_upload_mb=_int("MAX_UPLOAD_MB", 16),
            feature_labs=_bool("FEATURE_LABS", True),
            feature_peer_mocks=_bool("FEATURE_PEER_MOCKS", True),
            feature_community=_bool("FEATURE_COMMUNITY", True),
            rubric_embeddings=_bool("RUBRIC_EMBEDDINGS", False),
        )

    @property
    def is_production(self):
        return self.flask_env == "production" or bool(os.environ.get("RENDER"))

    def llm_enabled(self):
        """False when LLM_MODE=off — every AI feature must check this."""
        return self.llm_mode != "off"


# Singleton, loaded once at import.
config = AppConfig.from_env()

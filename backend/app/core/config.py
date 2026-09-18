"""Single source of environment truth (LAYER 1).

Every name here maps onto `.env.example`.  Two naming conventions coexist in
that file: the ATLAS-owned knobs are prefixed (``ATLAS_MOCK_BROKERS``) while the
third-party vendor blocks are bare (``TRADOVATE_CID``, ``KRAKEN_API_SECRET``,
``FCM_PROJECT_ID``).  Rather than fight that with a single ``env_prefix`` we give
every field an explicit ``AliasChoices`` so BOTH spellings resolve, with the
``ATLAS_``-prefixed variant always winning.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, List

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _alias(name: str) -> AliasChoices:
    """Accept ``ATLAS_<NAME>`` first, then the bare ``<NAME>``."""
    upper = name.upper()
    if upper.startswith("ATLAS_"):
        return AliasChoices(upper, upper[len("ATLAS_") :])
    return AliasChoices(f"ATLAS_{upper}", upper)


class Settings(BaseSettings):
    """Runtime configuration, loaded once and cached by :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ── Runtime ────────────────────────────────────────────────────────────
    env: str = Field(default="development", validation_alias=_alias("ENV"))
    log_level: str = Field(default="INFO", validation_alias=_alias("LOG_LEVEL"))
    backend_port: int = Field(default=8000, validation_alias=_alias("BACKEND_PORT"))
    mock_brokers: bool = Field(default=True, validation_alias=_alias("MOCK_BROKERS"))

    # ── Infrastructure ─────────────────────────────────────────────────────
    redis_url: str = Field(
        default="redis://localhost:6379/0", validation_alias=_alias("REDIS_URL")
    )
    database_url: str = Field(
        default="postgresql+asyncpg://atlas:atlas_dev_password@localhost:5432/atlas",
        validation_alias=_alias("DATABASE_URL"),
    )

    # ── Webhook authentication (spec §3, frozen contract A) ────────────────
    webhook_token: str = Field(
        default="change_me_tradingview_token", validation_alias=_alias("WEBHOOK_TOKEN")
    )
    webhook_hmac_secret: str = Field(
        default="change_me_hmac_secret", validation_alias=_alias("WEBHOOK_HMAC_SECRET")
    )
    webhook_signature_header: str = Field(default="X-Atlas-Signature")

    # ── HITL confirmation window (spec §5) ─────────────────────────────────
    confirmation_ttl_seconds: int = Field(
        default=60, validation_alias=_alias("CONFIRMATION_TTL_SECONDS")
    )

    # ── Risk guardrails ────────────────────────────────────────────────────
    default_risk_pct: float = Field(default=1.0, validation_alias=_alias("DEFAULT_RISK_PCT"))
    max_risk_pct: float = Field(default=5.0, validation_alias=_alias("MAX_RISK_PCT"))
    max_contracts: int = Field(default=4, validation_alias=_alias("MAX_CONTRACTS"))
    max_daily_drawdown_pct: float = Field(
        default=3.0, validation_alias=_alias("MAX_DAILY_DRAWDOWN_PCT")
    )
    max_concurrent_positions: int = Field(
        default=3, validation_alias=_alias("MAX_CONCURRENT_POSITIONS")
    )
    account_equity: float = Field(
        default=100_000.0, validation_alias=_alias("ACCOUNT_EQUITY")
    )

    # ── Tradovate (spec §4.1) ──────────────────────────────────────────────
    tradovate_base_url: str = Field(
        default="https://demo.tradovateapi.com/v1",
        validation_alias=_alias("TRADOVATE_BASE_URL"),
    )
    tradovate_username: str = Field(default="", validation_alias=_alias("TRADOVATE_USERNAME"))
    tradovate_password: str = Field(default="", validation_alias=_alias("TRADOVATE_PASSWORD"))
    tradovate_app_id: str = Field(default="Atlas", validation_alias=_alias("TRADOVATE_APP_ID"))
    tradovate_app_version: str = Field(
        default="1.0", validation_alias=_alias("TRADOVATE_APP_VERSION")
    )
    tradovate_cid: str = Field(default="", validation_alias=_alias("TRADOVATE_CID"))
    tradovate_secret: str = Field(default="", validation_alias=_alias("TRADOVATE_SECRET"))
    tradovate_device_id: str = Field(default="", validation_alias=_alias("TRADOVATE_DEVICE_ID"))
    tradovate_account_spec: str = Field(
        default="ACCOUNT_DEMO", validation_alias=_alias("TRADOVATE_ACCOUNT_SPEC")
    )
    tradovate_account_id: int = Field(default=0, validation_alias=_alias("TRADOVATE_ACCOUNT_ID"))

    # ── Kraken (spec §4.2) ─────────────────────────────────────────────────
    kraken_base_url: str = Field(
        default="https://api.kraken.com", validation_alias=_alias("KRAKEN_BASE_URL")
    )
    kraken_api_key: str = Field(default="", validation_alias=_alias("KRAKEN_API_KEY"))
    kraken_api_secret: str = Field(default="", validation_alias=_alias("KRAKEN_API_SECRET"))
    kraken_default_leverage: int = Field(
        default=3, validation_alias=_alias("KRAKEN_DEFAULT_LEVERAGE")
    )

    # ── Firebase Cloud Messaging (spec §5) ─────────────────────────────────
    fcm_project_id: str = Field(default="", validation_alias=_alias("FCM_PROJECT_ID"))
    fcm_service_account_json: str = Field(
        default="./secrets/fcm-service-account.json",
        validation_alias=_alias("FCM_SERVICE_ACCOUNT_JSON"),
    )
    fcm_default_device_token: str = Field(
        default="", validation_alias=_alias("FCM_DEFAULT_DEVICE_TOKEN")
    )

    # ── PWA / CORS ─────────────────────────────────────────────────────────
    cors_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:4173",
            "http://localhost:3000",
            "capacitor://localhost",
            "https://localhost",
        ],
        validation_alias=_alias("CORS_ORIGINS"),
    )

    # ── Research agent knobs ───────────────────────────────────────────────
    dsr_threshold: float = Field(default=1.5, validation_alias=_alias("DSR_THRESHOLD"))
    monte_carlo_draws: int = Field(default=1000, validation_alias=_alias("MONTE_CARLO_DRAWS"))

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Allow ``ATLAS_CORS_ORIGINS=a,b,c`` as well as a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return value
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @property
    def is_mock(self) -> bool:
        """True when broker clients must build/sign but never hit the network."""
        return bool(self.mock_brokers)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings instance."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cache — used by tests that mutate the environment."""
    get_settings.cache_clear()

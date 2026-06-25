import logging
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# T1 (S4): default placeholders that must never reach production. Booting with any
# of these means JWTs are signed / exchange keys encrypted with a public value.
# The encryption_key dev default is a *valid* Fernet key (so local/tests work with
# the strict SecretCipher of T2), but it is in source — hence rejected in prod too.
_DEV_DEFAULT_ENCRYPTION_KEY = "ZGV2LWluc2VjdXJlLWRlZmF1bHQtZmVybmV0LWtleSE="
_PLACEHOLDER_SECRETS: dict[str, tuple[str, ...]] = {
    "secret_key": ("change_me_to_random_secret",),
    "encryption_key": (
        "replace_with_32_urlsafe_base64_fernet_key",
        _DEV_DEFAULT_ENCRYPTION_KEY,
    ),
    "jwt_secret_key": ("change_me_to_random_jwt_secret",),
}
# Below this length a secret is brute-forceable; reject it the same as a placeholder.
_MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    app_name: str = "Trade Platform API"
    api_v1_prefix: str = "/api/v1"
    debug: bool = True
    log_level: str = "INFO"
    sql_echo: bool = False

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "trade_platform"
    postgres_user: str = "trade_user"
    postgres_password: str = "trade_password"

    # SQLAlchemy async connection pool, sized per-process. The sum across the
    # API + worker + scheduler containers must stay under Postgres
    # ``max_connections`` (100), so the default is kept small and the API — which
    # serves user traffic plus the auto-trade reconciler/WS runtime that briefly
    # holds a session across exchange I/O — is given extra headroom via the
    # DB_POOL_SIZE / DB_MAX_OVERFLOW env overrides in docker-compose.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800

    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "change_me_to_random_secret"
    encryption_key: str = _DEV_DEFAULT_ENCRYPTION_KEY
    # T2 migration: previous raw ENCRYPTION_KEY value(s), comma-separated. Supplied
    # only during key rotation so ciphertext written before T2 (sha256-derivation)
    # stays decryptable via MultiFernet. Empty in steady state.
    encryption_key_legacy: str = ""
    jwt_secret_key: str = "change_me_to_random_jwt_secret"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7
    # Step-up (2FA re-auth) token TTL — short by design: it authorizes a single
    # window of critical actions (start auto-trade, change exchange key, edit
    # risk-config, disable 2FA) and must expire quickly.
    jwt_step_up_token_expire_minutes: int = 5
    # Review I8: when True, step-up-gated critical actions REQUIRE 2FA — a user
    # without 2FA enrolled is refused (must enroll) instead of passing through.
    # Default False for back-compat (2FA is opt-in); flip on for real-money hardening.
    step_up_require_2fa: bool = False
    # 2FA brute-force lockout (C1): lock the enrollment for N minutes after this many
    # consecutive failed codes — a 6-digit TOTP is otherwise online-guessable.
    totp_max_failed_attempts: int = 5
    totp_lockout_minutes: int = 15
    # Login-2FA: TTL of the short-lived challenge token issued by /signin (password
    # OK) and exchanged at /2fa/login for the real token pair.
    login_2fa_challenge_minutes: int = 5
    # T5 (S7): fixed-window throttle for /signin and /2fa/login, per source IP and
    # per account. Fails open on Redis outage; the per-user TOTP lockout is the hard cap.
    login_rate_limit_max_attempts: int = 10
    login_rate_limit_window_seconds: int = 60
    # Max concurrent SSE event streams per user, per worker (S1) — bounds the Redis
    # subscriptions + tasks a single token-holder can hold open.
    sse_max_streams_per_user: int = 5
    exchange_http_timeout_seconds: int = 10
    exchange_max_retries: int = 3
    exchange_retry_delay_ms: int = 700
    exchange_default_page_limit: int = 100
    analysis_backend_base_url: str = "http://localhost:3001"
    analysis_backend_api_key: str = ""
    analysis_http_timeout_seconds: float = 15.0
    taskiq_stream_maxlen: int = 10000
    taskiq_result_keep_results: bool = False
    taskiq_result_ex_time_seconds: int = 1800
    taskiq_result_key_prefix: str = "taskiq:result"
    personal_analysis_status_batch_size: int = 100
    personal_analysis_max_attempts: int = 3
    personal_analysis_poll_interval_seconds: int = 60
    personal_analysis_scheduler_loop_enabled: bool = True
    # Review I4: cooldown (seconds) between acting on repeated in-position watcher
    # triggers for the same (position, indicator, action) — stops a persistent
    # condition (e.g. RSI overbought) from re-adjusting SL / partial-closing every tick.
    watcher_trigger_cooldown_seconds: int = 300
    # W8: data older than this (minutes) is flagged stale by the 4h freshness sweep.
    agent_freshness_threshold_minutes: int = 240
    # T14 (W8b): when True, the pre-trade path BLOCKS a new entry whose latest AI
    # analysis is older than the threshold (acts on staleness, not just alerts).
    # Ships OFF — safe default; enabled per the governance calibration rollout.
    agent_freshness_block_enabled: bool = False
    # W9: strategy_health_snapshots older than this are pruned by the KPI-guard sweep.
    strategy_health_snapshot_retention_days: int = 90
    auto_trade_status_batch_size: int = 100
    auto_trade_max_attempts: int = 5
    auto_trade_retry_interval_seconds: int = 60
    auto_trade_scheduler_loop_enabled: bool = True
    # Phase 1 / B2 — portfolio-DD watcher (auto-pauses ALL of a user's strategies
    # when the worst running-strategy drawdown breaches the threshold). Ships OFF:
    # it acts on real money, so the threshold must be calibrated with traders
    # before a human flips it on. Threshold is in percent (e.g. 20.0 == 20%).
    # Bounded (0, 100]: a non-positive value would mass-halt every running strategy
    # (worst_dd seeds at 0.0), so a typo must fail fast at startup, not silently.
    portfolio_dd_halt_enabled: bool = False
    portfolio_dd_halt_threshold_pct: float = Field(default=20.0, gt=0, le=100)
    # Phase 1 / B4 — how stale a strategy_health_snapshot may be before
    # compute_portfolio recomputes live KPIs request-time instead of reading it.
    # Kept above the 5-min (300s) kpi-guard cron period so a one-tick-old snapshot
    # is still "fresh": in steady state every poll reads the snapshot, and the
    # request-time recompute is reserved for brand-new strategies / a stalled cron
    # (avoids an N-recompute-per-request storm at the 300s boundary — review I2).
    kpi_freshness_seconds: int = 600
    cors_allow_origins: list[str] = ["*"]
    cors_allow_methods: list[str] = ["*"]
    cors_allow_headers: list[str] = ["*"]
    cors_allow_credentials: bool = False
    ai_forecast_exports_dir: str = "exports"
    internal_api_key: str = ""

    # Telegram trade notifications (phase 1). Empty bot token disables the
    # whole feature: the dispatcher is a no-op and the endpoints report
    # "not configured".
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""  # for deep links; auto-filled via getMe if empty
    telegram_webhook_secret: str = ""  # path segment + X-Telegram-Bot-Api-Secret-Token
    telegram_public_base_url: str = ""  # e.g. https://api.example.com — used for setWebhook
    telegram_link_code_ttl_seconds: int = 900
    telegram_notify_batch_size: int = 200
    telegram_notify_max_attempts: int = 5
    telegram_notify_lookback_minutes: int = 30
    telegram_http_timeout_seconds: float = 10.0

    # T20 (W11c): email confirmation for critical actions via Resend. Empty
    # RESEND_API_KEY (or EMAIL_FROM) disables the whole feature — endpoints report
    # "not configured" and existing flows are unchanged (accounts without
    # email-confirm keep working). EMAIL_FROM must be on a Resend-verified domain.
    resend_api_key: str = ""
    email_from: str = ""
    resend_base_url: str = "https://api.resend.com"
    email_http_timeout_seconds: float = 10.0
    email_confirm_code_ttl_minutes: int = 10

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> "Settings":
        """Fail fast in production when secrets are placeholders or too short.

        In ``debug`` mode (local/dev) the same condition is only a warning so the
        default config keeps working; production runs with ``DEBUG=false`` and a
        weak secret then refuses to boot rather than signing tokens with a public
        value (S4). ``encryption_key`` strength is tightened further in T2.
        """
        weak = [
            field
            for field, placeholders in _PLACEHOLDER_SECRETS.items()
            if (value := getattr(self, field)) in placeholders
            or len(value) < _MIN_SECRET_LENGTH
        ]
        if weak:
            message = (
                f"Insecure secret(s) {sorted(weak)}: placeholder or shorter than "
                f"{_MIN_SECRET_LENGTH} chars. Set strong values via env before "
                "starting in production."
            )
            if self.debug:
                logger.warning("%s", message)
            else:
                raise ValueError(message)
        # T6 (S9): wildcard CORS in production is a hygiene smell (mitigated by
        # cors_allow_credentials=False). Warn so prod overrides it with explicit origins.
        if not self.debug and "*" in self.cors_allow_origins:
            logger.warning(
                "CORS allow_origins is '*' in a non-debug environment; set explicit "
                "origins via CORS_ALLOW_ORIGINS in production."
            )
        return self

    @property
    def encryption_legacy_keys(self) -> tuple[str, ...]:
        """Old raw ENCRYPTION_KEY values for decrypt-only MultiFernet migration."""
        return tuple(k.strip() for k in self.encryption_key_legacy.split(",") if k.strip())

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

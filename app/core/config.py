from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "change_me_to_random_secret"
    encryption_key: str = "replace_with_32_urlsafe_base64_fernet_key"
    jwt_secret_key: str = "change_me_to_random_jwt_secret"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7
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
    auto_trade_status_batch_size: int = 100
    auto_trade_max_attempts: int = 5
    auto_trade_retry_interval_seconds: int = 60
    auto_trade_scheduler_loop_enabled: bool = True
    cors_allow_origins: list[str] = ["*"]
    cors_allow_methods: list[str] = ["*"]
    cors_allow_headers: list[str] = ["*"]
    cors_allow_credentials: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Settings for the Phase-1 risk knobs (B2 portfolio-DD watcher + B4 live-KPI).

P1-T1: the portfolio-DD watcher must ship **off by default** (real money — it
auto-pauses every strategy a user owns), and both knobs must be env-overridable
like the rest of :class:`Settings`. ``_env_file=None`` disables the dotenv file
source but env vars are still read (verified via pydantic-settings docs).
"""

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings


def _strong_secret_overrides() -> dict[str, str]:
    """Non-placeholder, sufficiently long secrets for a production-mode Settings."""
    return {
        "secret_key": "s" * 48,
        "jwt_secret_key": "j" * 48,
        "encryption_key": Fernet.generate_key().decode("utf-8"),
    }


def test_placeholder_secrets_rejected_in_production() -> None:
    # T1 (S4): the default placeholder secrets must not boot a non-debug app —
    # a forgotten env var would silently sign JWTs / encrypt keys with a public value.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, debug=False)


@pytest.mark.parametrize("field", ["secret_key", "jwt_secret_key", "encryption_key"])
def test_short_secret_rejected_in_production(field: str) -> None:
    overrides = _strong_secret_overrides()
    overrides[field] = "tooshort"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, debug=False, **overrides)


def test_strong_secrets_pass_in_production() -> None:
    settings = Settings(_env_file=None, debug=False, **_strong_secret_overrides())
    assert settings.debug is False


def test_dev_default_encryption_key_rejected_in_production() -> None:
    # T2: the dev-default encryption_key is a valid Fernet key (so local/tests work),
    # but it lives in source — production must refuse it even if the other secrets are strong.
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            debug=False,
            secret_key="s" * 48,
            jwt_secret_key="j" * 48,
        )


def test_placeholder_secrets_allowed_in_debug() -> None:
    # Local/dev (debug=True) must keep working with placeholder defaults — only a
    # warning is emitted, never a hard failure.
    settings = Settings(_env_file=None, debug=True)
    assert settings.debug is True


def test_wildcard_cors_warns_in_production(caplog: pytest.LogCaptureFixture) -> None:
    # T6 (S9): "*" CORS in prod is a hygiene smell (mitigated by credentials=False)
    # — warn loudly so it gets overridden, but don't block boot.
    with caplog.at_level("WARNING"):
        settings = Settings(
            _env_file=None, debug=False, cors_allow_origins=["*"], **_strong_secret_overrides()
        )
    assert settings.cors_allow_origins == ["*"]
    assert any("CORS" in record.message for record in caplog.records)


def test_explicit_cors_does_not_warn_in_production(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        Settings(
            _env_file=None,
            debug=False,
            cors_allow_origins=["https://app.example.com"],
            **_strong_secret_overrides(),
        )
    assert not any("CORS" in record.message for record in caplog.records)


@pytest.mark.parametrize("bad", [0.0, -5.0, 150.0])
def test_portfolio_dd_threshold_rejects_out_of_range(bad: float) -> None:
    # C1 — a non-positive or >100 threshold would mass-halt every running strategy
    # (worst_dd seeds at 0.0). Reject it at the boundary so a typo fails fast at
    # startup instead of silently pausing all trading.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, portfolio_dd_halt_threshold_pct=bad)


def test_portfolio_dd_and_kpi_freshness_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)

    # Safety-critical invariant: the auto-pause-all watcher is OFF until a human
    # enables it after calibrating the threshold with traders.
    assert settings.portfolio_dd_halt_enabled is False
    # A sane positive default exists for when it is enabled.
    assert isinstance(settings.portfolio_dd_halt_threshold_pct, float)
    assert settings.portfolio_dd_halt_threshold_pct > 0
    # B4 (I2): how stale a health snapshot may be before compute_portfolio recomputes.
    # Decoupled from the 5-min (300s) kpi-guard cron so a one-tick-old snapshot is
    # still "fresh" — avoids a per-request recompute storm at the 300s boundary.
    assert settings.kpi_freshness_seconds == 600


def test_portfolio_dd_and_kpi_freshness_load_from_env(monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIO_DD_HALT_ENABLED", "true")
    monkeypatch.setenv("PORTFOLIO_DD_HALT_THRESHOLD_PCT", "25.5")
    monkeypatch.setenv("KPI_FRESHNESS_SECONDS", "120")

    settings = Settings(_env_file=None)

    assert settings.portfolio_dd_halt_enabled is True
    assert settings.portfolio_dd_halt_threshold_pct == 25.5
    assert settings.kpi_freshness_seconds == 120

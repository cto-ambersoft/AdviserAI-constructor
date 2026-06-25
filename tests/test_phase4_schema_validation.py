"""Schema-edge validation for Phase 4 fields (review fixes I2 + S1).

Pure Pydantic validation — no DB. Guards that the API edge rejects bad values
with a 422 rather than letting them reach the DB CHECK constraint.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.auto_trade import AutoTradeConfigRead, AutoTradeRiskConfig


def _config_read_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 1,
        "user_id": 1,
        "profile_id": 1,
        "account_id": 1,
        "enabled": True,
        "is_running": False,
        "position_size_usdt": 100.0,
        "leverage": 1,
        "min_confidence_pct": 62.0,
        "fast_close_confidence_pct": 80.0,
        "confirm_reports_required": 2,
        "risk_mode": "1:2",
        "sl_pct": 1.0,
        "tp_pct": 2.0,
        "last_started_at": None,
        "last_stopped_at": None,
        "created_at": datetime(2026, 6, 18, tzinfo=UTC),
        "updated_at": datetime(2026, 6, 18, tzinfo=UTC),
    }
    base.update(overrides)
    return base


# --- I2: lifecycle_stage must be a constrained Literal, not a free string ------


@pytest.mark.parametrize(
    "stage", ["research", "sandbox", "validation", "live", "rejected", "archived"]
)
def test_lifecycle_stage_accepts_valid_stages(stage: str) -> None:
    model = AutoTradeConfigRead.model_validate(_config_read_payload(lifecycle_stage=stage))
    assert model.lifecycle_stage == stage


def test_lifecycle_stage_defaults_to_live() -> None:
    model = AutoTradeConfigRead.model_validate(_config_read_payload())
    assert model.lifecycle_stage == "live"


def test_lifecycle_stage_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        AutoTradeConfigRead.model_validate(_config_read_payload(lifecycle_stage="bogus"))


# --- S1: anomaly_window must have a sane upper bound --------------------------


def test_anomaly_window_accepts_in_range() -> None:
    cfg = AutoTradeRiskConfig(anomaly_window=20)
    assert cfg.anomaly_window == 20


def test_anomaly_window_rejects_below_minimum() -> None:
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(anomaly_window=1)


def test_anomaly_window_rejects_above_maximum() -> None:
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(anomaly_window=5000)


# --- T13 (W8c): conflicting-signal policy drops the interface-only net/replace ---


@pytest.mark.parametrize("policy", ["off", "block_opposite"])
def test_conflicting_signal_policy_accepts_enforced_values(policy: str) -> None:
    cfg = AutoTradeRiskConfig(conflicting_signal_policy=policy)
    assert cfg.conflicting_signal_policy == policy


@pytest.mark.parametrize("policy", ["net", "replace"])
def test_conflicting_signal_policy_rejects_unimplemented_values(policy: str) -> None:
    # net/replace were never enforced (interface-only); they are removed so the UI
    # / API can't offer a silently-ignored option.
    with pytest.raises(ValidationError):
        AutoTradeRiskConfig(conflicting_signal_policy=policy)

import pytest
from pydantic import ValidationError

from app.schemas.strategy_profile import (
    STRATEGY_PROFILE_ADJUSTMENT_KEYS,
    StrategyProfileConfig,
)


def _single_tp_payload() -> dict[str, object]:
    return {
        "sl_value": 1.5,
        "tp_value": 3.0,
    }


def test_strategy_profile_config_defaults_for_single_tp() -> None:
    profile = StrategyProfileConfig.model_validate(_single_tp_payload())

    assert profile.sl_mode == "fixed"
    assert profile.tp_mode == "single"
    assert profile.tp_value == pytest.approx(3.0)
    assert profile.tp_levels is None
    assert profile.trailing_enabled is False
    assert profile.trailing_callback_rate == pytest.approx(1.0)
    assert profile.breakeven_enabled is False
    assert profile.breakeven_trigger_rr == pytest.approx(1.0)
    assert profile.volatility_sl_enabled is False
    assert profile.volatility_atr_period == 14
    assert profile.volatility_atr_multiplier == pytest.approx(2.0)
    assert profile.watchers == []
    assert profile.adjustment_priority == list(STRATEGY_PROFILE_ADJUSTMENT_KEYS)
    assert profile.max_position_pct == pytest.approx(100.0)
    assert profile.allow_sl_widen is False


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("trailing_callback_rate", 0.09, "trailing_callback_rate must be between 0.1 and 10.0."),
        ("trailing_callback_rate", 10.1, "trailing_callback_rate must be between 0.1 and 10.0."),
        ("breakeven_trigger_rr", 0.49, "breakeven_trigger_rr must be between 0.5 and 5.0."),
        ("breakeven_trigger_rr", 5.01, "breakeven_trigger_rr must be between 0.5 and 5.0."),
        (
            "volatility_atr_multiplier",
            0.49,
            "volatility_atr_multiplier must be between 0.5 and 5.0.",
        ),
        (
            "volatility_atr_multiplier",
            5.01,
            "volatility_atr_multiplier must be between 0.5 and 5.0.",
        ),
    ],
)
def test_strategy_profile_config_rejects_out_of_range_values(
    field_name: str,
    value: float,
    message: str,
) -> None:
    payload = _single_tp_payload()
    payload[field_name] = value

    with pytest.raises(ValidationError, match=message):
        StrategyProfileConfig.model_validate(payload)


def test_strategy_profile_config_accepts_multi_tp_with_close_pct_sum_near_100() -> None:
    profile = StrategyProfileConfig.model_validate(
        {
            "sl_mode": "atr",
            "sl_value": 2.0,
            "tp_mode": "multi",
            "tp_levels": [
                {"price_offset_pct": 1.5, "close_pct": 33.3, "move_sl_to": "breakeven"},
                {"price_offset_pct": 3.0, "close_pct": 33.3, "move_sl_to": "tp1"},
                {"price_offset_pct": 5.0, "close_pct": 33.4, "move_sl_to": None},
            ],
            "watchers": [
                {
                    "indicator": "rsi",
                    "params": {"period": 14, "timeframe": "15m"},
                    "condition": "> 75",
                    "action": "tighten_sl",
                    "action_params": {"sl_offset_atr": 1.5},
                    "is_active": True,
                }
            ],
            "adjustment_priority": ["watcher", "trailing", "breakeven", "volatility"],
        }
    )

    assert [level.close_pct for level in profile.tp_levels or []] == pytest.approx(
        [33.3, 33.3, 33.4]
    )
    assert [level.move_sl_to for level in profile.tp_levels or []] == ["breakeven", "tp1", None]
    assert profile.watchers[0].indicator == "RSI"


def test_strategy_profile_config_rejects_multi_tp_when_close_pct_sum_is_not_100() -> None:
    payload = {
        "sl_value": 1.0,
        "tp_mode": "multi",
        "tp_levels": [
            {"price_offset_pct": 1.5, "close_pct": 33.0, "move_sl_to": "breakeven"},
            {"price_offset_pct": 3.0, "close_pct": 33.0, "move_sl_to": "tp1"},
            {"price_offset_pct": 5.0, "close_pct": 33.0, "move_sl_to": None},
        ],
    }

    with pytest.raises(
        ValidationError,
        match=r"tp_levels close_pct must sum to 100% \(\+/- 0\.1\) when tp_mode='multi'\.",
    ):
        StrategyProfileConfig.model_validate(payload)


def test_strategy_profile_config_rejects_invalid_adjustment_priority_key() -> None:
    payload = _single_tp_payload()
    payload["adjustment_priority"] = ["watcher", "invalid-step"]

    with pytest.raises(ValidationError, match="adjustment_priority contains invalid keys"):
        StrategyProfileConfig.model_validate(payload)


def test_strategy_profile_config_rejects_duplicate_adjustment_priority_key() -> None:
    payload = _single_tp_payload()
    payload["adjustment_priority"] = ["watcher", "trailing", "watcher"]

    with pytest.raises(ValidationError, match="adjustment_priority must not contain duplicates"):
        StrategyProfileConfig.model_validate(payload)


def test_strategy_profile_config_rejects_invalid_tp_move_sl_to_reference() -> None:
    payload = {
        "sl_value": 1.0,
        "tp_mode": "multi",
        "tp_levels": [
            {"price_offset_pct": 2.0, "close_pct": 50.0, "move_sl_to": "breakeven"},
            {"price_offset_pct": 4.0, "close_pct": 50.0, "move_sl_to": "take-profit-1"},
        ],
    }

    with pytest.raises(ValidationError, match="move_sl_to must be null, 'breakeven'"):
        StrategyProfileConfig.model_validate(payload)


def test_strategy_profile_config_requires_tp_value_for_single_mode() -> None:
    with pytest.raises(ValidationError, match="tp_value is required when tp_mode='single'"):
        StrategyProfileConfig.model_validate({"sl_value": 1.0})

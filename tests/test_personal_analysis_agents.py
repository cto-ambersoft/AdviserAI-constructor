"""Regression: the systemic-risk agent (core internal key `riskControl`, public
code `RC`) must be a supported personal-analysis agent so users can enable,
disable and weight it and so it is forwarded to core."""
import pytest

from app.schemas.personal_analysis import (
    PERSONAL_ANALYSIS_AGENT_NAMES,
    get_personal_analysis_defaults,
    normalize_agents_and_weights,
)


def test_risk_control_is_a_supported_agent() -> None:
    assert "riskControl" in PERSONAL_ANALYSIS_AGENT_NAMES


def test_defaults_enable_and_weight_risk_control() -> None:
    agents, weights = get_personal_analysis_defaults()
    assert agents["riskControl"] is True
    assert weights["riskControl"] == 1.0


def test_normalize_accepts_risk_control_selection_and_weight() -> None:
    agents, weights = normalize_agents_and_weights(
        agents={"riskControl": True, "techModelSignal": False},
        agent_weights={"riskControl": 0.7},
    )
    assert agents["riskControl"] is True
    # unspecified agents default to disabled when an explicit selection is given
    assert agents["techModelSignal"] is False
    assert weights["riskControl"] == 0.7


def test_risk_control_weight_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match=r"riskControl"):
        normalize_agents_and_weights(agents=None, agent_weights={"riskControl": 1.5})

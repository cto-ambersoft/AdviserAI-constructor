from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

PERSONAL_ANALYSIS_AGENT_NAMES = (
    "twitterSentiment",
    "researchFundamental",
    "researchQuantitative",
    "newsSearch",
    "techModelSignal",
    "binanceRealtime",
)


def _default_agents() -> dict[str, bool]:
    return {name: True for name in PERSONAL_ANALYSIS_AGENT_NAMES}


def _default_weights() -> dict[str, float]:
    return {name: 1.0 for name in PERSONAL_ANALYSIS_AGENT_NAMES}


def get_personal_analysis_defaults() -> tuple[dict[str, bool], dict[str, float]]:
    return _default_agents(), _default_weights()


def normalize_agents_and_weights(
    *,
    agents: dict[str, bool] | None,
    agent_weights: dict[str, float] | None,
) -> tuple[dict[str, bool], dict[str, float]]:
    normalized_agents = _default_agents() if agents is None else dict(agents)
    unknown_agents = sorted(set(normalized_agents.keys()) - set(PERSONAL_ANALYSIS_AGENT_NAMES))
    if unknown_agents:
        raise ValueError(f"Unknown agents: {', '.join(unknown_agents)}")

    for agent_name in PERSONAL_ANALYSIS_AGENT_NAMES:
        normalized_agents.setdefault(agent_name, False if agents is not None else True)

    enabled_agents = [name for name, enabled in normalized_agents.items() if bool(enabled)]
    if not enabled_agents:
        raise ValueError("At least one agent must be enabled.")

    raw_weights = _default_weights() if agent_weights is None else dict(agent_weights)
    unknown_weights = sorted(set(raw_weights.keys()) - set(PERSONAL_ANALYSIS_AGENT_NAMES))
    if unknown_weights:
        raise ValueError(f"Unknown agent_weights keys: {', '.join(unknown_weights)}")

    normalized_weights: dict[str, float] = {}
    for agent_name in PERSONAL_ANALYSIS_AGENT_NAMES:
        weight = float(raw_weights.get(agent_name, 1.0))
        if weight < 0.0 or weight > 1.0:
            raise ValueError(f"agent_weights.{agent_name} must be in range [0.0, 1.0].")
        normalized_weights[agent_name] = weight
    return normalized_agents, normalized_weights


class PersonalAnalysisProfileCreate(BaseModel):
    symbol: str = Field(min_length=3, max_length=24)
    query_prompt: str | None = Field(default=None, max_length=10_000)
    agents: dict[str, bool] | None = None
    agent_weights: dict[str, float] | None = None
    interval_minutes: int = Field(default=60, ge=5, le=1440)

    @model_validator(mode="after")
    def validate_agents(self) -> "PersonalAnalysisProfileCreate":
        self.agents, self.agent_weights = normalize_agents_and_weights(
            agents=self.agents,
            agent_weights=self.agent_weights,
        )
        return self


class PersonalAnalysisProfileUpdate(BaseModel):
    symbol: str | None = Field(default=None, min_length=3, max_length=24)
    query_prompt: str | None = Field(default=None, max_length=10_000)
    agents: dict[str, bool] | None = None
    agent_weights: dict[str, float] | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "PersonalAnalysisProfileUpdate":
        if not self.model_dump(exclude_none=True):
            raise ValueError("At least one field must be provided for update.")

        if self.agents is not None:
            unknown_agents = sorted(set(self.agents.keys()) - set(PERSONAL_ANALYSIS_AGENT_NAMES))
            if unknown_agents:
                raise ValueError(f"Unknown agents: {', '.join(unknown_agents)}")
            if not any(bool(enabled) for enabled in self.agents.values()):
                raise ValueError("At least one agent must be enabled.")

        if self.agent_weights is not None:
            unknown_weights = sorted(
                set(self.agent_weights.keys()) - set(PERSONAL_ANALYSIS_AGENT_NAMES)
            )
            if unknown_weights:
                raise ValueError(f"Unknown agent_weights keys: {', '.join(unknown_weights)}")
            for agent_name, weight in self.agent_weights.items():
                float_weight = float(weight)
                if float_weight < 0.0 or float_weight > 1.0:
                    raise ValueError(f"agent_weights.{agent_name} must be in range [0.0, 1.0].")
        return self


class PersonalAnalysisManualTriggerRequest(BaseModel):
    query_prompt: str | None = Field(default=None, max_length=10_000)
    agents: dict[str, bool] | None = None
    agent_weights: dict[str, float] | None = None

    @model_validator(mode="after")
    def validate_overrides(self) -> "PersonalAnalysisManualTriggerRequest":
        if self.agents is not None:
            unknown_agents = sorted(set(self.agents.keys()) - set(PERSONAL_ANALYSIS_AGENT_NAMES))
            if unknown_agents:
                raise ValueError(f"Unknown agents: {', '.join(unknown_agents)}")
            if not any(bool(enabled) for enabled in self.agents.values()):
                raise ValueError("At least one agent must be enabled.")

        if self.agent_weights is not None:
            unknown_weights = sorted(
                set(self.agent_weights.keys()) - set(PERSONAL_ANALYSIS_AGENT_NAMES)
            )
            if unknown_weights:
                raise ValueError(f"Unknown agent_weights keys: {', '.join(unknown_weights)}")
            for agent_name, weight in self.agent_weights.items():
                float_weight = float(weight)
                if float_weight < 0.0 or float_weight > 1.0:
                    raise ValueError(f"agent_weights.{agent_name} must be in range [0.0, 1.0].")
        return self


class PersonalAnalysisProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    symbol: str
    query_prompt: str | None
    agents: dict[str, bool]
    agent_weights: dict[str, float]
    interval_minutes: int
    is_active: bool
    next_run_at: datetime
    last_triggered_at: datetime | None
    last_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PersonalAnalysisManualTriggerResponse(BaseModel):
    trade_job_id: str
    core_job_id: str
    status: str
    created_at: datetime


class PersonalAnalysisDefaultsRead(BaseModel):
    available_agents: list[str]
    agents: dict[str, bool]
    agent_weights: dict[str, float]


class PersonalAnalysisJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: int
    profile_id: int
    core_job_id: str
    status: str
    attempt: int
    max_attempts: int
    error: str | None
    next_poll_at: datetime
    completed_at: datetime | None
    core_deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PersonalAnalysisHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    profile_id: int
    trade_job_id: str
    symbol: str
    analysis_data: dict[str, Any]
    core_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

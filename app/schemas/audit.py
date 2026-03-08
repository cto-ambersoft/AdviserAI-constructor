from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

AUDIT_EVENTS = (
    "BUILDER_CHANGE",
    "SAVE_STRATEGY",
    "UPDATE_STRATEGY",
    "INDICATORS_CHANGE",
    "CLEAR_AUDIT_LOG",
    "PORTFOLIO_RUN",
)
AUDIT_TARGET_TYPES = ("system", "strategy", "portfolio", "backtest")
AUDIT_DEFAULT_TARGET_TYPE = "system"
AUDIT_DEFAULT_TARGET_ID = "n/a"
AUDIT_GLOBAL_ACTORS = ("system", "global", "__system__")


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor: str
    event: str
    reason: str
    target_type: str
    target_id: str
    payload: dict[str, object]
    created_at: datetime
    updated_at: datetime


class AuditLogCreate(BaseModel):
    event: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=512)
    target_type: str = Field(default=AUDIT_DEFAULT_TARGET_TYPE, min_length=1, max_length=64)
    target_id: str = Field(default=AUDIT_DEFAULT_TARGET_ID, min_length=1, max_length=64)
    payload: dict[str, object] = Field(default_factory=dict)


class AuditMetaResponse(BaseModel):
    suggested_events: list[str]
    suggested_target_types: list[str]
    default_target_type: str
    default_target_id: str
    list_limit_default: int
    list_limit_min: int
    list_limit_max: int

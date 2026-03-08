from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

STRATEGY_TYPES = (
    "builder_vwap",
    "atr_order_block",
    "knife_catcher",
    "grid_bot",
    "intraday_momentum",
)
STRATEGY_DEFAULT_TYPE = "builder_vwap"
STRATEGY_DEFAULT_VERSION = "1.0.0"


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    strategy_type: str = Field(default=STRATEGY_DEFAULT_TYPE, min_length=1, max_length=64)
    version: str = Field(default=STRATEGY_DEFAULT_VERSION, min_length=1, max_length=32)
    description: str | None = None
    is_active: bool = True
    config: dict[str, object] = Field(default_factory=dict)


class StrategyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    strategy_type: str | None = Field(default=None, min_length=1, max_length=64)
    version: str | None = Field(default=None, min_length=1, max_length=32)
    description: str | None = None
    is_active: bool | None = None
    config: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_non_empty(self) -> "StrategyUpdate":
        if self.model_dump(exclude_none=True):
            return self
        raise ValueError("At least one field must be provided for update.")


class StrategyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    strategy_type: str
    version: str
    description: str | None
    is_active: bool
    config: dict[str, object]
    created_at: datetime
    updated_at: datetime


class StrategyMetaResponse(BaseModel):
    supported_strategy_types: list[str]
    default_strategy_type: str
    default_version: str
    name_min_length: int
    name_max_length: int
    strategy_type_min_length: int
    strategy_type_max_length: int
    version_min_length: int
    version_max_length: int

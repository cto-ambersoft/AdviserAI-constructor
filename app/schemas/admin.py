from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AdminUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    is_active: bool
    is_admin: bool
    created_at: datetime
    updated_at: datetime


class AdminStrategyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    strategy_type: str
    version: str
    description: str | None
    is_active: bool
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AdminAutoTradeConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    profile_id: int
    account_id: int
    enabled: bool
    is_running: bool
    position_size_usdt: float
    leverage: int
    min_confidence_pct: float
    fast_close_confidence_pct: float
    confirm_reports_required: int
    risk_mode: str
    sl_pct: float
    tp_pct: float
    last_started_at: datetime | None
    last_stopped_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminAutoTradePositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    config_id: int
    profile_id: int
    account_id: int
    symbol: str
    side: str
    status: str
    entry_price: float
    quantity: float
    position_size_usdt: float
    leverage: int
    tp_price: float
    sl_price: float
    entry_confidence_pct: float
    opened_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    close_price: float | None
    open_order_id: str | None
    close_order_id: str | None
    open_history_id: int | None
    close_history_id: int | None
    created_at: datetime
    updated_at: datetime


class AdminLivePaperProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    strategy_id: int
    strategy_revision: int
    is_running: bool
    total_balance_usdt: float
    per_trade_usdt: float
    last_processed_at: datetime | None
    last_poll_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminUserRuntimeStatsRead(BaseModel):
    total_strategies: int = Field(ge=0)
    active_strategies: int = Field(ge=0)
    auto_trade_configs: int = Field(ge=0)
    running_auto_trade_configs: int = Field(ge=0)
    auto_trade_positions: int = Field(ge=0)
    open_auto_trade_positions: int = Field(ge=0)
    live_paper_running: bool = False


class AdminUserRuntimeRead(BaseModel):
    user: AdminUserRead
    stats: AdminUserRuntimeStatsRead
    strategies_truncated: bool = False
    auto_trade_configs_truncated: bool = False
    auto_trade_positions_truncated: bool = False
    strategies: list[AdminStrategyRead] = Field(default_factory=list)
    auto_trade_configs: list[AdminAutoTradeConfigRead] = Field(default_factory=list)
    auto_trade_positions: list[AdminAutoTradePositionRead] = Field(default_factory=list)
    live_paper_profile: AdminLivePaperProfileRead | None = None


class AdminRuntimeSummaryRead(BaseModel):
    total_users: int = Field(ge=0)
    active_users: int = Field(ge=0)
    admin_users: int = Field(ge=0)
    total_strategies: int = Field(ge=0)
    active_strategies: int = Field(ge=0)
    total_auto_trade_configs: int = Field(ge=0)
    running_auto_trade_configs: int = Field(ge=0)
    total_auto_trade_positions: int = Field(ge=0)
    open_auto_trade_positions: int = Field(ge=0)
    running_live_paper_profiles: int = Field(ge=0)


class AdminRuntimePageRead(BaseModel):
    users_limit: int = Field(ge=1)
    after_user_id: int | None = Field(default=None, ge=1)
    next_after_user_id: int | None = Field(default=None, ge=1)
    has_more: bool = False


class AdminRuntimeSnapshotResponse(BaseModel):
    generated_at: datetime
    summary: AdminRuntimeSummaryRead
    page: AdminRuntimePageRead
    users: list[AdminUserRuntimeRead] = Field(default_factory=list)

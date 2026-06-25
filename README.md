# Trade Platform (FastAPI, 2025 Stack)

Production-ready starter for a trading backend with clear separation of API, business logic, and infrastructure.

## Stack

- Dependency management: `uv`
- API: `FastAPI` + `Pydantic v2`
- Database: `PostgreSQL` + `SQLAlchemy 2.0 (async)` + `Alembic`
- Background jobs: `Taskiq` + `Redis`
- Quality: `Ruff` + `Mypy`
- Tests: `Pytest` + `pytest-asyncio` + `HTTPX`

## Project structure

```text
trade_platform/
├── app/
│   ├── api/
│   │   └── v1/
│   │       ├── endpoints/
│   │       └── router.py
│   ├── core/
│   ├── db/
│   ├── models/
│   ├── schemas/
│   ├── services/
│   ├── worker/
│   └── main.py
├── migrations/
├── tests/
├── .env.example
├── pyproject.toml
└── docker-compose.yml
```

## Quick start with uv

1. Install Python and dependencies:
   - `uv sync`
2. Copy environment:
   - `cp .env.example .env`
3. Apply DB migrations:
   - `uv run alembic upgrade head`
4. Run API in dev mode:
   - `uv run uvicorn app.main:app --reload`
5. Run worker:
   - `uv run taskiq worker app.worker.broker:broker app.worker.tasks`
6. Run scheduler:
   - `uv run taskiq scheduler app.worker.scheduler:scheduler app.worker.tasks`
7. Run API in production style:
   - `uv run gunicorn -k uvicorn.workers.UvicornWorker app.main:app -w 4 -b 0.0.0.0:8000`

## Quality gates

- Format: `uv run ruff format .`
- Lint: `uv run ruff check .`
- Type check: `uv run mypy app`
- Tests: `uv run pytest`

## Migrations

- Create migration:
  - `uv run alembic revision --autogenerate -m "init"`
- Apply migrations:
  - `uv run alembic upgrade head`

## Backtest API contracts

- `POST /api/v1/backtest/vwap`
- `GET /api/v1/backtest/ai-forecast-files`
- `GET /api/v1/backtest/vwap/indicators`
- `GET /api/v1/backtest/vwap/presets`
- `GET /api/v1/backtest/vwap/regimes`
- `GET /api/v1/backtest/catalog`
- `POST /api/v1/backtest/atr-order-block`
- `POST /api/v1/backtest/knife-catcher`
- `POST /api/v1/backtest/grid-bot`
- `POST /api/v1/backtest/intraday-momentum`
- `POST /api/v1/backtest/portfolio`
- `POST /api/v1/live/signals/builder` (legacy, no paper mode)
- `POST /api/v1/live/signals/atr-order-block` (legacy, no paper mode)
- `PUT /api/v1/live/paper/profile`
- `POST /api/v1/live/paper/play`
- `POST /api/v1/live/paper/stop`
- `GET /api/v1/live/paper/poll`
- `GET /api/v1/live/auto-trade/config`
- `PUT /api/v1/live/auto-trade/config`
- `POST /api/v1/live/auto-trade/play`
- `POST /api/v1/live/auto-trade/stop`
- `GET /api/v1/live/auto-trade/state`
- `GET /api/v1/live/auto-trade/events`
- `GET /api/v1/live/auto-trade/trades`
- `POST /api/v1/live/auto-trade/close-positions`
- `POST /api/v1/analysis/trigger-now`
- `GET /api/v1/analysis/runs`
- `GET /api/v1/analysis/runs?limit=1`
- `GET /api/v1/analysis/runs?date=YYYY-MM-DD&limit=50`
- `GET /api/v1/analysis/:symbol`
- `GET /api/v1/analysis/market-state`
- `GET /api/v1/analysis/personal/profiles`
- `GET /api/v1/analysis/personal/defaults`
- `POST /api/v1/analysis/personal/profiles`
- `PUT /api/v1/analysis/personal/profiles/:profile_id`
- `DELETE /api/v1/analysis/personal/profiles/:profile_id`
- `POST /api/v1/analysis/personal/profiles/:profile_id/trigger`
- `GET /api/v1/analysis/personal/jobs/:trade_job_id`
- `GET /api/v1/analysis/personal/history`
- `GET /api/v1/analysis/personal/latest`

Market data source behavior for backtests:

- `candles` in request body is optional.
- If `candles` is omitted, backend loads OHLCV internally via exchange using `symbol`, `timeframe`, and `bars`.
- If `candles` is provided, backend uses it as an explicit override (useful for deterministic replay or client-owned datasets).

VWAP indicator selection behavior:

- Send `enabled` with indicator names to run an explicit custom set.
- If `enabled` is empty, backend derives the set from `preset`.
- Unknown indicator names are rejected with `422` validation error.
- VWAP supports stop modes: `ATR`, `Swing`, `Order Block (ATR-OB)`.
- VWAP risk sizing supports `max_position_pct` cap and returns per-trade `sl_explain`.
- Optional AI forecast integration for VWAP:
  - `run_with_ai` (default `false`)
  - `ai_forecast_file` (required if `run_with_ai=true`, must be CSV from `exports`)
  - `ai_bull_confidence_threshold` and `ai_bear_confidence_threshold` in range `0..100`
  - when thresholds are not provided, runtime defaults are `52.0` for bull and bear
  - AI CSV must include: `signal_time_utc`, `predicted_trend`, `confidence_bull`, `confidence_bear`, `confidence_flat`

AI forecast file catalog:

- `GET /api/v1/backtest/ai-forecast-files` returns all available `.csv` files from `exports`:
  - `file_name`
  - `modified_at_utc`
- In production (Docker/server), set `AI_FORECAST_EXPORTS_DIR` if files are stored outside the default working directory:
  - example: `AI_FORECAST_EXPORTS_DIR=/home/ubuntu/adviser-ai/trade/exports`
  - relative values are resolved from current process working directory.

VWAP response behavior with AI enabled:

- If `run_with_ai=false`, response shape stays unchanged:
  - `summary`, `trades`, `chart_points`, `explanations`
- If `run_with_ai=true`, endpoint returns compact comparison payload:
  - `result`: final result with per-bar AI regime override
  - `baseline`: result without AI override (with stripped `chart_points` to reduce payload size)
  - `comparison`: precomputed deltas (`total_pnl_delta`, `win_rate_delta`, `trades_delta`)
- In AI mode, strategy resolves regime for each bar from latest AI signal (`signal_time_utc <= bar_time`):
  - bars before the first AI signal fall back to request `regime`
  - low-confidence `bull`/`bear` predictions fall back to request `regime`
  - `flat` predictions also respect confidence (`confidence_flat`) and fall back to request `regime` when confidence is low
  - `bull` AI blocks shorts, `bear` AI blocks longs

Example: list AI forecast files

```http
GET /api/v1/backtest/ai-forecast-files
```

```json
{
  "files": [
    {
      "file_name": "ai_forecast_backtest_btc_1h.csv",
      "modified_at_utc": "2026-03-24T08:21:55+00:00"
    }
  ]
}
```

Example: run VWAP with AI comparison

```http
POST /api/v1/backtest/vwap
Content-Type: application/json
```

```json
{
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "bars": 500,
  "regime": "Flat",
  "preset": "Custom",
  "enabled": ["EMA Fast (21)", "EMA Slow (50)", "VWAP", "MACD", "ATR"],
  "run_with_ai": true,
  "ai_forecast_file": "ai_forecast_backtest_btc_1h.csv",
  "ai_bull_confidence_threshold": 70.0,
  "ai_bear_confidence_threshold": 70.0
}
```

```json
{
  "result": {
    "summary": {},
    "trades": [],
    "chart_points": {},
    "explanations": []
  },
  "baseline": {
    "summary": {},
    "trades": [],
    "chart_points": {},
    "explanations": []
  },
  "comparison": {
    "total_pnl_delta": 0.0,
    "win_rate_delta": 0.0,
    "trades_delta": 0
  }
}
```

Strategy sizing/capital behavior:

- ATR Order-Block accepts `allocation_usdt` and returns `pnl_usdt`.
- Grid Bot supports both `initial_capital_usdt` and optional `order_size_usdt`.
- Grid Bot can close remaining open positions at end-of-data via `close_open_positions_on_eod`.
- Intraday Momentum supports optional fixed `entry_size_usdt` (fallback: risk-based sizing).

Backtest catalog behavior:

- `GET /api/v1/backtest/catalog` returns UI metadata for all strategy forms:
  - supported timeframes per strategy
  - VWAP presets, regimes, and indicators
  - knife-catcher side and entry mode options
  - portfolio built-in strategy names

Unified response shape:

- `summary` - aggregate metrics
- `trades` - normalized trade rows
- `chart_points` - compact points for frontend charting
- `explanations` - decision/reporting context

State and audit endpoints:

- `GET/POST/DELETE /api/v1/strategies`
- `GET /api/v1/strategies/meta`
- `GET /api/v1/audit`
- `POST /api/v1/audit/events`
- `GET /api/v1/audit/meta`
- `GET /api/v1/market/ohlcv`
- `GET /api/v1/market/meta`
- `POST /api/v1/live/signals/builder` (legacy, no paper mode)
- `POST /api/v1/live/signals/atr-order-block` (legacy, no paper mode)
- `PUT /api/v1/live/paper/profile`
- `POST /api/v1/live/paper/play`
- `POST /api/v1/live/paper/stop`
- `GET /api/v1/live/paper/poll`

Live paper mode behavior:

- Paper execution for legacy `/live/signals/*` removed.
- Stateful paper flow uses `/live/paper/*` only.
- Profile is singleton per user and stores both:
  - `total_balance_usdt` (total trade balance)
  - `per_trade_usdt` (position entry size)
- `per_trade_usdt` must be `<= total_balance_usdt`.
- Entry size is configured in one place (`/live/paper/profile`) and no longer passed via execution `entry_usdt`.

Analysis proxy configuration:

- Analysis routes proxy downstream backend responses 1:1 and keep downstream status codes.
- Configure downstream via env:
  - `ANALYSIS_BACKEND_BASE_URL` (default `http://localhost:3001`)
  - `ANALYSIS_BACKEND_API_KEY` (forwarded as `X-API-Key`)
- `ANALYSIS_HTTP_TIMEOUT_SECONDS` (request timeout for downstream calls)

Personal analysis pipeline configuration:

- `PERSONAL_ANALYSIS_STATUS_BATCH_SIZE` (default `100`)
- `PERSONAL_ANALYSIS_MAX_ATTEMPTS` (default `3`)
- `PERSONAL_ANALYSIS_POLL_INTERVAL_SECONDS` (default `60`)
- `PERSONAL_ANALYSIS_SCHEDULER_LOOP_ENABLED` (default `true`)
- `AUTO_TRADE_STATUS_BATCH_SIZE` (default `100`)
- `AUTO_TRADE_MAX_ATTEMPTS` (default `5`)
- `AUTO_TRADE_RETRY_INTERVAL_SECONDS` (default `60`)
- `AUTO_TRADE_SCHEDULER_LOOP_ENABLED` (default `true`)
- `TASKIQ_STREAM_MAXLEN` (default `10000`)
- `TASKIQ_RESULT_KEEP_RESULTS` (default `false`)
- `TASKIQ_RESULT_EX_TIME_SECONDS` (default `1800`)
- `TASKIQ_RESULT_KEY_PREFIX` (default `taskiq:result`)

Auto-trade exchange ledger behavior:

- Exchange fills are synchronized into local DB as canonical source-of-truth.
- New storage tables:
  - `exchange_trade_ledger` - normalized exchange fills with origin markers (`platform|external|unknown`).
  - `exchange_trade_sync_state` - per `(account_id, symbol, market_type)` high-water marks for incremental sync.
  - `exchange_order_metadata` - order provenance map written by auto-trade execution.
- Sync strategy:
  - backfill for managed auto-trade symbols (30 days on first sync),
  - incremental sync with overlap window and idempotent upsert to avoid misses/duplicates.
- Auto-trade runtime writes deterministic `client_order_id` and records metadata for robust reconciliation.
- Background task `sync_auto_trade_exchange_trades` runs every minute via Taskiq scheduler.
- `GET /api/v1/live/auto-trade/trades` returns synchronized ledger rows and summary.

Operational notes:

- `auto_trade_positions` remains as strategy lifecycle state (open/close/reason/risk context).
- `exchange_trade_ledger` remains execution truth from exchange.
- Keep both worker and scheduler running for continuous sync.

Multi-TP SL repositioning (per-level `sl_lock_pct`):

- Each `tp_level` may declare an SL move directive that fires when the level
  fills. Two equivalent forms:
  - `sl_lock_pct` (preferred, numeric): signed % of the entry→TP interval to
    lock as the new SL on the remaining quantity. `0` = breakeven, `50` =
    halfway, `100` = SL at TP price, `-50` = halfway between entry and the
    original SL (loosens risk relative to breakeven). Formula:
    `new_SL = entry + (TP_price − entry) × sl_lock_pct / 100`.
  - `move_sl_to` (legacy string): `"breakeven"`, `"tpN"` (e.g. `"tp1"`), or
    `"none"` to opt out of any SL move.
- Validation is strict at strategy save time: in `tp_mode="multi"`, every
  level except the last must declare one of the two directives. The literal
  string `move_sl_to="none"` is the explicit opt-out — leaving both fields
  null is rejected with HTTP 422 to eliminate the silent-no-op failure mode.
- **Last-level semantics**: when the final declared TP fires, the position
  is fully closing — there is nothing left for an SL to protect. The runtime
  therefore **does not** enqueue a `replace_sl` for the final level even if
  it carries an `sl_lock_pct` / `move_sl_to` directive (informational only).
  Instead it emits an `sl_adjustment_skipped` audit event with reason
  `last_level`, and the WS-manager-side cleanup cancels the remaining
  conditional orders. This avoids the previous failure mode where the
  engine enqueued `replace_sl(quantity=0)` against Binance, which is
  rejected by the LOT_SIZE filter, escalated to an emergency-close that
  also failed, and raced the synchronous SL cancel — leaving the
  operator with "TP3 + SL слетели".
- **Replacement SL is position-attached**: the multi-TP path issues
  `replace_sl` with `close_position=True`. Binance maps that to
  `closePosition=true` (no `quantity` / `reduceOnly` fields) and
  auto-tracks the live position size; Bybit maps it to `tpslMode: "Full"`.
  Trailing / breakeven / volatility flows pass `close_position=False`
  explicitly when they need a sliced SL.
- Run `uv run python -m scripts.audit_strategy_profiles` to list any stored
  profiles that violate the per-level-directive rule before they are next
  saved.

Auto-trade restart resilience:

- The FastAPI lifespan now calls `AutoTradeService.hydrate_active_positions`
  on startup and every 60 s. Open `AutoTradePosition` rows are reloaded into
  the per-account `WebSocketManager` registry, so TP/SL fills delivered after
  a restart are routed correctly. Previously the registry was empty after
  restart and the SL repositioning code path never ran.
- `WebSocketManager.track_position` proactively kicks off the realtime SL
  pipeline (kline subscription) for `OPEN` positions that need trailing /
  breakeven / volatility, so hydrated positions get the same coverage as
  freshly opened ones.

Auto-trade observability events (`GET /api/v1/live/auto-trade/events`):

- `sl_adjustment_decided` — multi-TP fill triggered an SL move (level, new
  price, current SL). Info level.
- `sl_adjustment_skipped` — SL was not moved (reason: `lock_pct_null`,
  `no_change`, `sl_order_id_missing`, etc.). Warning level.
- `sl_adjustment_dispatched` — `replace_sl` task enqueued. Info level.
- `tp_fill_unmatched` — fill event could not be mapped to any TP level. Error
  level. Includes the full level snapshot for debugging.
- `multi_tp_inferred_from_position_update` — partial-close reconciler had to
  synthesize a TP advancement because the order topic did not deliver the
  fill within `PARTIAL_CLOSE_RECONCILE_DELAY_SECONDS`. Warning level.
- `order_task_fatal_error` — non-transient adapter failure on a queued order
  task. Error level. SL/`replace_sl` failures additionally enqueue an
  `emergency_market_close` so the position is not left unprotected.
- `strategy_profile_validation_failed` — persisted profile JSON failed
  validation; per-level config will be dropped at runtime. Error level.
- `position_manual_closed` — single position flattened via the manual
  close-positions endpoint. Info level.
- `position_manual_close_failed` — adapter rejected the manual market
  close. Error level. Cancels of TP/SL still attempted before the failure.
- `auto_trade_close_positions_completed` — terminal aggregate event for
  every manual close-positions invocation, with `closed_count`,
  `failed_count`, `skipped_count`, and the reason. Info or error level.
- `multi_tp_duplicate_dispatch_ignored` — `MultiTPEngine.handle_tp_triggered`
  short-circuited because the level was already triggered or already
  dispatched (payload `reason: "already_dispatched" | "already_triggered"`).
  Defence-in-depth against duplicate WS events and concurrent re-entry
  inside the per-position lock window. Warning level.
- `sl_adjustment_skipped_position_already_flat` — pre-flight `get_position`
  reported the exchange position is gone, so no `replace_sl` was enqueued
  (multi-TP engine) or the queue dropped the in-flight task before it
  reached the adapter. Emitted instead of `emergency_market_close` when
  the position has already closed itself. Warning level.
- `sl_adjustment_skipped_would_trigger_immediately_vs_mark` — the requested
  SL trigger sits at or beyond the current mark (and clamping would push
  it below entry). Emitted by the engine pre-flight and by the queue when
  Binance returns `-2021` / `-4131`. Warning level.
- `sl_adjustment_clamped_to_safe_distance` — the requested SL trigger was
  too close to the mark; the engine pushed it 0.1% away from the mark and
  enqueued the replace anyway (clamped target still protects profit vs
  entry). Warning level.
- `emergency_close_skipped_position_flat` — emergency market close was
  skipped because the live exchange position is flat. Prevents the prod
  Binance `-2022` "ReduceOnly Order is rejected" loop. Warning level.
- `replace_sl_coalesced_inflight` — two multi-TP `replace_sl` tasks
  arrived within the quick-fire window (`0.5s`) for the same position;
  the queue coalesced them into the latest-intent task. Info level.
- `cancel_remaining_orders_quiesce_timeout` — `_cancel_remaining_orders`
  waited up to 2 s for an in-flight `replace_sl` to finish before issuing
  on-exchange DELETEs but the task did not complete. Error level —
  operator should inspect whether the position has a duplicate SL.

Manual close-positions flow (`POST /api/v1/live/auto-trade/close-positions`):

- Two-step destructive operation. First request with `confirm: false` (or
  omitting `confirm`) returns **HTTP 412 Precondition Failed** with a
  preview body listing every position that would be closed (symbol, side,
  quantity, entry price, current SL price, count of conditional orders).
  Nothing changes on the exchange or in the database.
- Re-send with `confirm: true` to execute. Per position the flow is:
  1. Cancel every known TP/SL conditional order (best-effort; failures are
     logged but do not block the close).
  2. Verify with `get_position`; if the position is already flat on the
     exchange, mark the DB row CLOSED and skip the market call (idempotent).
  3. Otherwise issue a market reduce-only close for the live size.
  4. Mark DB row CLOSED with `close_reason` (defaults to `manual_close`),
     untrack from the WS manager registry, emit `position_manual_closed`.
- Failures on individual symbols do not abort the batch — the response
  surfaces `closed[]`, `failed[]`, `skipped_already_closed[]` so the
  operator sees the full outcome.
- Independent from `/auto-trade/stop`: this endpoint does **not** flip
  `is_running`. Pending signal-queue rows are also left as-is. To both
  pause new entries and flatten existing positions, call `/stop` and
  `/close-positions` together.
- Optional fields in the body: `account_id` (when the user owns multiple
  configs), `reason` (free-text recorded in the audit event).

Bybit-specific notes:

- All `/v5/position/trading-stop` requests pass `orderLinkId` so subsequent
  WS execution events (which echo it back as `orderLinkId`) can be matched
  back to a specific level even when Bybit assigns a different real
  `orderId`. The WS manager matches against both `order_id` and
  `client_order_id` from the normalized event.
- `cancel_and_replace_sl` uses `tpslMode: "Partial"` and explicit `slSize`
  matching the remaining quantity so the SL targets the correct slice on
  positions opened with multi-TP (which uses Partial mode).
- TP level price-match tolerance is `MULTI_TP_MATCH_TOLERANCE_PCT = 0.5%`
  (was sub-tick precision); the `_match_tp_level` resolver now picks the
  level with the smallest absolute price delta within tolerance, prefers
  `event.trigger_price` over fill price, and emits `tp_fill_unmatched` if
  nothing matches at all.

## W7 — Multi-Strategy Account Partitioning

> Acceptance: ≥3 concurrent strategies on one user **without signal collisions**.

The platform supports multi-strategy trading by binding each strategy to its
own physical exchange sub-account (its own API key). Physical isolation on
the exchange side makes signal collisions impossible by construction:

- `AutoTradeConfig.uq_auto_trade_configs_user_account_id` ⇒ 1 strategy per
  `ExchangeCredential`.
- `ExchangeCredential.uq_exchange_credentials_user_api_key_hash` ⇒ a user
  cannot register the same physical sub-account twice. Duplicate api_key
  uploads return **HTTP 409** with `DuplicateApiKeyError`.
- Each sub-account has its own balance ⇒ that balance **is** the strategy
  budget; the exchange itself enforces it. No separate `strategy_budget_usdt`
  column.

### Setup (operator)

1. **On the exchange**, create N sub-accounts (Binance: master + sub
   accounts; Bybit: UTA sub-accounts; OKX: sub-accounts). Generate one API
   key per sub-account and grant futures-trading permissions.
2. In the platform, register each key via `POST /api/v1/exchange/accounts`
   with a distinct `account_label` (e.g. `BTC-Scalp`, `ETH-Grid`,
   `SOL-Intraday`).
3. Create one `AutoTradeConfig` per credential via
   `PUT /api/v1/live/auto-trade/config` (the form lets you set a free-text
   `strategy_name` shown in the multi-strategy switcher).
4. Use `POST /api/v1/live/auto-trade/play-all` and
   `POST /api/v1/live/auto-trade/stop-all` for bulk lifecycle.

### W7 endpoints

- `GET /api/v1/live/auto-trade/portfolio` — aggregated view: realized +
  unrealized PnL across all strategies, per-strategy entry with live USDT
  balance (parallel fetch, partial failures degrade gracefully into
  `balance_error`).
- `POST /api/v1/live/auto-trade/play-all` — flip `is_running=true` on every
  enabled config. Skips disabled. Returns per-row outcome.
- `POST /api/v1/live/auto-trade/stop-all` — flip `is_running=false` on every
  running config. **Does NOT close positions** — use the existing
  `/auto-trade/close-positions` for that.
- `GET /api/v1/live/auto-trade/balance?account_id=N` — live USDT balance
  snapshot for one sub-account. Powers the per-strategy budget card.

### W7 audit events

- `bulk_play_all_invoked` / `bulk_stop_all_invoked` — bulk lifecycle calls.
- `config_shares_profile_with` — soft warning emitted when two configs
  reference the same `PersonalAnalysisProfile` (legitimate when mirroring a
  signal across two sub-accounts, but worth surfacing).

### Budget semantics

`AutoTradeConfig.position_size_usdt` is the **margin** posted per trade, not
the leveraged notional. The budget card shows `margin_used / sub-account
balance` with a "margin × Nx = notional" tooltip. Effective exposure at
leverage *N* is roughly `position_size_usdt × leverage`.

### What we deliberately did NOT do

- ❌ Multiple `AutoTradeConfig` rows per credential (would require dropping
  the existing UQ and re-introducing the hard collision rule the ТЗ
  initially proposed; rejected as a slow abstraction).
- ❌ Per-strategy Taskiq queues. The existing signal queue already isolates
  per `(history_id, config_id)`, which is sufficient at current load.
- ❌ Account-level capacity cap. Deferred to W8 Risk Engine.

## Asset Universe — supported symbols

> Acceptance (W7 Asset Expansion): execution of trades on **BTC/USDT and
> ETH/USDT** end-to-end.

The platform is symbol-agnostic at every layer (models, backtests, exchange
adapters). The active universe is driven by env-list config rather than
hard-coded constants, so adding a third symbol (SOL, etc.) is an env tweak
plus QA, not a code change.

### Env list (in `core/.env`)

- `ANALYSIS_SCHEDULED_SYMBOLS` — comma-separated list of symbols the
  scheduled-analysis cron iterates each session tick. Default
  `BTCUSDT,ETHUSDT`. Each cron firing creates one `ScheduledAnalysisRun`
  per symbol; failures on one symbol do not block the next (sequential
  `for-of` with try/catch per symbol).
- `AI_FORECAST_SYMBOLS` — symbols included in the weekly AI Forecast
  Catalogue rebuild when no explicit `input.symbol` is supplied. Default
  `BTCUSDT,ETHUSDT`.

### Backend symbol handling

- `PersonalAnalysisProfile.symbol` is `String(24)` — accepts any string
  the universe defines. No DB-level whitelist.
- `to_linear_perp_symbol("ETHUSDT") → "ETH/USDT:USDT"` ([signal.py:7-8](app/services/auto_trade/signal.py))
  via the `_KNOWN_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH", ...)` tuple.
- `_get_stale_threshold` ([ws/manager.py:276-281](app/services/ws/manager.py))
  already has explicit ETH and BTC branches (premium tolerance) so WS
  staleness detection is identical for both.
- `position_size_usdt` is **margin**, not notional. ETH at $3,500 with
  `position_size_usdt=100` requests quantity `0.0286 ETH`. The UI tooltip
  on the budget card surfaces this distinction.

### Frontend (multi-strategy + multi-asset)

- The strategy switcher (auto-trade dashboard) is keyed by `config_id` so
  two strategies sharing one sub-account (e.g. BTC-VWAP + ETH-Grid both on
  Binance-Sub1) remain visually distinct.
- All scoped GETs (`state`, `positions`, `events`, `trades`, `config`,
  `balance`, `ai-overlay/config`) accept both `?account_id=` and
  `?config_id=`. Frontend prefers `config_id` when set.

### Adding a new symbol

1. Add the symbol code (e.g. `SOLUSDT`) to `ANALYSIS_SCHEDULED_SYMBOLS`
   (and to `AI_FORECAST_SYMBOLS` if needed).
2. Restart core workers — next session tick begins generating runs for it.
3. Users create a `PersonalAnalysisProfile` with that `symbol`.
4. Auto-trade picks it up automatically once the user creates a config
   pointing at that profile.

No DB migration, no code change, no rollout coordination beyond the env
list update.

## Documentation notes (Context7 aligned)

- FastAPI dependency injection via `Depends` and `yield`-style DB session providers.
- SQLAlchemy async setup with `create_async_engine` and `async_sessionmaker`.
- uv dependency groups with `[dependency-groups]` and `[tool.uv].default-groups`.
- Taskiq worker startup pattern: `taskiq worker <module>:<broker>`.
- CORS is enabled globally via FastAPI `CORSMiddleware` and configured to allow all origins, methods, and headers by default.

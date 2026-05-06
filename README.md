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

## Documentation notes (Context7 aligned)

- FastAPI dependency injection via `Depends` and `yield`-style DB session providers.
- SQLAlchemy async setup with `create_async_engine` and `async_sessionmaker`.
- uv dependency groups with `[dependency-groups]` and `[tool.uv].default-groups`.
- Taskiq worker startup pattern: `taskiq worker <module>:<broker>`.
- CORS is enabled globally via FastAPI `CORSMiddleware` and configured to allow all origins, methods, and headers by default.

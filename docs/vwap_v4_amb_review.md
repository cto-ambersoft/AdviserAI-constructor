# `vwap_v4_amb.py` Review (1-3002)

## Critical

1. UI and domain logic are tightly coupled in one file.
   - `streamlit` calls are interleaved with indicator calculations, backtests, state storage, and reporting.
   - Impact: impossible to reuse in API/worker flows without running UI code.
2. Stateful behavior relies on file writes with swallowed errors.
   - `saved_strategies.json` and `audit_log.jsonl` are updated with broad `except Exception: pass`.
   - Impact: silent data loss/corruption, no consistency guarantees, race risks.

## High

1. Blocking exchange I/O in request path.
   - `ccxt.binance().fetch_ohlcv(...)` in a sync function.
   - Impact: blocks server workers and reduces throughput under concurrent use.
2. Rendering inconsistencies and duplicated work.
   - Chart is rendered multiple times in one run path.
   - Impact: extra CPU, unstable perceived UI behavior.
3. Incorrect plotting value in ATR OB output path.
   - Exit marker uses entry price in at least one branch.
   - Impact: visual mismatch between trade data and chart.

## Medium

1. Metrics and edge-case handling are inconsistent.
   - Mixed OPEN/closed trade handling can produce confusing aggregates.
2. Business rules are hardcoded in UI-level defaults.
   - Strategy and risk assumptions are spread across Streamlit controls.

## Migration-ready recommendations

1. Extract domain modules first:
   - indicator engine
   - strategy engines (VWAP, ATR OB, Knife, Grid, Momentum)
   - reporting/metrics
2. Add API-first contracts for all backtests:
   - request schemas with deterministic defaults
   - response payload with `summary`, `trades`, `chart_points`, `explanations`
3. Move persistence to Postgres:
   - strategy definitions and snapshots
   - audit events with indexed querying
4. Replace sync `ccxt` with `ccxt.async_support`.
5. Add regression tests using fixture OHLCV datasets to compare old/new outputs.

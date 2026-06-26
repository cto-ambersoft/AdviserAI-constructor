# Backtest Reproducibility (Finding 7)

> Scope: how a published backtest report can be reproduced byte-for-byte from a
> single `experimentId`. Covers the metric formulas + units (7.3), the
> trading-cost model + assumptions (7.4, ties to Finding 6.2), the run manifest
> (7.1/7.2), and the replay procedure (7.5/7.6). The source of truth is the code
> referenced inline; this document explains and cross-checks it.

A backtest is reproducible only if every input that affects the numbers is
pinned: the metric-formula code, the engine logic, the trading-cost parameters,
and the exact OHLCV window. Each backtest response carries a `run_manifest`
([run_manifest.py](app/services/backtesting/run_manifest.py)) that captures all
of these; `core` persists it next to the published report, and a replay
endpoint re-runs from it and flags any drift.

---

## 1. Metric formulas and units

All metrics are computed in [common.py](app/services/backtesting/common.py) from
the per-trade `pnl_usdt` and `r_multiple`. The catalogue of keys, labels and
human-readable intent is in
[metrics_schema.py](app/services/backtesting/metrics_schema.py) and is served
live at `GET /api/v1/backtest/metrics-schema` (see §4).

**R-multiple** ([`compute_trade_r_multiple`](app/services/backtesting/common.py)) —
the building block for most performance metrics. Per closed trade:
`r_real` if the engine recorded it, else `pnl_usdt / risk_usdt`, where
`risk_usdt` is the engine value or derived as `|entry − sl| × quantity`
(or `(|entry − sl| / entry) × allocation_usdt`). Unit: multiples of risk (R).

| Metric | Formula | Unit |
|---|---|---|
| `win_rate` | share of closed trades with a positive result × 100 | percent |
| `profit_factor` | `Σ(R>0) / |Σ(R<0)|`; if no losses → `max(0, Σ wins)` | ratio |
| `sharpe_proxy` | `mean(R) / std(R, ddof=1) × √N`; `0` if `N<2` or `std≤0` | ratio (per-trade R, **not** annualised) |
| `max_drawdown` | `max(running_max(cumsum R) − cumsum R)` | R |
| `max_drawdown_pct` | `max((peak − equity) / peak)` over the equity curve × 100 | percent |
| `total_return_pct` | `(final / initial − 1) × 100` | percent |
| `annualized_return_pct` | `((final / initial) ^ (365.25 / period_days) − 1) × 100` | percent |
| `calmar_ratio` | `annualized_return_pct / max_drawdown_pct` | ratio |
| `walk_forward_stability.stability_score` | `positive_windows / num_windows` (R split into 4 windows) | fraction `0..1` |

Notes:
- **Annualisation uses `CRYPTO_YEAR_DAYS = 365.25`** ([common.py:7](app/services/backtesting/common.py#L7)) — crypto trades 24/7, so there is no trading-day (252) adjustment.
- `win_rate` has two consistent definitions that agree on sign: the R-based
  share in `calculate_performance_metrics`, and the NET-`pnl_usdt`-based share
  in [`refresh_net_pnl_summary`](app/services/backtesting/cost_model.py) used by
  engines that pre-summarise before costs are applied (so a marginal winner that
  fees flip to a loss is counted as a loss).
- All capital metrics derive from `pnl_usdt`. Because the cost model (§2) nets
  costs off `pnl_usdt` before `add_capital_metrics` runs, every downstream
  metric reflects costs automatically.

**Walk-forward scope (Finding 6.3).** `walk_forward_stability`
([common.py:175](app/services/backtesting/common.py#L175)) is a deterministic
protocol applied to the **algorithmic component**: the closed-trade R-multiples
are split into 4 sequential windows and `stability_score = positive_windows /
num_windows`. Walk-forward is meaningful only for the deterministic algo layer.
The **LLM/AI decision component is not subject to deterministic walk-forward** —
the model under the hood is not reproducible bar-by-bar — so it is excluded from
this metric by design. The score reflects only the algorithmic strategy's
out-of-sample stability, not the LLM's.

---

## 2. Trading-cost model and assumptions

Implemented in [cost_model.py](app/services/backtesting/cost_model.py). One
shared model nets costs off each closed trade's `pnl_usdt` uniformly across all
five engines (VWAP, ATR Order-Block, Knife-Catcher, Grid, Intraday).

**Units — percent per side of notional** (matching the pre-existing
`order_fee_pct` / `fee_pct` engine params):

| Param | Meaning | Default |
|---|---|---|
| `fee_pct` | exchange commission per side | `0.06` (≈ `0.12%` round-trip) |
| `slippage_pct` | execution slippage per side | `0.0` (off) |
| `funding_pct_per_bar` | perpetual funding per held bar | `0.0` (off) |

**Per-trade cost** ([`trade_cost_usdt`](app/services/backtesting/cost_model.py)):

```
cost_usdt = (entry_notional + exit_notional) × (fee_pct + slippage_pct) / 100
          +  entry_notional × funding_pct_per_bar / 100 × holding_bars
```

- Fee + slippage are charged on **both** sides (entry and exit).
- Funding is linear in the number of held bars.
- `pnl_pct` is rescaled by `net / gross` so each engine's own `pnl_pct` basis is
  preserved (engines disagree: some use percent of entry, some a fraction of
  total capital); a zero gross is left untouched (cost still lands in `pnl_usdt`).

**Zero cost is a deliberate no-op.** With `fee = slippage = funding = 0` the
trades are returned untouched, so a cost-free run reproduces the pre-Finding-7.4
numbers exactly. This is the regression guarantee in
[tests/test_backtest_cost_model.py](tests/test_backtest_cost_model.py).

---

## 3. The run manifest

[`build_run_manifest`](app/services/backtesting/run_manifest.py) attaches a
`run_manifest` block to every backtest response:

| Field | What it pins | How |
|---|---|---|
| `engine` / `engine_version` | engine identity + logic version | `engine_version` is a manual semver, bumped on breaking engine-logic changes |
| `metric_formula_version` | the metric-formula code | SHA-256 of the `common.py` + `metrics_schema.py` source (the `feature_code_hash` pattern) |
| `cost_model` | the costs actually applied | the resolved `fee_pct` / `slippage_pct` / `funding_pct_per_bar` |
| `candles_hash` | the exact OHLCV window | SHA-256 of the index (int64 ns) + each OHLCV column (contiguous float64 bytes) |
| `data_window` | window bounds + bar count | `start` / `end` / `bars` |
| `seed` / `env` | RNG seed + runtime fingerprint | `python` / `pandas` / `numpy` versions (informational) |

Both hashes are **deterministic and library-version-independent**: identical
metric code + identical candles always yield identical hashes (explicit byte
serialisation, not pandas' internal hashing). `metric_formula_version` is
memoised per process.

`core` persists the manifest on `backtest_experiments` and hoists the key fields
(`metricFormulaVersion`, `engineVersion`, `costModel`, `candlesHash`) onto the
published `ai_forecast_catalogue` entry, so a report exposes its provenance
without loading the experiment
([backtest-experiment.service.ts](../core/src/analysis/backtest-experiment.service.ts)).

---

## 4. Reproducing a report

### Recover the metric definitions for a stored version

```
GET /api/v1/backtest/metrics-schema?version=<metric_formula_version>
```

Returns the current `metric_formula_version`, `engine_version` and the metric
catalogue. When `version` is supplied, `matches_current` flags whether the
stored run was produced by the code running now — a mismatch means the formulas
have drifted and the report cannot be byte-reproduced without checking out the
matching revision.

### Replay an experiment

```
POST /api/v1/backtest-experiments/:experiment_id/replay   (X-API-Key)
```

Re-runs the backtest from the experiment's stored snapshot
(`replayInputs.comparePayload`, which survives TTL-cleanup of the source
personal-analysis jobs — see §5) and reports:

- `reproduced` — `true` only if **all** baseline + AI metrics match within
  `1e-6` **and** `metricFormulaVersion` + `candlesHash` match.
- `flags.metricFormulaVersion` / `flags.candlesHash` — `{stored, replay, matches}`.
- `flags.metricDiffs` — per-metric `{scope, key, stored, replay}`; a metric that
  disappears on replay is flagged with `replay: null`.

### Clean-checkout reproduction (formula/engine drift)

If `matches_current` is `false`, check out the revision whose
`metric_formula_version` matches the stored one (tag engine releases by
`engine_version`), then replay:

```
git checkout <tag-matching-engineVersion>
# redeploy constructor, then:
POST /api/v1/backtest-experiments/:experiment_id/replay
```

The candles are re-fetched by the stored `data_window`; `candles_hash` verifies
they match the original window (a mismatch surfaces as a flag, e.g. if the
exchange revised historical bars).

---

## 5. Retention

- `backtest_experiments` and `ai_forecast_catalogue` have **no TTL** — they are
  the durable record of published reports.
- Raw `exports/` CSVs and source personal-analysis jobs **may** be cleaned; the
  experiment's `replayInputs` snapshot (the full compare payload incl.
  `ai_forecast_rows`) is self-contained, so replay still works after cleanup.
- OHLCV candles are not snapshotted — only their `candles_hash`. They are
  re-fetched by window on replay and verified against the hash.

---

## 6. Scope: what is and isn't modelled (Finding 7.4 / 6.2)

**Modelled:**
- Commission on both sides of every closed trade (`fee_pct`).
- Optional per-side execution slippage (`slippage_pct`).
- Optional linear perpetual funding per held bar (`funding_pct_per_bar`).

**Explicitly out-of-scope** (documented, not silently ignored):
- **Partial fills** — every trade fills fully at a single price. No
  partial-fill modelling. (`apply_cost_model` skips `OPEN` trades and any trade
  with no derivable notional.)
- **Maker/taker distinction, fee tiers, rebates** — a single flat per-side
  `fee_pct` is used; defaulting to `0.06%` (taker).
- **Order-book depth / market impact** — slippage is a flat per-side percentage,
  not a depth- or size-dependent impact model.
- **Funding-rate schedule** — funding is a flat per-bar rate, not anchored to
  real 8-hour funding timestamps or the live funding-rate curve.
- **Borrow / margin interest** for spot-margin positions.

These simplifications keep the cost model deterministic and engine-agnostic.
Each is a candidate for a future finding if backtest realism needs to increase;
none affect the reproducibility guarantees in §3–§4.

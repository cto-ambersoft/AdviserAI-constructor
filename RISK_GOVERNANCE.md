# Risk Governance, Strategy Health & Data Freshness (Milestone 4 · Week 8)

Reference for the three subsystems delivered in W8. All three live in the
`constructor` (FastAPI + Taskiq) service. Every limit/feature is **opt-in and
fail-safe**: absent config = prior behaviour, so legacy strategies are
unaffected.

---

## 1. Pre-Trade Risk Engine

A deterministic validator run **before any order is placed**, in
`_process_without_open_position` (after the AI-overlay entry block, before
sizing). A violation records a `risk_blocked` `AutoTradeEvent` and opens
nothing.

- **Config:** `auto_trade_risk_configs` (1:1 with `auto_trade_configs`,
  `config_id` PK=FK, all limits nullable = "off"). Surfaced on the config API
  as a nested `risk` object; bad values rejected with 422 at the edge and a DB
  CHECK backstop. Migration `20260526_0018`.
- **Engine:** `app/services/auto_trade/risk/engine.py` —
  `check_pre_trade(...) -> RiskDecision`, pure, first-violation-wins. A
  `risk_cfg` that is `None` or `enabled=False` ⇒ allow.

| Rule | Block code | Scope | Notes |
|---|---|---|---|
| Leverage ceiling | `leverage` | config | inclusive (`>` ceiling blocks) |
| Max open positions | `max_open` | **user (portfolio-wide)** | per-config is degenerate (1-open-per-account index) |
| Max open per symbol | `max_open_per_symbol` | user + symbol | anti-duplicate across strategies |
| Exposure cap | `exposure` | user | Σ `position_size_usdt`(open)+new `>` cap |
| Daily loss (usdt) | `daily_loss` | strategy (config), UTC day | absolute; always enforced |
| Daily loss (pct) | `daily_loss_pct` | strategy (config), UTC day | % of the strategy's sub-account balance |
| Conflicting signal | `conflicting_signal` | user + symbol | `block_opposite`; `net`/`replace` logged + skipped (W10) |

**Daily-loss fail-open (SPEC §6.3):** the gate computes the strategy's today
realized PnL as a **single SQL aggregate over stored entry/close prices (no
exchange call)**, scoped to the config so it matches the per-account balance
the `_pct` rule divides by. The sub-account balance is fetched **only when
there is a realized loss today** (the `_pct` rule can't fire otherwise). If
that fetch fails, the `_pct` rule is **skipped with a `risk_check_degraded`
warning — never blocks**; the absolute `_usdt` limit still fires. Inputs are
computed lazily (only when daily-loss is configured).

> **No auto-close / auto-pause in W8.** The engine only *blocks new entries* and
> *records* events. Kill-switch and KPI-Guard auto-pause are W9.

---

## 2. Strategy Health Score

On-read composite (no table) over a strategy's closed positions in a rolling
window, reusing `backtesting/common.py` so live and backtest metrics agree.

- **Service:** `app/services/auto_trade/health.py` —
  `compute_strategy_health(...) -> StrategyHealth`.
- **Endpoint:** `GET /api/v1/live/auto-trade/strategies/{config_id}/health?window_days=30`
  (ownership-checked, 404 on unknown).
- **Metrics:** win rate, max drawdown %, total PnL, Sharpe-proxy, walk-forward
  stability → composite `health_score ∈ [0,100]` + `health_class`
  (`healthy`/`warning`/`critical`). Weights + normalization refs are **named
  constants, calibrated in W9**.
- **`< HEALTH_MIN_TRADES` (10) closed trades ⇒ `insufficient_data`** (never a
  false `critical`); empty ⇒ safe zeros.

---

## 3. Data Freshness (4h sweep)

- **Helper:** `app/core/freshness.py` — `normalize_to_utc` / `age_minutes` /
  `is_fresh` (one source of truth, shared with the ai_overlay resolver).
- **Sweep:** `app/services/personal_analysis/freshness.py` —
  `sweep_agent_freshness(session)` checks each active profile's latest
  `PersonalAnalysisHistory`, upserts an `AgentFreshnessStatus` row per
  `(profile_id, agent_key)` (`__profile__` aggregate + one per enabled agent),
  and emits one `data_stale` event per stale profile. Migration `20260527_0019`.
- **Cron:** Taskiq `agent_freshness_every_4h` @ `0 */4 * * *` (UTC) in
  `app/worker/tasks.py`; threshold `settings.agent_freshness_threshold_minutes`
  (default 240, env-configurable).
- **Endpoint:** `GET /api/v1/health/agents?is_fresh=true|false`
  (auth'd, scoped to the caller's profiles).

> Per-agent freshness is currently the profile's data recency (constructor-
> side). True per-agent timestamps come from core's `AiDecisionEvent.perAgent`
> (deferred past W8).

---

## 4. Definition of Done — verification

| Gate | Result |
|---|---|
| Full test suite | **688 passed, 1 skipped** |
| W8 tests added | **~50** (engine, health, freshness, gate integration, endpoints) |
| `mypy app` | 52 errors — all pre-existing; **none in W8 code** (refactor dropped one) |
| `ruff` / `ruff format` | **11 W8 new files 100% clean**; edited files carry only pre-existing debt |
| Migrations | `0018` + `0019` up/down verified on SQLite (CHECK/UNIQUE/index enforced); single linear head |
| Cron | `agent_freshness_every_4h` @ `0 */4 * * *` registered |

### Acceptance criteria (SPEC §1.3)

| # | Criterion | Status |
|---|---|---|
| AC-W8-1 | Pre-Trade Risk Engine blocks ≥4 rules with `risk_blocked` + payload | ✅ (5 rules / 7 block codes) |
| AC-W8-2 | `risk_cfg` None / all-None ⇒ unchanged (no regression) | ✅ (regression tests + suite green) |
| AC-W8-3 | Health endpoint returns all fields; values reconcile with `common.py` | ✅ (golden-set test) |
| AC-W8-4 | `< min_trades` ⇒ `insufficient_data`, never false `critical` | ✅ |
| AC-W8-5 | `sweep_agent_data_freshness` @ `0 */4 * * *`; writes rows; `data_stale` on stale | ✅ |
| AC-W8-6 | ruff + ruff format + mypy clean; migrations up/down | ✅ |

---

# Risk Enforcement (Milestone 4 · Week 9 — AC#4)

W9 builds the **in-trade and post-trade** enforcement layer on top of the W8
pre-trade foundation: the things W8 §6.3 deliberately forbade (auto-close,
auto-pause). All of it is **opt-in and OFF by default** — every threshold ships
`None`/disabled and must be calibrated with traders before it is enabled in
production (the W8 §6.2 "ask first" rule still governs). The cardinal W9 rule:
**a strategy is paused or a position is closed only on a *confirmed breach
computed from data we actually have*** — missing data, an unreachable balance, a
compute error, or `insufficient_data` never triggers an autonomous action.

## 5. KPI-Guard auto-pause

Pauses a *running* strategy when its live KPIs breach a configured guard. The
single pause mechanism (`AutoTradeService._auto_pause_strategy`) is **idempotent**
(row-locked; an already-stopped strategy is a clean no-op) and emits a
caller-named trigger event **plus** the generic `strategy_auto_paused` — distinct
from the user-facing `auto_trade_stop` so a system halt is auditable. Re-enable
is a normal user `set_running(True)`.

- **Config:** extra columns on `auto_trade_risk_configs` (migration `20260605_0025`),
  all nullable / off: `kpi_guard_enabled`, `kpi_guard_max_dd_pct`,
  `kpi_guard_max_daily_loss_usdt`, `kpi_guard_max_daily_loss_pct`,
  `kpi_guard_min_win_rate_pct`, `kpi_guard_min_trades`. **Distinct from** the
  pre-trade `daily_loss_limit_*`: those *block the next entry*; these *pause the
  whole strategy* and are meant to be set more conservatively (flagged for trader
  sign-off).
- **Evaluator:** `app/services/auto_trade/risk/kpi_guard.py` —
  `evaluate_kpi_guard(...) -> GuardDecision`, pure. Two rule families:
  - **daily-loss** (`daily_loss` / `daily_loss_pct`) — a *hard same-day
    realized-loss* aggregate, **NOT gated by sample size** (a runaway loss must
    halt a fresh strategy too). The pct variant **fails open**: an
    unavailable/invalid balance skips the check with a `risk_check_degraded`
    warning, never a pause.
  - **statistical** (`max_dd` / `min_win_rate`) — only fire on a *reliable* sample
    (`insufficient_data` or `< kpi_guard_min_trades` ⇒ no statistical breach).
- **Drivers:** the cron `evaluate_kpi_guards` (`kpi_guard_every_5m` @ `*/5 * * * *`,
  UTC) sweeps running configs (in-process health, no exchange — review C1) and the
  on-close fast path `_maybe_auto_pause_after_close` (wired into the autonomous
  opposite-trend close and the manual flatten) pauses **within the same
  transaction** as a losing close. The two are mutually idempotent.
- **History:** `strategy_health_snapshots` (migration `20260605_0024`, append-only,
  no unique key) records one snapshot per evaluated config per tick — the data the
  guard reads and the AC#7 dashboard renders.
- **Events:** `kpi_guard_triggered`, `strategy_auto_paused`, `risk_check_degraded`.

## 6. Volatility Kill-Switch

In-trade hard auto-close on a confirmed volatility spike, followed by a risk-off
latch (the strategy is paused so it stops opening new entries until a human
re-enables it).

- **Config:** extra columns on `auto_trade_risk_configs` (migration `20260605_0026`),
  off by default: `kill_switch_enabled`, `kill_switch_atr_spike_mult`,
  `kill_switch_atr_period`, `kill_switch_price_move_pct`,
  `kill_switch_cooldown_seconds`.
- **Detector:** `app/services/sl_tp/kill_switch.py` —
  `detect_volatility_spike(...) -> KillSwitchSignal`, pure. Two first-trigger-wins
  branches: ATR spike (`current_atr >= mult * baseline`, guarded on a strictly
  positive baseline) and last-bar price move (`|move| >= pct`). Any missing input
  / non-positive baseline / unset threshold ⇒ no close.
- **Detection hook:** `RealtimeSLAdjuster.on_tick` runs the detector **before** the
  SL pipeline (per-position kill-switch cooldown), via an injected
  `kill_switch_handler` callback — `None` (the default) makes it a pure no-op, so
  the realtime SL hot path is byte-for-byte unchanged when the kill-switch is off.
- **Close + latch:** `AutoTradeService.kill_switch_close_position` reuses the
  existing `_flatten_single_position` (DB `state`→CLOSED, exchange reduce-only,
  ledger), stamps `close_reason="volatility_kill_switch"`, emits
  `kill_switch_triggered`, then latches risk-off via `_auto_pause_strategy`
  (`risk_off_entered` + `strategy_auto_paused`) — **even if the close itself
  failed** (a spike must stop new entries regardless). Idempotent; never retried
  into a loop (the realtime cooldown guards re-entry). `EMERGENCY_CLOSE` is only a
  valid FSM transition from `ERROR_RECOVERY`, and the close path sets `state`
  directly, so no FSM transition is driven (consistent with every other close).
- **⏳ Not yet wired in production (T2.3b):** the `ws/manager` injection of
  `kill_switch_handler` → a session-backed `kill_switch_close_position`, and the
  `PositionContext` builder populating the kill-switch config from the risk row.
  Both halves above are fully tested and off-by-default; the kill-switch cannot
  fire live until this plumbing lands.

## 7. KPI transparency (AC#7 backend)

- `roi_pct` added to the Strategy Health Score; the normalization base is a single
  named helper (`_normalization_base_usdt`, the per-trade `position_size_usdt`
  proxy — real-balance swap deferred to calibration, no exchange on the read path).
- `GET /api/v1/live/auto-trade/positions/{id}/trace` — Post-Trade execution trace:
  the signal→close timeline (position metadata + the `decision_event_id` pointer
  into core's `ai_decision_events`, surfaced not dereferenced + the chronological
  `AutoTradeEvent` list). Ownership-checked, read-only.
- `PortfolioSummaryResponse` carries per-strategy `win_rate_pct` / `max_dd_pct` /
  `sharpe_proxy` / `roi_pct` (from the latest snapshot) and a portfolio
  `portfolio_max_dd_pct` (worst per-strategy DD; a true merged-equity portfolio DD
  is deferred to the Portfolio Supervisor, W11). The live-KPI **dashboard UI** is W12.

> ⚠️ **ROI/DD base caveat (review I3).** `roi_pct` and `max_dd_pct` are normalized
> by the **per-trade notional** (`position_size_usdt`), **not account equity** — a
> deliberate W9 proxy (the read path makes no exchange call). They can read **high**
> (e.g. 12 winning trades on a 100-USDT base ⇒ `roi_pct=120%`, meaning 120% of one
> position's notional, not the account). The W12 dashboard **must label the
> denominator**, and calibration **must** decide whether to swap the base to the real
> sub-account balance before these numbers are trusted for decisions. The OpenAPI
> field descriptions on `StrategyHealthRead` / `StrategyPortfolioEntryRead` carry the
> same warning for the frontend.

> **KPI-Guard sample floors (review I2).** The statistical rules (`max_dd`,
> `min_win_rate`) require **≥ `HEALTH_MIN_TRADES` (10)** closed trades — below that,
> `compute_strategy_health` returns `insufficient_data` with zeroed metrics, so a
> `kpi_guard_min_trades` set *below* 10 has no effect (it only adds a *higher* floor).
> The daily-loss rules are intentionally **ungated** by sample size (a runaway loss
> halts even a fresh strategy).

## 8. Definition of Done — verification (W9)

| Gate | Result |
|---|---|
| Full test suite | **793 passed, 1 skipped** |
| W9 tests added | **~45** (KPI-Guard, kill-switch, snapshots, on-close, trace, portfolio KPIs, migrations) |
| `mypy app` | 51 errors — all pre-existing; **none in W9 code** |
| `ruff` / `ruff format` | all W9 new files 100% clean; edited files carry only pre-existing debt |
| Migrations | `0024`→`0025`→`0026` up/down verified on SQLite (op-proxy); single linear head `0026` |
| Cron | `kpi_guard_every_5m` @ `*/5 * * * *` registered |

### Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| AC#4 | Auto-pause on **Max DD** | ✅ engine (`kpi_guard_max_dd_pct` → pause) |
| AC#4 | Auto-pause on **Loss per day** | ✅ engine (`kpi_guard_max_daily_loss_usdt/_pct` → pause) |
| AC#4 | **KPI Guard** (min win-rate) | ✅ engine |
| AC#4 | **Volatility Kill-Switch** | ✅ engine; **prod wiring T2.3b deferred** |
| AC#7 | Live KPI per strategy + portfolio (Sharpe-proxy / Win-Rate / ROI / DD) | ✅ backend; **UI W12** |

**Carried forward:** kill-switch production wiring (**T2.3b**); Portfolio Supervisor
v2 DD-watcher / auto-pause-all (**T3.3 → W11**); KPI dashboard UI + SSE (**W12**);
`net`/`replace` conflicting-signal policies (**W10**); cross-service per-agent
freshness from core (post-M4). All W9 thresholds remain **OFF pending trader
calibration** (SPEC §6.2).

---

# 7. Calibration & Safe Enablement (M4 remediation · T18)

> **Why this section exists.** Every in-trade / portfolio / promotion governance
> control ships **OFF by default** (NULL thresholds or an `*_enabled=False` flag).
> This is deliberate — they act on **real money** (mainnet sub-account), so a wrong
> threshold is worse than no threshold. Nothing protects the account until an
> operator calibrates and enables each control. **Do not set values "by eye".**

## 7.1 The off-by-default control surface

| Control | Flag / thresholds | Default | Effect when off |
|---|---|---|---|
| Pre-Trade limits | `enabled`=True, but every limit NULL | no rule until a limit is set | entries unbounded |
| Conflicting-signal | `conflicting_signal_policy` | `off` | opposite signals allowed |
| KPI-Guard auto-pause | `kpi_guard_enabled` + `kpi_guard_*` | `False` | no auto-pause (AC#4 dormant) |
| Volatility Kill-Switch | `kill_switch_enabled` + `kill_switch_*` | `False` | no auto-close on spike |
| Portfolio-DD halt (merged-equity, T12) | `portfolio_dd_halt_enabled` + `_threshold_pct` | `False` / 20% | no halt-all |
| Strategy anomaly detection | `anomaly_detection_enabled` + `anomaly_*` | `False` | no anomaly alerts |
| AI overlay (entry-lock / ATR / RSI) | per-flag | all `False` | `ai_trend` does not affect trading |
| Data-freshness gate (T14) | `agent_freshness_block_enabled` | `False` | stale data alerts only, never blocks |
| Promotion KPI-Gate | `promote_*` thresholds | built-in conservative fallbacks | sandbox→live uses fallback gate |

## 7.2 Calibration procedure (with traders)

1. Pull the real per-strategy series (W9 ledger / `compute_strategy_health`) for the
   account and review historical Max-DD, daily-loss, win-rate, ATR-spike and
   trade-frequency distributions **with the trading desk**.
2. Pick thresholds from those distributions (e.g. KPI-Guard Max-DD a little beyond
   the worst *expected* drawdown), not round numbers.
3. For the anomaly z-threshold, back-test the z-score/EWM detector on the real
   series so it fires on genuine outliers, not normal variance.
4. Record the agreed values + rationale (date, who signed off) before enabling.

## 7.3 Rollout order (per control)

`demo/testnet → live with minimal position_size_usdt → full size.` Enable **one**
control at a time; watch its events (SSE + Telegram) for a full trading cycle before
the next. Portfolio-DD halt and the freshness **block** are account-wide — enable
those last and watch closely.

## 7.4 Acceptance configuration (what "enabled for sign-off" means)

For M4 acceptance the controls are **present, tested, and OFF by default**; the
client decides which to enable post-calibration. The minimum recommended live set
once calibrated: KPI-Guard auto-pause (AC#4), Volatility Kill-Switch, and the
Portfolio-DD halt. Leave anomaly-detection and the freshness block in alert-only
until their thresholds are validated on real series.

> Operational note: enabling is a **per-config** (or env, for portfolio/account-wide
> watchers) change — no deploy required. Disabling is the rollback.

---

# 8. Account Security — Two-Factor Authentication

Two co-equal, **per-user opt-in** second factors guard sign-in and the step-up
re-auth on critical actions (start auto-trade, save config, add/change an exchange
key, promote/demote a strategy, apply an agent-weight suggestion, disable a factor):

- **TOTP** (authenticator app) — RFC 6238, recovery codes, per-enrollment lockout.
- **Email-2FA** — a one-time code emailed via **Resend**. Enrollment is
  *verify-on-enroll* (the account email is not implicitly trusted); the factor then
  works at login and step-up. Per-factor lockout mirrors TOTP. Codes are stored
  hashed, single-use, TTL-bounded, with distinct purposes
  (`email_2fa_enroll` / `email_2fa_login` / `email_2fa_step_up`) so a code minted for
  one purpose can't be replayed for another.

A single helper, `app.services.two_factor.has_second_factor` /
`available_factors`, is the source of truth every gate consults — login (`/signin`
advertises `factors`), step-up (`require_step_up`), and the UI factor picker. When
both factors are enrolled the user picks; when one is enrolled it auto-selects.
Disabling the **last** factor is allowed and returns the account to "no 2FA".

**Resend prerequisite (off-by-default).** Email-2FA is gated on `RESEND_API_KEY` +
`EMAIL_FROM`. With Resend unconfigured, `available_factors` never includes `email`,
enrollment returns `503`, and TOTP-only / no-2FA users are completely unaffected.
The `step_up_require_2fa` hard-block (review I8), when on, counts **either** factor.

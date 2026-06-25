# TODO — AC#4 In-Trade Governance

Из [ac4-plan.md](ac4-plan.md). Вертикальные срезы: каждая задача = один полный путь. Чекбокс — только когда
**acceptance + verification** оба зелёные. Checkpoints — жёсткие гейты.

Легенда: `[ ]` todo · `[~]` in progress · `[x]` done · 🔴 AC-critical · ⛳ checkpoint
Голова миграций: `20260604_0023` → следующая `20260604_0024`.

---

## Phase 0 — Фундамент
- [ ] **T0.1** Live-KPI / running-equity сервис (+ фикс review-I6)
  - [ ] `app/services/auto_trade/live_kpi.py` `compute_live_kpi(...) -> LiveKpi`
  - [ ] running_pnl = realized_net(window, ledger) + open unrealized; running-DD по equity incl open MtM
  - [ ] **I6:** running_dd_pct нормирован на реальную базу (баланс суб-аккаунта / `capital_base_usdt`), НЕ per-trade
  - [ ] `sample_size < min_trades` ⇒ `insufficient_data`; `unrealized=None` ⇒ best-effort mark / degraded-флаг
  - [ ] V: `tests/test_live_kpi.py` (golden vs common.py; running-DD с убыточной открытой; insufficient; empty)

## Phase 1 — KPI-Guard auto-pause 🔴
- [ ] **T1.1** Risk-config + миграция `0024` + API
  - [ ] поля: `auto_pause_enabled`, `kpi_guard_max_dd_pct`, `kpi_guard_min_win_rate_pct`,
        `kpi_guard_min_trades`(10), `kpi_guard_window_hours`(24) — все nullable=off
  - [ ] CheckConstraints (схема ↔ DB верхние границы, урок I4); миграция `down_revision="20260604_0023"`
  - [ ] AC: upsert→read round-trip; daily-loss переиспользуется (breach при auto_pause ⇒ пауза, не только block)
  - [ ] V: `alembic up/down` (sqlite op-proxy); round-trip; mypy
- [ ] **T1.2** `evaluate_kpi_guard(kpi, risk_cfg) -> GuardDecision` (чистая логика)
  - [ ] `app/services/auto_trade/risk/kpi_guard.py`; first-breach-wins
  - [ ] правила: running_dd≥max_dd · today_net_loss≥daily_limit · win_rate<min_wr (только при ≥min_trades)
  - [ ] AC: off/insufficient_data ⇒ no-pause; reason+payload (actual vs threshold)
  - [ ] V: unit на каждое правило (block+pass), границы, insufficient_data
- [ ] **T1.3** Cron + on-close hook → пауза
  - [ ] `check_auto_trade_kpi_guards()` cron `*/5 * * * *` (`kpi_guard_every_5m`) над running+auto_pause конфигами
  - [ ] breach ⇒ `set_running(False)` + событие `kpi_guard_paused`; on-close hook после `_mark_position_closed`
  - [ ] AC: paused ⇒ сигналы скипаются; идемпотентно (повтор — no-op); off ⇒ ничего
  - [ ] V: integration (max_dd по открытой просадке → cron paused; убыточный close за дневной лимит → paused);
        idempotent; cron в `task.labels`
- [ ] ⛳ **Checkpoint A** — `pytest -k "kpi or guard or live_kpi"` · full suite без регрессий · ruff+mypy ·
      `0024` up/down · **пороги off; калибровка зафиксирована**. **AC#4 Loss/day + Max DD auto-pause закрыт.**

## Phase 2 — Volatility Kill-Switch 🔴 (параллельно)
- [ ] **T2.1** Schema + миграция `0025`
  - [ ] `volatility_kill_enabled`, `volatility_kill_atr_mult`, `volatility_kill_lookback` (nullable=off; `atr_mult>1`, `lookback>=2`)
  - [ ] V: `0025` up/down; round-trip
- [ ] **T2.2** Spike-детектор в watcher-тике
  - [ ] hook в `run_position_watcher_tick` (`watchers/service.py:301`); spike = ATR_short/ATR_baseline ≥ mult
        (reuse `compute_atr` `live_tracker.py:115`); pure, без exchange
  - [ ] AC: spike⇒trigger; below⇒no-op; мало баров⇒no-op
  - [ ] V: unit на синтетическом kline-буфере (spike/no-spike/insufficient)
- [ ] **T2.3** Kill = close reduce-only + pause
  - [ ] `trigger_volatility_kill(...)`: `_flatten_single_position`/`place_futures_market_order(reduce_only=True)`
        + `set_running(False)` + событие `volatility_kill_switch`; идемпотентно
  - [ ] AC: позиция закрыта reduce-only, paused, событие; повтор — no-op; off⇒ничего
  - [ ] V: integration с fake-adapter (spike→close+paused+event); idempotent
- [ ] ⛳ **Checkpoint B** — `pytest -k volatility` · full suite · ruff+mypy · `0025` up/down · **только reduce-only/pause, никогда не открывает**

## Phase 3 — Portfolio DD watcher 🟠
- [ ] **T3.1** Portfolio-risk + расчёт DD
  - [ ] `auto_trade_portfolio_risk` (1/user) + миграция `0026`; `portfolio_max_dd_pct` (nullable=off)
  - [ ] портфельный running-DD = по агрегированной equity (Σ T0.1 по конфигам поверх `compute_portfolio`)
  - [ ] V: `0026` up/down; тест агрегации DD по 2+ конфигам
- [ ] **T3.2** Cron → pause-all
  - [ ] `check_portfolio_drawdown()` cron `*/5 * * * *` (`portfolio_dd_every_5m`) → `set_running_bulk(False)` + `portfolio_dd_paused_all`
  - [ ] AC: breach ⇒ все стратегии paused; идемпотентно; off⇒ничего
  - [ ] V: integration (портфельный DD за порог → bulk-pause); idempotent; cron зарегистрирован
- [ ] ⛳ **Checkpoint C** — портфельная пауза · идемпотентно · ruff+mypy · `0026` up/down

## Phase 4 — Integration & DoD
- [ ] **T4.1** Финальный гейт + AC#4-маппинг
  - [ ] full `pytest` (incl. 747) · `ruff format --check`·`ruff check`·`mypy app`
  - [ ] `alembic up→down` для 0024/0025/0026; кроны `kpi_guard_every_5m`/`portfolio_dd_every_5m` зарегистрированы
  - [ ] AC#4: Max DD ✅ · Loss/day auto-pause ✅ · volatility kill ✅ · portfolio pause ✅

---

### Guardrails (стоячие, не чекать)
- ❗ Авто-действие только при флаге + пороге; NULL/off = поведение без изменений (no-regression тест).
- ❗ Никогда не открывать в kill-switch — только reduce-only/pause.
- ❗ Не паузить на шуме: `min_trades` + `insufficient_data` (фикс I6).
- ❗ Кроны лёгкие + идемпотентные; один scheduler; миграции hand-written, chained, rollback-verified.
- ❗ Калибровка порогов (реальные деньги) — с трейдерами перед включением.

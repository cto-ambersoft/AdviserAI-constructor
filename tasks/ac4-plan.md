# PLAN — AC#4 In-Trade Governance (auto-pause · KPI-Guard · Volatility Kill-Switch · Portfolio DD)

> Детальный пошаговый план под AC#4 «Risk Enforcement: автоматическая приостановка стратегии при
> нарушении KPI Guard (Max DD, Loss/day)». Сервис `constructor` (FastAPI + Taskiq + SQLAlchemy).
> Статус: **план — read-only, ждёт human-review перед кодом.**
>
> ⚠️ Это отдельный файл, чтобы **не затирать активные W9-доки** ([plan.md](plan.md)/[todo.md](todo.md)
> с открытыми `⚑ Ревью с человеком`). Связано: [m4-remaining.md](m4-remaining.md), [../RISK_GOVERNANCE.md](../RISK_GOVERNANCE.md).

---

## 0. Контекст, границы, инварианты

**Что уже есть (фундамент):**
- W8 Pre-Trade Risk Engine **блокирует входы** (`risk_blocked`) — но НЕ паузит running-стратегию.
  `engine.check_pre_trade` (`app/services/auto_trade/risk/engine.py`).
- W9 точный **net realized PnL** из ledger без exchange-вызовов: `compute_realized_breakdown`
  (`app/services/execution/futures_pnl.py:181`), `sum_funding` (`app/services/auto_trade/income_sync.py:46`),
  `_position_ledger_breakdown` (`service.py:4471`), `_today_realized_pnl_usdt` (net, pure-DB, T14).
- W8 Strategy Health (`app/services/auto_trade/health.py:145`) — метрики, **но только по закрытым** позициям
  (нет running-DD с открытой позицией).
- Пауза: `set_running` (`service.py:1430`) / `set_running_bulk` (`service.py:1473`); гейт `is_running`
  в `_process_queue_item` (`service.py:2733`) → `signal_skipped_config_inactive`.

**Инварианты (реальные деньги):**
- **Авто-действие только при включённом флаге и заданном пороге.** `NULL`/выключено = поведение без изменений
  (fail-safe), как в W8.
- **Не паузить на шуме:** оценивать DD/WR только при `>= kpi_guard_min_trades` закрытых сделок (учёт review-**I6**).
- **Кроны: быстрые + идемпотентные.** Taskiq шлёт задачу на каждый тик независимо от завершения прошлой
  (context7) → `set_running(False)` идемпотентен, sweep лёгкий; **один инстанс scheduler**.
- **Running-DD считать от реальной базы капитала** (баланс суб-аккаунта / `capital_base_usdt`), а НЕ от
  per-trade базы — это и есть фикс review-**I6**, складываем его в T0.1.
- **Не трогаем** open/close/execution-логику; добавляем только наблюдатели + действие pause/close-reduce-only.

---

## 1. Граф зависимостей

```
                  ┌─────────────────────────────────────────────┐
                  │ T0.1  Live-KPI / running-equity service      │
                  │  (net realized из ledger + open unrealized + │
                  │   running-DD от реальной базы; фикс I6)       │
                  └───────────────┬──────────────┬───────────────┘
                                  │              │
                ┌─────────────────┘              └────────────────┐
                ▼                                                 ▼
   ┌──────────────────────────────┐                ┌──────────────────────────────┐
   │ PHASE 1 — KPI-Guard auto-pause│                │ PHASE 3 — Portfolio DD watcher│
   │ T1.1 risk schema + migr 0024  │                │ T3.1 portfolio-risk + DD calc │
   │ T1.2 evaluate_kpi_guard (pure)│                │ T3.2 cron → set_running_bulk  │
   │ T1.3 cron + on-close + pause  │                └──────────────────────────────┘
   └──────────────────────────────┘
   ┌──────────────────────────────┐   (parallel — не зависит от T0.1; делит close-примитивы)
   │ PHASE 2 — Volatility Kill-Switch
   │ T2.1 schema + migr 0025
   │ T2.2 spike detect (watcher tick, compute_atr)
   │ T2.3 kill = close reduce-only + pause
   └──────────────────────────────┘

   ВСЁ ──► PHASE 4 — Integration & DoD (full pytest, ruff/mypy, миграции up/down, AC-маппинг)
```

**Критический путь (AC#4 ядро):** `T0.1 → T1.1 → T1.2 → T1.3`. T2 — параллельно (быстрая реакция на spike),
T3 — после T0.1 (переиспользует расчёт). Фикс **I6** входит в T0.1.

---

## 2. Фазы и задачи

Формат: **Цель · Файлы (file:line) · Acceptance · Verification · Depends**.

### Phase 0 — Фундамент

#### T0.1 — Live-KPI / running-equity сервис (+ фикс review-I6)
- **Цель:** `compute_live_kpi(*, session, config_id, window_hours=24) -> LiveKpi` — единый источник live-метрик
  для KPI-Guard (T1) и портфеля (T3), а позже для AC#7-дашборда. Поля: `realized_net_today_usdt`,
  `unrealized_usdt`, `running_pnl_usdt`, `running_dd_pct`, `win_rate_pct`, `sharpe_proxy`, `roi_pct`,
  `sample_size`, `capital_base_usdt`, `computed_at`.
- **Файлы:** `app/services/auto_trade/live_kpi.py` (NEW); переиспользовать `summarize_positions_pnl`
  (`service.py:2315`, окно `closed_after/before`), `_position_ledger_breakdown` (`service.py:4471`),
  `build_position_pnl_snapshot` (`service.py:2151`, открытая позиция → `ledger_breakdown.unrealized`),
  `backtesting/common.py` (`calculate_sharpe_proxy`/`calculate_equity_max_drawdown_pct`/win-rate),
  `_today_realized_pnl_usdt` (net, T14).
- **Acceptance:**
  - `running_pnl = realized_net(window) + unrealized(open)`; running-DD считается по equity-кривой,
    включающей **open mark-to-market**.
  - **I6:** `running_dd_pct` нормирован на реальную базу — `capital_base_usdt` = баланс суб-аккаунта
    (fetch один раз за вызов; cron-каденс, не hot-path) с fallback на `Σ deployed margin + realized`.
    НЕ per-trade база.
  - `sample_size < kpi_guard_min_trades` ⇒ метрики помечены `insufficient_data` (DD/WR не считаются «плохими»).
  - Открытая `unrealized = None` (не синхронизировано) ⇒ best-effort `fetch_mark_prices` или `0` с флагом
    `degraded`; без падения.
  - **Без exchange-вызовов в hot-path** (баланс/mark — только тут, на cron-каденсе).
- **Verification:** `tests/test_live_kpi.py` — golden-set (метрики совпадают с прямыми вызовами common.py),
  running-DD с открытой позицией в убытке, insufficient_data, empty; baseline-нормировка не зависит от числа сделок.
- **Depends:** — (фундамент; складывает фикс I6)

### Phase 1 — KPI-Guard auto-pause (per-strategy) 🔴

#### T1.1 — Расширение risk-config + миграция 0024 + API
- **Цель:** поля governance на `auto_trade_risk_configs`. **daily-loss переиспользуем** (W8
  `daily_loss_limit_usdt/_pct`): при `auto_pause_enabled` нарушение лимита теперь не только блокирует вход,
  но и **паузит** стратегию.
- **Файлы:** `app/models/auto_trade_risk_config.py` (EDIT); миграция
  `migrations/versions/20260604_0024_add_kpi_guard.py` (NEW, `down_revision="20260604_0023"`);
  `app/schemas/auto_trade.py` (`AutoTradeRiskConfig` nested + поля); upsert (`service.py` `_apply_risk_config`).
- **Поля (все nullable = off):** `auto_pause_enabled` bool default false · `kpi_guard_max_dd_pct` float ·
  `kpi_guard_min_win_rate_pct` float · `kpi_guard_min_trades` int (default 10) · `kpi_guard_window_hours` int (default 24).
- **Acceptance:** CheckConstraints (`max_dd_pct ∈ (0,100]`, `min_win_rate_pct ∈ (0,100]`, `min_trades >= 1`,
  `window_hours >= 1`); схема ↔ DB (верхние границы, урок review-**I4**); upsert→read round-trip.
- **Verification:** `alembic upgrade head` + `downgrade -1` (sqlite op-proxy); round-trip тест; `mypy`.
- **Depends:** —

#### T1.2 — `evaluate_kpi_guard(kpi, risk_cfg) -> GuardDecision` (чистая логика)
- **Цель:** детерминированно решить, паузить ли. First-breach-wins. Правила: `running_dd_pct >= max_dd_pct` ·
  `today_net_loss >= daily_loss_limit` (reuse) · `win_rate_pct < min_win_rate_pct` (только при
  `sample_size >= min_trades`). `auto_pause_enabled=False` или `insufficient_data` ⇒ no-pause.
- **Файлы:** `app/services/auto_trade/risk/kpi_guard.py` (NEW).
- **Acceptance:** каждое правило паузит/пропускает; min_trades-гард не паузит свежую стратегию; reason+payload
  (actual vs threshold) для события.
- **Verification:** unit-тесты на каждое правило (block+pass), границы, insufficient_data.
- **Depends:** T0.1, T1.1

#### T1.3 — Cron + on-close hook → пауза
- **Цель:** `check_auto_trade_kpi_guards()` — Taskiq cron (`*/5 * * * *`, `kpi_guard_every_5m`) итерирует
  running-конфиги с `auto_pause_enabled` → `compute_live_kpi` → `evaluate_kpi_guard` → при breach
  `set_running(config_id, False)` + событие `kpi_guard_paused` (payload: rule, actual, threshold, kpi-снимок).
  **Плюс on-close hook:** после `_mark_position_closed` (`service.py:2005`, вызовы из 1786/929/3499/4157)
  пересчёт guard для быстрой реакции (закрытие в убыток → мгновенная проверка).
- **Файлы:** `app/worker/tasks.py` (EDIT, по образцу `sweep_agent_data_freshness`); `service.py`
  (метод `check_kpi_breaches(session)` + hook).
- **Acceptance:** breach ⇒ `is_running=False` + `kpi_guard_paused` + последующие сигналы скипаются
  (`signal_skipped_config_inactive`); идемпотентность (повторный tick на уже-paused — no-op, без дубль-события);
  `auto_pause_enabled=False` ⇒ ничего; sweep не делает exchange-вызовов сверх T0.1 baseline-fetch.
- **Verification:** integration — стратегия с max_dd, открытая позиция в просадке → cron → paused + событие;
  on-close: закрытие убыточной сделки за лимит дня → paused; повторный прогон idempotent. Cron
  зарегистрирован (`task.labels`).
- **Depends:** T1.2

> **⛳ Checkpoint A — ядро AC#4.** `pytest -k "kpi or guard or live_kpi"` зелёный · full suite без регрессий ·
> ruff+mypy чисто · 0024 up/down · **пороги по умолчанию off; калибровка с трейдерами зафиксирована перед
> включением** (реальные деньги). **AC#4 «Loss/day + Max DD auto-pause» закрыт.**

### Phase 2 — Volatility Kill-Switch 🔴 (параллельный трек)

#### T2.1 — Schema + миграция 0025
- **Цель:** `volatility_kill_enabled` bool · `volatility_kill_atr_mult` float (spike = текущий ATR / baseline ATR
  ≥ mult) · `volatility_kill_lookback` int (баров для baseline).
- **Файлы:** `auto_trade_risk_config.py` (EDIT); `migrations/.../20260604_0025_add_volatility_kill.py`
  (`down_revision="20260604_0024"`); схема + upsert.
- **Acceptance:** nullable=off; CheckConstraints (`atr_mult > 1`, `lookback >= 2`).
- **Verification:** 0025 up/down; round-trip.
- **Depends:** T1.1 (порядок миграций)

#### T2.2 — Детекция spike в watcher-тике
- **Цель:** в `run_position_watcher_tick` (`app/services/watchers/service.py:301`) на каждом тике для позиции
  конфига с `volatility_kill_enabled`: ATR-spike = `compute_atr(short)` / `baseline_atr(lookback)` ≥ mult.
  Переиспользовать `live_tracker.compute_atr` (`sl_tp/live_tracker.py:115`) / `indicator_watcher._compute_atr`
  (`watchers/indicator_watcher.py:137`) — pure, без exchange-вызовов (kline-буфер уже загружен в тике).
- **Файлы:** `app/services/watchers/service.py` (EDIT) или новый `app/services/risk/volatility_kill.py`.
- **Acceptance:** spike ≥ mult ⇒ trigger; ниже ⇒ no-op; недостаточно баров ⇒ no-op (fail-safe).
- **Verification:** unit на детектор (spike/no-spike/insufficient-bars) на синтетическом kline-буфере.
- **Depends:** T2.1

#### T2.3 — Kill-действие (close reduce-only + pause)
- **Цель:** при trigger — закрыть открытые позиции конфига (`_flatten_single_position` / `adapter.partial_close`
  / `place_futures_market_order(reduce_only=True)`, `service.py:950/958`) + `set_running(False)` + событие
  `volatility_kill_switch` (payload: atr_now, baseline, mult, symbol). Идемпотентно (если уже закрыто/paused).
- **Файлы:** `service.py` (метод `trigger_volatility_kill(config, position, ...)`); вызов из T2.2.
- **Acceptance:** позиция закрыта reduce-only, стратегия paused, событие записано; повторный тик — no-op;
  `volatility_kill_enabled=False` ⇒ ничего.
- **Verification:** integration с fake-adapter — spike → close-вызов + paused + событие; idempotent повтор.
- **Depends:** T2.2

> **⛳ Checkpoint B.** `pytest -k volatility` · full suite без регрессий · ruff+mypy · 0025 up/down ·
> kill идемпотентен и **никогда не открывает** (только reduce-only/pause).

### Phase 3 — Portfolio DD watcher (W11-overlap) 🟠

#### T3.1 — Portfolio-risk + расчёт портфельного DD
- **Цель:** per-user порог `portfolio_max_dd_pct` + расчёт портфельной просадки (Σ live-KPI по конфигам).
  Хранение: новая 1-на-юзера таблица `auto_trade_portfolio_risk` (или поле — решить в ревью).
- **Файлы:** `app/models/auto_trade_portfolio_risk.py` (NEW) + миграция `20260604_0026` (NEW); расчёт поверх
  `compute_portfolio` (`app/services/auto_trade/portfolio.py`) + T0.1 по каждому конфигу.
- **Acceptance:** портфельный running-DD = по агрегированной equity-кривой; nullable=off.
- **Verification:** 0026 up/down; тест агрегации DD по 2+ конфигам.
- **Depends:** T0.1

#### T3.2 — Cron → pause-all
- **Цель:** `check_portfolio_drawdown()` — cron (`*/5 * * * *`, `portfolio_dd_every_5m`) → если
  `portfolio_dd_pct >= portfolio_max_dd_pct` → `set_running_bulk(user_id, False)` + событие
  `portfolio_dd_paused_all`.
- **Файлы:** `app/worker/tasks.py` (EDIT); `service.py` метод.
- **Acceptance:** breach ⇒ все стратегии юзера paused + событие; идемпотентно; off ⇒ ничего.
- **Verification:** integration — портфельный DD за порог → bulk-pause; повтор idempotent; cron зарегистрирован.
- **Depends:** T3.1

> **⛳ Checkpoint C.** портфельная пауза работает · идемпотентно · ruff+mypy · 0026 up/down.

### Phase 4 — Integration & DoD

#### T4.1 — Финальный гейт + AC#4-маппинг
- **Verification:** full `pytest` зелёный (incl. существующие 747) · `ruff format --check`/`ruff check`/`mypy app` ·
  `alembic upgrade head`→`downgrade -1` для 0024/0025/0026 · кроны `kpi_guard_every_5m`/`portfolio_dd_every_5m`
  зарегистрированы · маппинг на AC#4 (Max DD ✅, Loss/day auto-pause ✅, volatility kill ✅, portfolio pause ✅).
- **Depends:** Checkpoints A, B, C.

---

## 3. Календарь (≈W10–W11, ~1.5–2 FTE)

| День | Основной трек (AC#4 ядро) | Параллельно |
|---|---|---|
| 1–2 | T0.1 live-KPI + фикс I6 | — |
| 3 | T1.1 schema/migr 0024 → T1.2 evaluate | T2.1 schema/migr 0025 |
| 4 | T1.3 cron + on-close + pause — **Checkpoint A** | T2.2 spike-детектор |
| 5 | T3.1 portfolio-risk + DD | T2.3 kill-действие — **Checkpoint B** |
| 6 | T3.2 portfolio pause-all — **Checkpoint C** | — |
| 7 | T4.1 full gate + AC#4 review | калибровка порогов с трейдерами |

---

## 4. Риски и митигации
- **Реальные деньги:** все пороги default off; включение auto-pause/kill только после калибровки с трейдерами
  (значения `max_dd_pct`, daily-loss, `atr_mult`, `portfolio_max_dd_pct`). Документировать.
- **Over-pause на шуме:** `kpi_guard_min_trades` + `insufficient_data` (фикс I6) не паузят свежую стратегию.
- **Cron-overlap:** sweep лёгкий + `set_running(False)` идемпотентен; один scheduler-инстанс (context7).
- **Exchange-вызовы:** баланс/mark только в T0.1 на cron-каденсе (не в signal hot-path); кэшировать при росте числа стратегий.
- **Регрессии:** всё за флагами; `auto_pause_enabled`/`volatility_kill_enabled`/`portfolio_max_dd_pct` = NULL/false
  ⇒ поведение без изменений (явный no-regression тест).

## 5. Вне scope (defer, согласовать)
- W10 Strategy Promotion Pipeline · anomaly detection · внешние каналы алертов (Telegram/email — хватает события
  + лог) · risk-on/risk-off режим (опционально поверх kill-switch) · UI (Risk Config / Live Monitor — отдельный
  фронт-трек W12).

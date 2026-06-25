# TODO — Phase 1 (W10 backend: AC#4 portfolio-DD + AC#7 live-KPI)

Источник истины: [phase1-plan.md](phase1-plan.md). Границы: **не трогаем** execution/open/close и существующий per-strategy KPI-Guard/kill-switch — только добавляем портфельный watcher и freshness агрегата.
Легенда: `[S]`≈≤0.5д · `[M]`≈1–1.5д · deps — предшественники.

## Фаза B2 — Portfolio-DD watcher (AC#4, портфельный уровень)
- [x] **P1-T1** [S] Settings: `portfolio_dd_halt_enabled=False`, `portfolio_dd_halt_threshold_pct=20.0`, `kpi_freshness_seconds=300` · deps: — · `core/config.py` · ✅ `0f83b5c`
- [x] **P1-T2** [M] Сервис `sweep_portfolio_dd_guards`: per-user worst max-DD (`compute_strategy_health`, best-effort) → при ≥ порога `set_running_bulk(False)` + эмит `portfolio_dd_halt` · deps: P1-T1 · `auto_trade/service.py` · ✅ `876ff28`
- [x] **P1-T3** [S] Cron `portfolio_dd_every_5m` + `RISK_EVENTS |= {portfolio_dd_halt}` + ветка `format_event` · deps: P1-T2 · `worker/tasks.py`, `notifications/formatting.py` · ✅ `063d885`

### ▣ Checkpoint A — ✅ ПРОЙДЕН (B2 complete)
- [x] `pytest tests/ -q` → **850 passed, 1 skipped** (новые B2-тесты + без регрессий) · worst-DD≥порога → все стратегии юзера на паузе → 1 risk-событие (`portfolio_dd_halt`, notifiable) · 2-й sweep = halt 0 (идемпотентность) · cron `portfolio_dd_every_5m` зарегистрирован · `ruff` / `mypy` без новых ошибок

## Фаза B4 — Live-KPI в portfolio summary (AC#7)
- [x] **P1-T4** [M] `compute_portfolio`: при отсутствии/устаревании снапшота (> `kpi_freshness_seconds`) у **running**-стратегии → request-time `compute_strategy_health`; добавлен `kpi_as_of` в `StrategyPortfolioEntry` · deps: P1-T1 · `auto_trade/portfolio.py` · ✅ `3d7c9d7`
- [x] **P1-T5** [S] Прокинуть `kpi_as_of` в `StrategyPortfolioEntryRead` (per-strategy) · deps: P1-T4 · `api/v1/endpoints/live.py`, `schemas/auto_trade.py` · ✅ `910034d`

### ▣ Checkpoint B — ✅ ПРОЙДЕН (B4 complete)
- [x] `pytest tests/ -q` → **853 passed, 1 skipped** · running-стратегия без снапшота → live-KPI непустые + `kpi_as_of` (P1-T4 тесты) · «свежий» снапшот без перерасчёта (`compute_strategy_health` not called) · `GET /portfolio` отдаёт `kpi_as_of` на каждую стратегию · lint/typecheck без новых ошибок

## Завершение
- [x] **P1-T6** [S] Регенерация контракта (`kpi_as_of` в типах фронта; Node 20) · deps: P1-T5 · `openapi.json` (back ✅ `dbdf324`), `../constructor-front` (front ✅ `bc11842`) — additive, 0 путей +/−, tsc OK
- [ ] **P1-CR** [non-code] Письмо заказчику: перенос W10 Promotion Pipeline + W12 anomaly → M5 · deps: — · owner: PM · **на этой неделе**

## Code-review fixes (5-axis review of Phase 1)
- [x] **C1** [Critical] bound `portfolio_dd_halt_threshold_pct` to (0,100] + sweep guard — prevent mass-halt from a 0/negative threshold · ✅ `aa43b96`
- [x] **I1** [Important] lost portfolio-DD alert now loud (CRITICAL) + halt still counted, never silent · ✅ `b02833d`
- [x] **I3** [Important] cron surfaces per-tick `errors` at WARNING (best-effort, not a guaranteed circuit breaker) · ✅ `b02833d`
- [x] **I2** [Important] widen `kpi_freshness_seconds` 300→600 — decouple from cron, kill the boundary recompute storm · ✅ `0f4b0b1`
- [x] **S1** [Suggestion] formatter escaping invariant + non-numeric-payload test · ✅ `1a8e301`
- [x] **S3** [Suggestion] `window_days` in alert payload — label the DD as historical · ✅ `1a8e301`
- [x] **S2** [Suggestion] extract shared `as_aware_utc` (was duplicated) · ✅ `3f8eec0`
- [x] **S4** [Suggestion] extract `_resolve_strategy_kpis` helper · ✅ `2e8d8c8`
- [x] **S5** [Suggestion] within-window staleness coverage test · ✅ `2e8d8c8`

Final: **864 passed, 1 skipped**; ruff/mypy — no new errors (pre-existing debt only).

---
⚠️ Перед включением `portfolio_dd_halt_enabled=True` — **калибровка порога с трейдерами** (реальные деньги).

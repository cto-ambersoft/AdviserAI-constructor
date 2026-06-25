# Phase 4 — TODO (Promotion Pipeline + Anomaly Detection)

> Полный план: [phase4-plan.md](phase4-plan.md). Статус: [m4-closeout-plan.md → PHASE 4](m4-closeout-plan.md).
> Легенда: `[ ]` todo · `[~]` в работе · `[x]` готово · 🔴 must · 🟡 опц.

## 0. Блокеры — ✅ ВСЕ РЕШЕНЫ (2026-06-18)
- [x] 🔴 Решение #1: sandbox = **paper/dry-run**.
- [x] 🔴 Решение #2: promote = **только step-up**.
- [x] Решение #3: аномалии = **PnL-z, DD-velocity, win-rate-collapse, trade-freq** (4).
- [x] Решение #4: anomaly на демо = **включён** (alert-only; column-default off, демо on per-config).

## PHASE 4A — безопасный фундамент (нет изменения торговли)

### P4-1 🔴 Lifecycle stage foundation (B5)
- [x] `promotion/state_machine.py`: `LifecycleStage`/`PromotionTrigger` Enum + `VALID_TRANSITIONS` + `apply_transition` (raise on invalid). ✅
- [x] Тест `test_promotion_state_machine.py` — 12 кейсов зелёные. ✅
- [x] Migration `0031`: `lifecycle_stage` (String16, server_default `live`) + backfill всех существующих → `live`. ✅ (up/down/up verified :55432)
- [x] Model `auto_trade_config.py`: поле + CheckConstraint `IN (...)`. ✅
- [x] API: отдавать `lifecycle_stage` в `AutoTradeConfigRead` (openapi подтверждён). ✅
- [ ] **Перенос в P4-4:** flip «новые конфиги → sandbox» — вместе с enforcement (сейчас default `live`, behavior-neutral).
- [ ] Front: бэдж стадии на карточке стратегии.

### P4-5 🔴 Anomaly detector — pure (B6)
- [x] `anomaly/detector.py`: `per_trade_pnl_zscore`, `trade_frequency_baseline`, `detect_anomalies`, `AnomalyFinding` (4 метрики). NaN-z → не аномалия. ✅
- [x] Тест `test_anomaly_detector.py` — 7 кейсов (spike→finding; flat→0; short→0; wr-collapse; freq-spike). ✅
- [x] Migration `0032`: `anomaly_detection_enabled` (default False), `anomaly_z_threshold`, `anomaly_window` + схема/upsert-маппинг. ✅ (up/down/up verified :55432)

### 🔲 CHECKPOINT 1 — review (фундамент, без влияния на сделки)

## PHASE 4B — решающая логика (нужна калибровка)

### P4-2 🔴 KPI Gate + promote/demote (B5) — deps P4-1 ✅ BACKEND DONE
- [x] `promotion/kpi_gate.py`: `PromotionDecision` из `StrategyHealth` + min_sandbox + пороги; fail-safe insufficient_data. ✅
- [x] Migration `0033`: `promote_*` пороги (bounded CHECKs) + schema + upsert-маппинг. ✅
- [x] `service.py`: `promote_strategy` (gate-checked, FSM, emit) + `demote_strategy` + `get_promotion_status`. Live только после gate_passed. ✅
- [x] API: `POST /auto-trade/strategies/{id}/promote` (RequireStepUp, 422 gate-fail/409 non-sandbox), `/demote`, `GET /{id}/promotion-status`. ✅
- [x] Migration `0034`: `sandbox_entered_at` (KPI-Gate min-period clock). ✅
- [x] Тесты `test_promotion_kpi_gate.py`(7) + `test_promotion_service.py`(6) + `test_promotion_endpoints.py`(5). ✅
- [ ] **Front (Checkpoint 3):** gate-status панель + Promote(step-up)/Demote кнопки + `npm run gen:api-types`.

### P4-6 🔴 Anomaly cron + event (B6) — deps P4-5 ✅ BACKEND DONE
- [x] `worker/tasks.py`: `sweep_strategy_anomalies` cron `*/15`; series bounded to last 200 closed trades (review S5). ✅
- [x] Событие `strategy_anomaly_detected`: `formatting.py` RISK_EVENTS + `events/stream.py` SSE (+Telegram). ✅
- [x] Дедуп: 60-мин per-config cooldown. ✅
- [x] Тест `test_anomaly_sweep.py` (4: selection, flat=no-alert, disabled-skip, cooldown). ✅
- [ ] **Front (Checkpoint 3):** лента аномалий + `strategy_anomaly_detected` в `risk-events-store.ts`.

### 🔲 CHECKPOINT 2 — review + калибровка порогов с трейдерами

## PHASE 4C — полное замыкание (риск-критичный путь + UI)

### P4-3 🔴 Auto-eval cron + promotion_ready (B5) — deps P4-2 ✅ BACKEND DONE
- [x] `sweep_promotion_gates` cron `*/30`: sandbox→health→gate→emit `promotion_ready` (не авто-промоут, 6h cooldown dedup). ✅
- [x] События `promotion_ready/strategy_promoted/strategy_demoted/promotion_gate_failed` зарегистрированы (RISK_EVENTS+SSE+Telegram). ✅
- [x] Тест `test_promotion_gate_sweep.py` (4). ✅
- [ ] **Front (Checkpoint 3):** toast «готова к промоуту».

### P4-4 ✅ Sandbox validation (B5) — deps P4-1 — ГОТОВО
> **РЕШЕНИЕ (2026-06-18, финал):** sandbox-валидация = **реальные сделки на demo-аккаунте** (НЕ backtest, НЕ paper-движок). Sandbox на demo-кредах → реальные закрытые сделки → существующий `compute_strategy_health` → KPI Gate. Переиспользует весь execution+health код, ноль нового движка, реальные fills/slippage. Backtest-вариант отвергнут: auto-trade входит по AI-сигналам (`enqueue_history_signal`/`PersonalAnalysisHistory`), а builder-backtest — по индикаторам; честного backtest'а для AI-стратегии нет.
- [x] **Slice 1 — safety gate:** базовый блок не-live исполнения. ✅
- [x] **Slice 2a — prep:** pure `health_from_trades()` — общий метрик-кор. 3 теста. ✅
- [x] **Slice 2b — demo-валидация:** инвариант «не-live → только demo» (`set_running`/entry-guard через `_account_is_demo`); sandbox на demo торгует → gate уже читает через `compute_strategy_health` (ноль нового wiring). Гард-тесты переписаны. Регресс 425. ✅
- [x] **Flip:** новые конфиги → `sandbox` (+`sandbox_entered_at`); валидация на demo → гейт (step-up) → live. Полный прогон **980 passed, 1 skipped** (ноль регресса). ✅
- [ ] *(follow-up, опц.)* Promote: авто-switch кредов demo→real при переводе в live (сейчас юзер сам ставит real-аккаунт через upsert после промоушена).

### P4-7 🔴 Anomaly UI (B6) — deps P4-6
- [ ] `risk-events-store.ts` +`strategy_anomaly_detected`; лента/бэйдж на Live Monitor.
- [ ] Фронт-смоук.

### P4-8 🟡 Auto-reaction на critical — deps P4-6 + P4-2
- [ ] За флагом `anomaly_auto_react_enabled` (default False): critical → demote/pause + событие.

### 🔲 CHECKPOINT 3 — финальный QA + acceptance + staging deploy
- [ ] Полный `uv run pytest` зелёный (особо регресс ордер-пути).
- [ ] `npm run gen:api-types` (Node≥18) после новых роутов.
- [ ] Acceptance-демо: Sandbox→KPI Gate→Live (step-up) + anomaly-алерт.
- [ ] Staging deploy (Makefile trade-стек); финальная калибровка порогов.

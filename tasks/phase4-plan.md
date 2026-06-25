# Phase 4 — Implementation Plan: Strategy Promotion Pipeline (W10) + Anomaly Detection (W12)

> **Скоуп:** два направления состава работ договора M4, возвращённые в активную разработку (решение 2026-06-18, change-request отменён). См. статус в [m4-closeout-plan.md → PHASE 4](m4-closeout-plan.md).
> **Источник задач:** контрактный план M4 (W10, W12) + аудит кода (оба направления — 0% в коде).
> **Сервисы:** `constructor` (бэк, основной объём) · `constructor-front` (UI) · `core` не затрагивается.
> **Режим:** план составлен в read-only. Код не менялся. Документация по taskiq/pandas — context7-verified ([m4-closeout-plan.md → Appendix C](m4-closeout-plan.md)).

---

## 1. Цель и определение готовности

| Направление | Контрактный критерий | DoD |
|---|---|---|
| **B5 Promotion Pipeline** (W10) | Формальный lifecycle Deep Research→Sandbox→KPI Validation→Live, FSM с guard-условиями; KPI Gate авто-проверяет Min Win Rate, Max DD, min sandbox-период перед Live | Стратегия проходит стадии через FSM; перевод в Live возможен только при пройденном KPI Gate + step-up; sandbox не торгует реальными деньгами |
| **B6 Anomaly Detection** (W12) | Strategy anomaly detection (наблюдаемость) | Статистический детектор отклонений поведения стратегии; high/critical → событие в SSE/Telegram; off-by-default до калибровки |

---

## 2. Архитектурные решения — ✅ ЗАФИКСИРОВАНЫ (2026-06-18)

> Все 4 решения приняты заказчиком/командой. P4-2 и P4-4 разблокированы.

1. **«Sandbox»-исполнение = (A) paper / dry-run** ✅ — на live-сигналах сделки симулируются (записываются в `auto_trade_positions` с пометкой sim), реальные ордера на биржу НЕ уходят. KPI Gate берёт сделки из того же `auto_trade_positions` (sim-сделки в sandbox).
2. **Promotion = только через step-up** ✅ — cron авто-ОЦЕНИВАЕТ гейт и эмитит `promotion_ready`; фактический перевод в Live — только step-up'ом (консистентно с play/upsert/exchange-key под `RequireStepUp`). Авто-промоут запрещён.
3. **Набор аномалий = 4** ✅ — PnL z-score, drawdown-velocity, win-rate-collapse, trade-frequency. Достаточно на старт.
4. **Anomaly на демо = ВКЛЮЧЁН** ✅ — column-default `anomaly_detection_enabled=False` (прод-safe), но на демо включаем per-config (alert-only; авто-реакция P4-8 остаётся off). Риск низкий: детектор только алертит, не трогает сделки.
3. **Где живут пороги?** → расширяем существующую сателлит-таблицу **`auto_trade_risk_configs`** ([auto_trade_risk_config.py](../app/services/../app/models/auto_trade_risk_config.py)) — additive, все поля nullable, off-by-default (тот же паттерн, что kpi_guard_*/kill_switch_*). Не плодим новую таблицу.
4. **FSM-стек?** → ручной dict `VALID_TRANSITIONS` + `Enum`, как [position/state_machine.py](../app/services/position/state_machine.py). **Не** тянуть `transitions`/`python-statemachine`.
5. **Метрики?** → переиспользовать `compute_strategy_health` / `StrategyHealth` ([health.py:173](../app/services/auto_trade/health.py)). KPI Gate = инверсия `kpi_guard.py` (промоут при passed, а не пауза при breached).

---

## 3. Граф зависимостей

```
ДЕКОР: ── = depends on; ║ = независимые потоки (можно параллелить)

B5 (Promotion)                              B6 (Anomaly)
─────────────────                           ─────────────────
P4-1 Stage foundation                       P4-5 Detector (pure)
  (migration+FSM+API read+UI badge)           (z-score/EWM + thresholds + tests)
        │                                            │
        ▼                                            ▼
P4-2 KPI Gate + promote/demote              P4-6 Anomaly cron + event
  (gate decision + step-up API + UI)          (sweep + strategy_anomaly_detected + SSE/TG)
        │                                            │
        ▼                                            ▼
P4-3 Auto-eval cron + promotion_ready       P4-7 Anomaly UI (Live Monitor feed)
        │
        ▼
P4-4 Sandbox execution gate ◄──── РЕШЕНИЕ #1 (paper-path)
  (gate real orders in process_signal_queue)

P4-8 (опц.) Auto-reaction: critical anomaly → demote/pause
  ── depends on P4-6 (anomaly event) И P4-2 (demote_strategy)

Общая инфраструктура событий: формат регистрации нового события одинаков —
  app/services/notifications/formatting.py (RISK_EVENTS) +
  app/services/events/stream.py (allowed SSE types) +
  constructor-front/stores/risk-events-store.ts (named list)
```

**Критический путь:** P4-1 → P4-2 → P4-4 (самый длинный, B5). B6 (P4-5→P4-7) короче и полностью независим до P4-8. → **вести оба потока параллельно**.

---

## 4. Вертикальные срезы (задачи)

Каждый срез — один полный путь (DB → service → API → event → UI, где применимо), демонстрируемый и тестируемый отдельно. НЕ горизонтальные слои.

### ▶ PHASE 4A — Безопасный фундамент (без изменения торгового поведения)

#### P4-1 · Lifecycle stage foundation (B5)
**Полный путь:** migration → model → FSM → API read → UI badge. **Поведение торговли не меняется** (гейтинг исполнения — позже, в P4-4).
- **Файлы:**
  - `migrations/versions/20260618_0031_add_lifecycle_stage.py` — колонка `lifecycle_stage` (String(16), NOT NULL, server_default `'live'`); **backfill: все существующие конфиги → `live`** (нулевой регресс). Новые конфиги через API создаются в `sandbox` (логика в сервисе, не в дефолте колонки).
  - [app/models/auto_trade_config.py](../app/models/auto_trade_config.py) — поле `lifecycle_stage` + CheckConstraint `lifecycle_stage IN ('research','sandbox','validation','live','rejected','archived')`.
  - `app/services/auto_trade/promotion/state_machine.py` (новый) — `LifecycleStage(str, Enum)`, `PromotionTrigger(str, Enum)`, `VALID_TRANSITIONS` dict, `apply_transition(stage, trigger) -> LifecycleStage` (raise on invalid).
  - [app/api/v1/endpoints/live.py](../app/api/v1/endpoints/live.py) + схема read — отдавать `lifecycle_stage` в `AutoTradeConfigRead`.
  - constructor-front: бэдж стадии на карточке стратегии (Live Monitor / dashboard).
- **Acceptance:** каждый конфиг отдаёт `lifecycle_stage`; backfill выставил всем существующим `live`; FSM отклоняет невалидный переход (напр. `research`→`live`).
- **Verification:** `uv run pytest tests/test_promotion_state_machine.py`; alembic up/down на throwaway PG (:55432, см. memory); `GET /auto-trade/config` показывает поле; UI рендерит бэдж.

#### P4-5 · Anomaly detector — чистые функции (B6)
**Полный путь:** detector module + thresholds + unit tests. Без wiring (cron/UI — позже). Независим от B5.
- **Файлы:**
  - `app/services/auto_trade/anomaly/detector.py` (новый) — чистые функции: `per_trade_pnl_zscore(net_pnls, window) -> pd.Series`, `trade_frequency_baseline(counts, alpha) -> pd.Series`, `detect_anomalies(series, cfg) -> tuple[AnomalyFinding, ...]`; `@dataclass(frozen=True) AnomalyFinding(metric, value, baseline, z_score, severity)`. pandas `rolling().mean()/.std()` + `ewm().mean()` (Appendix C).
  - [app/models/auto_trade_risk_config.py](../app/models/auto_trade_risk_config.py) + migration `..._0032_add_anomaly_thresholds.py` — `anomaly_detection_enabled` (Bool, default **False**), `anomaly_z_threshold` (Float, nullable, дефолт-в-движке ~3.0), `anomaly_window` (Int, nullable).
  - **Fail-safe:** `rolling().std()` → NaN пока окно не заполнено (`min_periods=window`) → NaN-z трактуется как «недостаточно данных», не аномалия (зеркало `insufficient_data` в health).
- **Acceptance:** на синтетическом ряде с PnL-выбросом >3σ → ≥1 finding; на ровном ряде → 0 findings; короткий ряд (< window) → 0 findings.
- **Verification:** `uv run pytest tests/test_anomaly_detector.py`; alembic up/down 0032.

> **🔲 CHECKPOINT 1 — review.** Оба среза НЕ меняют торговое поведение (P4-1 backfill→live, P4-5 off-by-default). FSM- и detector-тесты зелёные. Ревью архитектуры стадий и набора аномалий ДО того, как что-то начнёт влиять на сделки.

---

### ▶ PHASE 4B — Решающая логика (требует калибровки)

#### P4-2 · KPI Gate + promote/demote (B5) — depends P4-1
**Полный путь:** gate decision → thresholds → service methods → step-up API → UI promote/demote + gate-status.
- **Файлы:**
  - `app/services/auto_trade/promotion/kpi_gate.py` (новый) — чистое решение по образцу [kpi_guard.py](../app/services/auto_trade/risk/kpi_guard.py): на вход `StrategyHealth` + `min_sandbox_days` (по `last_started_at`/первому входу в sandbox) + пороги → `@dataclass PromotionDecision(can_promote: bool, passed: tuple, failed: tuple[GateCriterion])`. Критерии: `promote_min_win_rate_pct`, `promote_max_dd_pct`, `promote_min_trades`, `promote_min_sandbox_days`. Fail-safe: `insufficient_data`/sample < min_trades → НЕ промоутить.
  - migration `..._0033_add_promotion_gate_thresholds.py` — `promote_*` поля в `auto_trade_risk_configs` (nullable, консервативные дефолты; калибровать с трейдерами).
  - [app/services/auto_trade/service.py](../app/services/auto_trade/service.py) — `promote_strategy(config_id)` (FSM `request_promotion`→gate→`promote_to_live`; emit) и `demote_strategy(config_id)` (live→sandbox; reuse `_emit_event` @ ~1594/1626). Перевод в `live` разрешён ТОЛЬКО после `gate_passed`.
  - [app/api/v1/endpoints/live.py](../app/api/v1/endpoints/live.py) — `POST /auto-trade/{id}/promote` под `RequireStepUp`; `POST /auto-trade/{id}/demote`. `GET /auto-trade/{id}/promotion-status` → gate criteria pass/fail + дни в sandbox.
  - constructor-front: панель «Gate status» (критерии + дни в sandbox), кнопки Promote (step-up-модалка, как play) / Demote.
- **Acceptance:** sandbox-стратегия с KPI ≥ порогов и выдержанным min-sandbox → gate `can_promote=true`, promote проходит со step-up; Max DD выше порога → promote отклонён (422/«gate failed»); promote без step-up → 401/перехват step-up.
- **Verification:** `uv run pytest tests/test_kpi_gate.py tests/test_promotion_service.py`; `npm run gen:api-types` после новых роутов (Node≥18, Appendix B); ручной round-trip в UI.

#### P4-6 · Anomaly cron + event (B6) — depends P4-5
**Полный путь:** cron → detector → event → SSE/Telegram registration.
- **Файлы:**
  - [app/worker/tasks.py](../app/worker/tasks.py) — `sweep_strategy_anomalies` (`@broker.task(schedule=[{"cron": "*/15 * * * *", "schedule_id": "anomaly_sweep_every_15m"}])`, Appendix C). По running-конфигам: серия сделок из `auto_trade_positions` (W9 net-PnL) → `detect_anomalies` → high/critical → emit.
  - Регистрация события `strategy_anomaly_detected`: [formatting.py:30](../app/services/notifications/formatting.py) (`RISK_EVENTS` + формат сообщения) + [events/stream.py:34](../app/services/events/stream.py) (allowed SSE types).
  - **Дедуп:** only-on-transition + cooldown (не алертить одну аномалию каждый тик; паттерн как `data_stale`).
- **Acceptance:** при синтетической аномалии — ровно одно событие `strategy_anomaly_detected` в открытый SSE + Telegram; повторный тик той же аномалии события не плодит; при `anomaly_detection_enabled=False` — тишина.
- **Verification:** `uv run pytest tests/test_anomaly_sweep.py`; ручной тест SSE (открыть `/events/stream`, инжектнуть аномалию).

> **🔲 CHECKPOINT 2 — review + калибровка.** Появилось первое решающее поведение (promote-gate, anomaly-alert). **Откалибровать пороги `promote_*` и `anomaly_z_threshold` с трейдерами — не «на глаз» (реальные деньги).** Решить, включать ли anomaly на демо. Ревью step-up-флоу promote.

---

### ▶ PHASE 4C — Полное замыкание (риск-критичный путь + UI)

#### P4-3 · Auto-eval cron + promotion_ready (B5) — depends P4-2
**Полный путь:** cron авто-оценки → event → UI toast.
- **Файлы:**
  - [app/worker/tasks.py](../app/worker/tasks.py) — `evaluate_promotion_gates` (`*/30 * * * *`, Appendix C): по sandbox-конфигам считать health → KPI Gate; при `can_promote` — emit `promotion_ready` (НЕ авто-промоутить). Идемпотентность: not-spam (only-on-transition + cooldown).
  - Регистрация `promotion_ready`, `strategy_promoted`, `strategy_demoted`, `promotion_gate_failed` в `RISK_EVENTS` + SSE types + фронт-стор.
  - constructor-front: toast/бэйдж «готова к промоуту» на карточке.
- **Acceptance:** квалифицирующаяся sandbox-стратегия → одно `promotion_ready` событие; неквалифицирующаяся — без события.
- **Verification:** `uv run pytest tests/test_promotion_gates_sweep.py`; SSE smoke.

#### P4-4 · Sandbox execution gate (B5) — depends P4-1 + РЕШЕНИЕ #1
**Самый риск-критичный срез — трогает путь размещения ордеров. Делать ПОСЛЕ того, как FSM/gate проверены.**
- **Файлы:**
  - [app/services/auto_trade/service.py:3129](../app/services/auto_trade/service.py) (`process_signal_queue`) — для конфигов не в стадии `live`: путь paper/dry-run (записать sim-позицию в `auto_trade_positions` с флагом, **не** вызывать биржевой `create_order`). Маркер sim — новое поле/`close_reason`-конвенция или `raw_open_order={"sim": true}`.
  - Health/KPI Gate должны учитывать sim-сделки sandbox-стадии корректно (тот же `auto_trade_positions`).
- **Acceptance:** sandbox-стратегия по сигналу создаёт sim-позицию, реального ордера на бирже нет (проверить отсутствие вызова `create_order`); live-стратегия торгует как раньше (нулевой регресс по существующим тестам).
- **Verification:** `uv run pytest tests/` (полный прогон — регресс!); таргетный тест `tests/test_sandbox_paper_execution.py` (mock-биржа: assert `create_order` не вызван для sandbox).

#### P4-7 · Anomaly UI (B6) — depends P4-6
- **Файлы:** constructor-front `stores/risk-events-store.ts` (+`strategy_anomaly_detected` в named list), лента/бэйдж аномалий на Live Monitor.
- **Acceptance:** anomaly-событие появляется в ленте монитора с severity.
- **Verification:** фронт-смоук; событие из SSE рендерится.

#### P4-8 · (опц.) Auto-reaction на critical — depends P4-6 + P4-2
- За флагом (`anomaly_auto_react_enabled`, default False): `critical` → `demote_strategy` (P4-2) или `_auto_pause_strategy`. По умолчанию только алерт.
- **Acceptance:** при флаге on и critical-аномалии стратегия демоутится/паузится + событие; при off — только алерт.

> **🔲 CHECKPOINT 3 — финальный QA + acceptance.** Полный `uv run pytest` зелёный (особенно регресс ордер-пути после P4-4). Регенерить фронт-контракт (`npm run gen:api-types`). Acceptance-демо: lifecycle Sandbox→KPI Gate→Live (со step-up) + anomaly-алерт. Staging deploy (Makefile trade-стек). Финальная калибровка порогов с трейдерами.

---

## 5. Оценка и параллелизм

| Поток | Срезы | Оценка | Можно параллелить |
|---|---|---|---|
| B5 Promotion (бэк) | P4-1 → P4-2 → P4-3 → P4-4 | ~4–5 дн | критический путь |
| B6 Anomaly (бэк) | P4-5 → P4-6 → (P4-8) | ~3–4 дн | ║ параллельно B5 |
| UI | P4-1 badge, P4-2 gate panel, P4-7 anomaly feed | ~2.5–3 дн | следом за бэк-срезами |

**Итого ~7–9 дн бэк (параллельно ~5) + ~3 дн UI.**

---

## 6. Риски и предпосылки

- **Real-money safety:** P4-4 трогает путь ордеров — самый опасный срез, идёт последним и под полный регресс-прогон. Sandbox-paper (решение #1) должен гарантировать отсутствие реальных ордеров.
- **Калибровка порогов** (`promote_*`, `anomaly_z_*`) — с трейдерами, не «на глаз». Оба фичефлага off/conservative by default.
- **Миграции** 0031/0032/0033 — additive, проверять up/down на throwaway PG :55432 (см. memory [Alembic migration local verify]).
- **Фронт-контракт** — после новых роутов (`/promote`, `/demote`, `/promotion-status`) регенерить типы (Node≥18).
- **Предпосылка:** решения #1 и #2 (раздел 2) подтверждены до старта P4-2/P4-4.

---

## 7. Для ревью (вопросы заказчику/команде перед стартом)

1. Подтвердить sandbox = paper/dry-run (решение #1A)? Или нужен live-mini-size?
2. Promote — авто или только через step-up-подтверждение (решение #2)?
3. Набор аномалий на старт (PnL-z, DD-velocity, win-rate-collapse, trade-freq) — достаточно 3–4? Какие пороги-стартовые?
4. Включаем ли anomaly-детектор на демо или ship off до калибровки?

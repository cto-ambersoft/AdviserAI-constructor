# M4 Closeout Plan — статус + рабочий чеклист

> **Обновлено: 2026-06-18.** Бэк `constructor` @ `cbbf9f5`, фронт `constructor-front` @ `314a75b` — **развёрнуто в прод** (ambercore-server, контейнеры `app_trade*` + `app_trade_front`, миграции до `0030`).
> **Связанные доки:** ТЗ фронта [m4-frontend-tz.md](m4-frontend-tz.md) · [phase1-todo.md](phase1-todo.md) · [phase2-todo.md](phase2-todo.md) · [../RISK_GOVERNANCE.md](../RISK_GOVERNANCE.md). Тех-долг — раздел «🧹 Техдолг» ниже.
> **Сервисы:** `constructor` (FastAPI бэк) · `constructor-front` (Next.js) · `core` (NestJS/Mastra AI).

---

> ⚠️ **СТАТУС-ПОПРАВКА (2026-06-20, T22).** Заявление ниже («весь объём закрыт,
> формального риска приёмки нет») было **переоценкой готовности**. Независимый аудит
> ([reports/M4_AUDIT.md](../reports/M4_AUDIT.md)) выявил мёртвый-в-проде код,
> подменённую семантику, off-by-default governance и дропнутые обещания. Эти разрывы
> закрыты в плане ремедиации [m4-remediation-plan.md](m4-remediation-plan.md) /
> [m4-remediation-todo.md](m4-remediation-todo.md) (Phase 0–5, коммиты T1–T20). Этот
> close-out оставлен как исторический контекст; актуальный статус — в remediation-todo.

## 📍 ТЕКУЩИЙ СТАТУС (2026-06-19) — ВЕСЬ объём договора M4 закрыт и в проде

**Все 7 acceptance-критериев + весь состав работ договора M4 выполнены и задеплоены на прод** (`aitrade-trade.ambersoft.llc`, бэк `9322bdf`+, фронт `ff62db1`+).

Закрыто с прошлого статуса:
- **PHASE 1–3** (B1 2FA, B2 portfolio-DD watcher, B3 SSE, B4 live-KPI, login-2FA, F1–F5 + step-up) — ✅ в проде.
- **PHASE 4 — оба последних направления договора, ранее под риском приёмки, ДОДЕЛАНЫ (без change-request):**
  - **B5 Strategy Promotion Pipeline (W10)** — FSM lifecycle, KPI Gate, promote/demote (step-up), auto-eval cron `promotion_ready`. ✅
  - **B6 Strategy Anomaly Detection (W12)** — детектор (4 метрики) + sweep-cron `*/15` → SSE/Telegram. ✅
  - **P4-4 sandbox-валидация = реальные сделки на demo-аккаунте** (не backtest/paper); новые конфиги стартуют в `sandbox` → гейт (step-up) → live. ✅
  - **Фронт Phase 4 (FE-0…FE-4):** контракт-реген, SSE-события, Lifecycle UI (бэдж/gate/promote/demote), risk-config секции (anomaly + promote), anomaly-лента. ✅ + vitest-набор (19 тестов).
- **Прод-фиксы PnL (по факту на реальном счёте 17):** портфельный realized теперь из синканного ledger и **account-scoped** (1 саб-аккаунт = 1 стратегия — все сделки счёта = стратегии) → сходится с per-account PnL-картой; убран per-position `fetch_futures_trades` (загрузка /portfolio 20с→~1с); health-KPI на ledger-net базисе. ✅
- **График:** маркеры просто BUY/SELL (без AUTO/MANUAL), TP/SL открытой позиции линиями. ✅

**Тесты:** 985 passed / 1 skipped (бэк) + 19 (фронт). **Acceptance-разрыв «состав работ ≠ AC» закрыт полностью — формального риска приёмки нет.**

**Остаётся:** только M5-defer (D3/D5/D6/D7) + мелкий тех-долг + **калибровка риск-порогов с трейдерами** (см. ниже).

## 0. Статус Acceptance Criteria — ✅ всё закрыто

| # | Критерий | Бэк | Фронт | Примечание |
|---|---|---|---|---|
| 1 | Research Module (каталог + Δ vs Baseline) | ✅ | ✅ `/forecasts` | trader-UI каталога |
| 2 | Autonomous Execution (SL trailing/breakeven/volatility) | ✅ | ✅ | — |
| 3 | Multi-Strategy ≥3 без коллизий | ✅ | ✅ | sub-account/стратегию |
| 4 | Risk Enforcement (KPI-Guard авто-пауза + portfolio-DD) | ✅ | ✅ Risk Config UI | portfolio-DD watcher **off by default** (нужна калибровка) |
| 5 | Security (Vault + 2FA) | ✅ | ✅ | TOTP + step-up + recovery + lockout + **login-2FA** |
| 6 | Asset Expansion BTC + ETH | ✅ | ✅ | — |
| 7 | KPI Transparency live dashboard | ✅ | ✅ `/monitor` + SSE | KPI live (snapshot+recompute), `kpi_as_of` |

**Закрыто полностью: все 7.** Оговорки: portfolio-DD требует калибровки порога перед включением (реальные деньги); merged-equity портфельный DD — пока worst-strategy прокси (M5).

**Легенда статусов задач:** `[ ]` todo · `[~]` в работе · `[x]` готово · 🔴 must-for-acceptance · 🟡 defer-M5.

---

## ✅ Уже сделано — НЕ переделывать (reference)

- Backend W5–W9: Position FSM ([state_machine.py](../app/services/position/state_machine.py)), динам. SL/TP + Multi-TP ([sl_tp/](../app/services/sl_tp/)), Volatility Kill-Switch ([kill_switch.py](../app/services/sl_tp/kill_switch.py)), **KPI-Guard авто-пауза** ([kpi_guard.py](../app/services/auto_trade/risk/kpi_guard.py) + cron `*/5`), Pre-Trade Engine ([risk/engine.py](../app/services/auto_trade/risk/engine.py)), Health Score ([health.py](../app/services/auto_trade/health.py)), Data Freshness `*/4h`.
- core W1–W4: AI Forecast каталог + Delta-vs-Baseline, Reasoning Path / ai_trend, Agent Accuracy 7d/30d, Weight Suggestions.
- API Vault (Fernet `SecretCipher` — [security.py](../app/core/security.py)).
- **Telegram risk alerting** — `RISK_EVENTS` уже диспатчатся ([formatting.py:30](../app/services/notifications/formatting.py)). *(W12 risk-alerting считаем закрытым.)*
- Контракт фронта синхронизирован с бэком (85 paths, риск-схема в типах).
- **F1 Risk Config UI (AC#4)** — ✅ готов, commit `63b5b9a` (T1.2): [auto-trade-risk-section.tsx](../../constructor-front/components/auto-trade/auto-trade-risk-section.tsx), все секции (Pre-Trade / KPI Guard / Kill-Switch), round-trip GET↔PUT через `mapRiskConfigToForm`/`buildRiskConfigPayload`.

---

# PHASE 1 — W10 (до 13 июн): дешёвые бэк-победы + старт Risk UI + change-request

### 🔴 B2 — Portfolio-DD watcher (авто-пауза всех стратегий) · AC#4 · ~1 день
**Зачем:** портфельный уровень AC#4; `set_running_bulk` уже есть, нужен только триггер.
**Где:** новый taskiq-крон в [../app/worker/tasks.py](../app/worker/tasks.py); расчёт DD — [portfolio.py](../app/services/auto_trade/portfolio.py) (`portfolio_max_dd_pct`); пауза — `set_running_bulk(False)` ([service.py:1500](../app/services/auto_trade/service.py)).
- [ ] Порог портфельного DD: добавить настройку (env/таблица — решить, per-user или global).
- [ ] Крон `portfolio_dd_watcher` (`*/5 * * * *`): по каждому пользователю считать портфельный DD; при превышении → `set_running_bulk(False)`.
- [ ] Эмит risk-события `portfolio_dd_halt` → добавить в `RISK_EVENTS` ([formatting.py:30](../app/services/notifications/formatting.py)) + формат сообщения → автоматически уходит в Telegram.
- [ ] Идемпотентность: не паузить повторно уже остановленные; not-spam алерт.
- [ ] Тест: портфель из 3 стратегий, DD>порог → все `is_running=False`, одно событие.
**Acceptance-чек:** при синтетическом портфельном DD>порога все стратегии пользователя встают на паузу + один Telegram-алерт.

### 🔴 B4 — «Live» KPI вместо 5-мин снапшота · AC#7 · ~1 день
**Зачем:** `PortfolioSummary`/health сейчас отдают KPI из снапшота (до 5 мин устаревания, `None` до первого крона) — для «live dashboard» слабо.
**Где:** [portfolio.py](../app/services/auto_trade/portfolio.py), health-endpoint в [../app/api/v1/endpoints/live.py](../app/api/v1/endpoints/live.py).
- [ ] Решение: (A) считать KPI request-time из W9-ledger, **или** (B) согласовать «обновление раз в 5 мин» как приемлемое и задокументировать.
- [ ] Если (A): переиспользовать ledger-агрегации; вернуть Sharpe-proxy/WR/ROI/running-DD по точному net-PnL, а не из снапшота.
- [ ] Не отдавать `None`, когда есть открытые сделки, но снапшота ещё нет.
**Acceptance-чек:** `/auto-trade/portfolio` и `/strategies/{id}/health` отдают непустые KPI сразу после старта стратегии.

### ✅ F1 — Risk Config UI · AC#4 · **ГОТОВО** (commit `63b5b9a`, T1.2)
Все три секции (Pre-Trade / KPI Guard / Kill-Switch) + round-trip GET↔PUT реализованы и закоммичены ([auto-trade-risk-section.tsx](../../constructor-front/components/auto-trade/auto-trade-risk-section.tsx)). Поля под CHECK-границы бэка, `net`/`replace` помечены «not yet enforced».
- [x] Секция Pre-Trade Limits (daily-loss usdt/pct, max-open +per-symbol, exposure cap, leverage ceiling, conflicting-signal policy).
- [x] Секция KPI Guard (enabled, max_dd_pct, max_daily_loss usdt/pct, min_win_rate, min_trades).
- [x] Секция Kill-Switch (enabled, atr_spike_mult, atr_period, price_move_pct, cooldown).
- [x] Round-trip: прелоад из `GET /auto-trade/config` (`mapRiskConfigToForm`) + сабмит во вложенный `risk` (`buildRiskConfigPayload`).
- [ ] *(желательно)* e2e-проверка: правка порога в UI → `PUT` → значение в `auto_trade_risk_configs` → KPI-Guard срабатывает по новому порогу.
> 💡 Высвободилось ~2–3 дня фронта — двигай старт **F2 Live Monitor** в Phase 1/начало W11.

### ✅ CR — Change-request заказчику · ОТМЕНЁН (2026-06-18)
- [x] ~~Согласовать перенос W10 + anomaly в M5.~~ **Решение изменено: оба направления доделываем в M4** (см. [PHASE 4](#phase-4--m4-extension-w10--anomaly-возвращены-в-объём-m4)). Change-request больше не требуется — состав работ закрывается полностью.
- [ ] *(опц.)* Если по merged-equity Portfolio DD (D3) или worker-isolation (D5) понадобится перенос — согласовать отдельным письмом. Это **не** acceptance-критерии, риск формальной приёмки минимальный.

---

# PHASE 2 — W11 (16–20 июн): 2FA + SSE + Live Monitor

### 🔴 B1 — 2FA TOTP (+ email confirmation) · AC#5 · 3–4 дня
**Зачем:** договорный критерий; сейчас 0% (нет `pyotp`, нет схемы, нет эндпоинтов).
**Где:** новый `app/services/auth/totp.py`; модель `user_totp`; эндпоинты в [../app/api/v1/endpoints/auth.py](../app/api/v1/endpoints/auth.py); шифрование секрета — существующий `SecretCipher` ([security.py](../app/core/security.py)).
- [ ] Зависимость `pyotp` (`pyproject.toml` + `uv lock`).
- [ ] Миграция: таблица `user_totp` (`user_id`, `secret_encrypted`, `confirmed_at`, `recovery_codes_encrypted`, timestamps).
- [ ] **enroll** `POST /auth/2fa/enroll`: `secret = pyotp.random_base32()`; `uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Amber")`; хранить секрет **зашифрованным**, `confirmed_at=NULL` (не активна до verify); вернуть `uri` для QR.
- [ ] **verify** `POST /auth/2fa/verify`: `pyotp.TOTP(secret).verify(code, valid_window=1)` (±30 c, constant-time); при успехе — `confirmed_at=now`.
- [ ] **step-up** `POST /auth/2fa/step-up`: проверить код → выдать коротко-живущий JWT-scope (напр. 5 мин) для критичных действий.
- [ ] Гейтить критичные действия step-up'ом: **start auto-trade**, смена exchange-key, правка risk-config.
- [ ] Email-confirmation как второй фактор подтверждения (план: «TOTP + email»).
- [ ] Recovery codes (генерация + одноразовое использование).
- [ ] Тесты: enroll→verify happy-path; неверный код; истёкший step-up отклоняет критичное действие.
**Acceptance-чек:** без валидного step-up критичное действие отклоняется; с TOTP-кодом — проходит.

### 🔴 B3 — SSE event channel `/events/stream` · W12 · 1–2 дня
**Зачем:** «live» в AC#7 и UX 2FA/риск-событий; сейчас фронт только `setInterval`-polling.
**Где:** `sse-starlette` + новый роутер; источник — Redis pub/sub риск-событий (уже публикуются для watcher/kill-switch).
- [ ] Зависимость `sse-starlette`.
- [ ] `GET /events/stream` → `EventSourceResponse(generator)`; в генераторе подписка на Redis-канал событий (`risk_blocked`, `kpi_guard_triggered`, `kill_switch_triggered`, `strategy_auto_paused`, `data_stale`, `portfolio_dd_halt`).
- [ ] В цикле: `if await request.is_disconnected(): break`; `except asyncio.CancelledError: ... raise` для очистки; `send_timeout` + `Cache-Control: no-cache`.
- [ ] Auth по токену; фильтрация событий по `user_id`.
- [ ] Тест: подписка → эмит события → клиент получает.
**Acceptance-чек:** при срабатывании kill-switch событие приходит в открытый `EventSource` < 1 c.

### 🔴 F2 (старт) — Live Monitor KPI dashboard · AC#7 · 3–4 дня (докончить в W12)
> F1 уже закрыт (см. Phase 1) — можно начинать F2 раньше.
**Зачем:** по живым стратегиям сейчас видно только PnL; Sharpe/WR/ROI есть только для бэктестов.
**Где:** новый роут `app/(app)/monitor` в `constructor-front`; данные — `GET /auto-trade/portfolio` + `/strategies/{id}/health` (уже в контракте).
- [ ] Сводные KPI-карточки портфеля: realized/net/unrealized PnL, portfolio running-DD.
- [ ] Таблица per-strategy: Sharpe-proxy / Win Rate / ROI / running-DD / health-class / sample-size.
- [ ] Контролы: play/stop/close-positions, play-all/stop-all (эндпоинты уже есть).

---

# PHASE 3 — W12 (23–27 июн): доделать фронт + QA + acceptance

### 🔴 F2 (доделать) — Live Monitor
- [ ] Завершить карточки + таблицу; подключить **F5** SSE вместо polling.

### 🔴 F3 — AI Forecast Catalogue trader-UI · AC#1 · 2–3 дня
**Зачем:** полный каталог сейчас в админке; у трейдера «Select» цепляет forecast только в backtest-билдер, не в live-стратегию.
**Где:** вынести из `admin-ai-backtest-config-dashboard.tsx` в трейдерский раздел.
- [ ] Экран каталога: фильтры symbol/timeframe, метрики (Win/Sharpe/MaxDD + **Delta-vs-Baseline**).
- [ ] «Attach to strategy» — привязка forecast к live-стратегии (не только к бэктесту).

### 🔴 F4 — 2FA UI · AC#5 · ~2 дня · зависит от B1
- [ ] enroll: показать QR из `provisioning_uri` + ручной ввод секрета.
- [ ] verify: ввод 6-значного кода.
- [ ] step-up: модалка подтверждения перед критичными действиями (start auto-trade, смена ключа, правка risk-config).
- [ ] recovery codes UI.

### 🔴 F5 — SSE-консьюмер вместо polling · W12 · ~1 день · зависит от B3
- [ ] `EventSource('/events/stream')` на Live Monitor; обновлять KPI/статусы по событиям; убрать `setInterval` там, где он дублирует.

### QA / релиз
- [ ] Прогон бэк-тестов (`uv run pytest`), фронт-смоук.
- [ ] Staging deploy ([ambercore prod deploy](../../core/PLAN.md) — Makefile-стек trade).
- [ ] Acceptance review по таблице AC (раздел 0).
- [ ] Калибровка риск-порогов с трейдерами (реальные деньги!): `kpi_guard_max_dd_pct`, дневные лимиты, volatility-spike, портфельный DD — **не ставить «на глаз»**.

---

# PHASE 4 — M4-extension (W10 + anomaly) — ✅ ВСЁ СДЕЛАНО И В ПРОДЕ

> Раздел ниже — исходный план; B5/B6/D4 + P4-4 + фронт FE-0…FE-4 **выполнены и задеплоены** (см. статус вверху и [phase4-todo.md](phase4-todo.md) / [phase4-frontend-todo.md](phase4-frontend-todo.md)). Оставлен как reference.

> **Статус: активная разработка (решение 2026-06-18).** Эти два направления — часть **состава работ договора**, а не acceptance-критерии. Делаем их, чтобы закрыть M4 по составу работ полностью, без change-request.
> **Паттерны кодовой базы (следовать им, не изобретать):** FSM — ручной dict `VALID_TRANSITIONS` + `Enum`, как в [position/state_machine.py](../app/services/position/state_machine.py) (НЕ тянуть `transitions`/`python-statemachine`). Краны — taskiq `@broker.task(schedule=[{"cron": ..., "schedule_id": ...}])`, как в [worker/tasks.py](../app/worker/tasks.py). «Чистое решение + side-effect отдельно» — как [kpi_guard.py](../app/services/auto_trade/risk/kpi_guard.py) (decision) ↔ `AutoTradeService._auto_pause_strategy` (effect). Метрики — переиспользовать [health.py](../app/services/auto_trade/health.py) (`compute_strategy_health`/`StrategyHealth`) и W9-ledger, не считать заново. Снэппеты — Appendix C (context7-verified).

---

## 🔴 B5 — Strategy Promotion Pipeline · W10 · ~4–5 дней (бэк) + D4 UI

**Зачем (договор):** «формальный lifecycle: Deep Research → Sandbox → KPI Validation → Live, state machine с guard conditions; KPI Gate — авто-проверка Min Win Rate, Max DD и минимального периода в sandbox перед переводом в Live». Сейчас в коде 0% — у `AutoTradeConfig` есть только `enabled`/`is_running`, стадии жизненного цикла нет.

**Архитектурное решение (зафиксировать перед стартом):**
- **Стадии:** `research` → `sandbox` → `validation` → `live`, плюс терминальные `rejected` / `archived` (demote из live → `sandbox`).
- **«Sandbox» = что значит исполнение?** Решить: (A) paper/dry-run (никаких реальных ордеров, сделки симулируются на live-сигналах) — **рекомендуется** для real-money safety; или (B) live-ордера с минимальным `position_size_usdt`. От этого зависит, откуда KPI Gate берёт сделки. По умолчанию — (A).
- **Promotion: авто или с подтверждением?** Cron авто-**оценивает** гейт и помечает `promotion_ready`; **фактический перевод в Live — через step-up** (реальные деньги; консистентно с гейтингом критичных действий). Не авто-промоутить молча.

**Где / задачи:**
- [ ] **Модель + миграция (0031):** колонка `lifecycle_stage` (String, default `sandbox`; backfill существующих рабочих конфигов → `live`) на `auto_trade_configs` ([auto_trade_config.py](../app/models/auto_trade_config.py)); новая таблица `strategy_promotion_events` (`config_id`, `from_stage`, `to_stage`, `decision`, `kpi_snapshot_json`, `created_at`) — аудит-история по образцу транзишн-лога Position FSM.
- [ ] **FSM** `app/services/auto_trade/promotion/state_machine.py`: `LifecycleStage(Enum)` + `PromotionTrigger(Enum)` + `VALID_TRANSITIONS` dict (как [state_machine.py:59](../app/services/position/state_machine.py)). Триггеры: `submit_to_sandbox`, `request_promotion`, `gate_passed`, `gate_failed`, `promote_to_live`, `demote`, `archive`. Невалидный переход → исключение.
- [ ] **KPI Gate** `app/services/auto_trade/promotion/kpi_gate.py` — **чистое решение** (как `kpi_guard.py`, но инверсия): на вход `StrategyHealth` + `min_sandbox_days` (по `last_started_at`/первому входу в sandbox) + пороги → `PromotionDecision(can_promote: bool, passed: tuple, failed: tuple[GateCriterion])`. Критерии: `min_win_rate_pct`, `max_dd_pct`, `min_trades`, `min_sandbox_days`. Fail-safe: `insufficient_data` / выборка ниже `min_trades` → НЕ промоутить (никогда не повышать на шуме — зеркало правила KPI-Guard).
- [ ] **Пороги:** добавить в `auto_trade_risk_config` (или новая `promotion_gate_config`) поля `promote_min_win_rate_pct`, `promote_max_dd_pct`, `promote_min_trades`, `promote_min_sandbox_days`. Дефолты — консервативные, **калибровать с трейдерами** (как риск-пороги).
- [ ] **Сервис:** `AutoTradeService.promote_strategy(config_id)` — guard-checked переход через FSM + emit события; перевод в `live` разрешён **только** после `gate_passed`. `demote_strategy(config_id)` (live→sandbox при срабатывании риск-гварда/аномалии — связь с B6).
- [ ] **Гейтинг исполнения:** в pre-trade / обработчике сигналов запретить реальные ордера для конфигов не в стадии `live` (sandbox → paper-путь по решению A). Точка — [risk/engine.py](../app/services/auto_trade/risk/engine.py) или обработчик сигнал-очереди.
- [ ] **Cron** `evaluate_promotion_gates` (`*/30 * * * *`): по sandbox-конфигам считать `compute_strategy_health` → KPI Gate; при `can_promote` → `lifecycle_stage` не трогать, но выставить флаг/emit `promotion_ready`. Идемпотентность: не спамить `promotion_ready` повторно (only-on-transition + cooldown, как `data_stale`).
- [ ] **Step-up для перевода в Live:** эндпоинт `POST /auto-trade/{id}/promote` под `RequireStepUp` (как play/upsert в [live.py](../app/api/v1/endpoints/live.py)).
- [ ] **События:** `promotion_ready`, `strategy_promoted`, `strategy_demoted`, `promotion_gate_failed` → добавить в `RISK_EVENTS` ([formatting.py:30](../app/services/notifications/formatting.py)) + SSE event-types ([events/stream.py](../app/services/events/stream.py)) + фронт-стор `risk-events-store.ts` → Telegram автоматически.
- [ ] **D4 — Strategy Lifecycle UI** (фронт, ~2 дня): бэдж стадии на карточке стратегии (Live Monitor / dashboard), панель «Gate status» (какие критерии pass/fail + сколько дней в sandbox), кнопки Promote (step-up-модалка) / Demote. Зависело от B5 — теперь в объёме.
- [ ] **Тесты:** gate pass happy-path; min_sandbox_days не выполнен → no-promote; выборка < min_trades → no-promote; невалидный FSM-переход отклонён; `promotion_ready` не дублируется; promote без step-up отклонён.

**Acceptance-чек:** стратегия в `sandbox` с KPI выше порогов и выдержанным min-sandbox-периодом → cron помечает `promotion_ready` + событие; перевод в `live` проходит только со step-up; стратегия с Max DD выше порога в Live не переводится.

---

## 🔴 B6 — Strategy Anomaly Detection · W12 · ~3–4 дня

**Зачем (договор):** направление «Observability & Monitoring — strategy anomaly detection». Сейчас 0% (есть только Health Score — это агрегат, не детектор отклонений). Отдельный статистический пайплайн обнаружения аномального поведения стратегии.

**Подход (без тяжёлых ML-зависимостей):** `numpy`/`pandas`/`pandas-ta` уже в `pyproject.toml`; **scikit-learn НЕ тянуть**. Детект — rolling z-score и EWM-базлайн по пер-стратегийным временным рядам (context7-verified, Appendix C): отклонение > N σ от скользящего базлайна = аномалия. Источник рядов — **W9-ledger** (точный net-PnL по сделкам) + `auto_trade_events`, не пересчитывать.

**Сигналы (на старт — 3–4, расширяемо):**
- Резкий PnL-выброс: rolling z-score per-trade net-PnL > порога.
- Скорость просадки: всплеск running-DD относительно EWM-базлайна.
- Обвал win-rate vs скользящего базлайна.
- Аномалия частоты сделок: всплеск (runaway) или тишина (застрял) vs EWM-частоты.

**Где / задачи:**
- [ ] **Детектор** `app/services/auto_trade/anomaly/detector.py` — **чистые функции** (как `kpi_guard.py`): на вход ряд сделок конфига → `tuple[AnomalyFinding]` (`metric`, `value`, `baseline`, `z_score`, `severity` ∈ {info, warning, critical}). pandas `rolling().mean()/.std()` для z-score, `ewm().mean()` для базлайна (Appendix C).
- [ ] **Пороги/конфиг:** `anomaly_z_threshold` (дефолт ~3.0), `anomaly_window`, severity-бэнды. **Off by default** (`anomaly_detection_enabled=False`) — включать после калибровки на реальных рядах (как portfolio-DD watcher).
- [ ] **Cron** `sweep_strategy_anomalies` (`*/15 * * * *`): по running-конфигам считать findings; high/critical → emit `strategy_anomaly_detected`. **Дедуп:** only-on-transition + cooldown (не алертить одну аномалию каждый тик).
- [ ] **Опц. авто-реакция:** на `critical` — за флагом — `demote_strategy` (B5) или `_auto_pause_strategy`. По умолчанию только алерт (не паузить на статистике без калибровки).
- [ ] **Событие:** `strategy_anomaly_detected` → `RISK_EVENTS` ([formatting.py:30](../app/services/notifications/formatting.py)) + SSE + фронт-стор → Telegram.
- [ ] **Фронт:** бэдж/лента аномалий на Live Monitor (расширить `risk-events-store.ts` — список именованных событий уже есть).
- [ ] **Тесты:** синтетический PnL-spike → z-score breach → один finding; ровный ряд → ноль findings; дедуп (повторный тик не эмитит второе событие); off-by-default → пайплайн не эмитит ничего.

**Acceptance-чек:** при синтетическом аномальном ряде (PnL-выброс > 3σ) детектор отдаёт finding и одно событие `strategy_anomaly_detected` приходит в SSE/Telegram; на ровном ряде — тишина.

---

## Обновлённый календарь остатка (W12 + добор)

| Поток | Задачи | Оценка | Зависимости |
|---|---|---|---|
| 🔴 B5 Promotion Pipeline | FSM + KPI Gate + cron + step-up promote + события | ~4–5 дн | health.py, FSM-паттерн |
| 🔴 B6 Anomaly Detection | детектор (z-score/EWM) + cron + дедуп + событие | ~3–4 дн | W9-ledger |
| 🔴 D4 Lifecycle UI | бэдж стадии + gate-status + promote/demote | ~2 дн | B5 |
| 🔴 Anomaly UI | лента аномалий на Live Monitor | ~0.5–1 дн | B6 |
| QA | тесты + калибровка порогов promote/anomaly с трейдерами | — | B5, B6 |

> Оба бэк-потока независимы → можно вести параллельно. Калибровку порогов (promote-gate + anomaly-z) делать **с трейдерами**, не «на глаз» — реальные деньги.

---

# 🟡 DEFER в M5 — НЕ блокирует приёмку M4

> **D1 (Promotion Pipeline), D2 (anomaly detection), D4 (Lifecycle UI) — БОЛЬШЕ НЕ DEFER.** Возвращены в активный объём M4 → см. [PHASE 4 — B5/B6/D4](#phase-4--m4-extension-w10--anomaly-возвращены-в-объём-m4).

| # | Задача | Нед. | Статус / действие |
|---|---|---|---|
| **D3** | Portfolio Supervisor v2: **merged-equity** портфельный DD + авто-пауза всех при портф. DD | W11 | сейчас portfolio-DD watcher на **worst-strategy прокси** ([portfolio.py:87](../app/services/auto_trade/portfolio.py)) → настоящий merged-equity DD в M5 |
| **D5** | Per-strategy worker isolation | W7 | опциональный пункт плана — defer/skip по согласованию |
| **D6** | Email confirmation для критичных действий (P2-T5) | W11 | опционально — AC#5 «либо другие решения» закрыт TOTP+step-up+recovery+lockout+login-2FA |
| **D7** | Первоклассная таблица `strategy_promotion_events` (lifecycle-история) | W10 | сейчас аудит lifecycle-переходов идёт через события `strategy_promoted`/`strategy_demoted`/`promotion_ready`/`promotion_gate_failed` (со снапшотом критериев гейта) — достаточно для M4; отдельная таблица истории → defer (review S1) |

---

# 🧹 Техдолг (актуальный, на 2026-06-19)

**🔴 Калибровка риск-порогов (приоритет — реальные деньги):** ВСЕ in-trade governance-механизмы задеплоены, но **off-by-default и не откалиброваны**. Перед включением на счёте 17 согласовать с трейдерами:
- [ ] KPI-Guard auto-pause (`kpi_guard_*`), Volatility Kill-Switch (`kill_switch_*`), portfolio-DD watcher (`portfolio_dd_halt_enabled=False`).
- [ ] Anomaly detection (`anomaly_detection_enabled=False`) — z-порог/окно; включается per-config.
- [ ] Promotion KPI-Gate пороги (`promote_*`) — min-win-rate / max-dd / min-trades / min-sandbox-days.
> Все пороги off → ничего не срабатывает на бою. Это защита счёта — значения нельзя ставить «на глаз».

**Бэкенд:**
- [x] ~~conflicting-signal `net`/`replace` — interface-only~~ — **убрано** (T13): нет в схеме/DB-CHECK/движке/фронте.
- [x] ~~`app/services/position/reconciliation.py` — пустой стаб~~ — **устаревшая заметка**: файла нет; логика в `ws/manager.py` + `service.py` (подтверждено аудитом §8).
- [x] ~~`pipeline.py` слот `"watcher"` — no-op стаб~~ — потребитель event-bus подключён в рантайме (T7); комментарий обновлён.
- [ ] **Per-trade health-KPI** (`win_rate`/`max_dd`/`sharpe`) считаются по **позициям бота**, а realized теперь **account-scoped** (вкл. ручные сделки без позиций) → лёгкая нестыковка scope. Полное выравнивание = реконструировать «сделки» из сырых fills (FIFO) — отдельная задача.
- [ ] **Portfolio unrealized** — из tracked-позиций бота, а не из live-позиции биржи (карта берёт live) → расхождение ~2–3 USDT на открытой позиции (мелочь, тот же знак).
- [ ] **`promote_min_sandbox_days`-bug?**: префетч gate-status + sweep всё ок, но проверить что sandbox-дни считаются от `sandbox_entered_at`, а не сбрасываются на рестартах.

**Безопасность (хвосты code-review):**
- [ ] **2FA per-IP throttle** — сейчас только per-user lockout (review I4); добавить per-IP против распределённого brute-force.
- [ ] **CORS** ([config.py](../app/core/config.py)) дефолт `["*"]` — проверить, что на проде выставлены явные origins через env (review S3).
- [x] **SSE-токен** — только через cookie/BFF, не в query-string — ✅ инвариант соблюдён на фронте.

**Ops / прод:**
- [ ] `app_trade_worker` / `app_trade_scheduler` healthcheck показывает `unhealthy` — **косметика** (taskiq без HTTP-сервера, а HEALTHCHECK курлит `/api/v1/health`); дать им отдельный healthcheck.
- [ ] **Память сервера**: 7.6Gi RAM + 3Gi swap → `docker compose build` под нагрузкой роняет ssh/OOM (наблюдалось при деплое; рантайм-сервисы выживали). При следующих деплоях билдить detached; рассмотреть build-лимиты / +RAM.
- [ ] **Budget «balance 0.00»** на auto-trade — проверить fetch баланса саб-аккаунта (вся маржа в открытой позиции, но total читается 0).
- [x] **Фронт-контракт** — реген после правок роутов ✅ (FE-0, Node 20, Appendix B); добавлен vitest-набор.
- [x] **Frontend test-suite** — ✅ заведён (vitest+jsdom+testing-library, 19 тестов; раньше был defer).

---

# Appendix A — Сниппеты (context7-verified)

### 2FA / pyotp
```python
import pyotp
# enroll
secret = pyotp.random_base32()                 # хранить ЗАШИФРОВАННЫМ через SecretCipher
uri = pyotp.TOTP(secret).provisioning_uri(      # отдать клиенту → QR
    name=user_email, issuer_name="Amber")
# verify (±30s, constant-time)
ok = pyotp.TOTP(secret).verify(code, valid_window=1)
```

### SSE / sse-starlette
```python
from sse_starlette.sse import EventSourceResponse
import asyncio

async def risk_stream(request):
    try:
        async for evt in subscribe_redis(user_id):     # pub/sub риск-событий
            if await request.is_disconnected():
                break
            yield {"event": evt["type"], "data": evt["payload"]}
    except asyncio.CancelledError:
        raise                                            # cleanup на disconnect/shutdown

# return EventSourceResponse(risk_stream(request),
#                            send_timeout=30, headers={"Cache-Control": "no-cache"})
```

# Appendix B — Регенерация контракта фронта (после правок роутов)
```bash
# 1) свежая спека из приложения (БД не нужна)
cd constructor && uv run python -c "import json; from app.main import app; \
  json.dump(app.openapi(), open('openapi.json','w'), indent=2, ensure_ascii=False)"
# 2) синхронизировать во фронт
cp constructor/openapi.json constructor-front/openapi.json
# 3) типы (ВНИМАНИЕ: default node v14 падает — нужен Node >=18)
cd constructor-front && export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && npm run gen:api-types
```

---

# Сводка по приоритету и срокам

| Приоритет | Задачи | Оценка |
|---|---|---|
| ✅ Backend (done) | ~~B1 2FA · B2 portfolio-DD · B3 SSE · B4 live-KPI~~ | закрыто, в проде |
| ✅ Frontend (done) | ~~F1 Risk Config · F2 Live Monitor · F3 Catalogue · F4 2FA UI · F5 SSE~~ | закрыто, в проде |
| 🔴 Backend (M4-extension) | **B5 Promotion Pipeline · B6 Anomaly Detection** | ~7–9 дней |
| 🔴 Frontend (M4-extension) | **D4 Lifecycle UI · Anomaly UI** | ~2.5–3 дня |
| 🟡 Defer (M5) | D3 merged-equity DD · D5 worker isolation · D6 email-2FA | согласовать при необходимости |

> **Критический путь — фронт + 2FA.** Бэк-остаток умеренный; фронт-остаток в одиночку за 2 недели нереалистичен → **нужен выделенный фронтендер на W11–W12**, иначе приёмка по AC#1/#4/#7 срывается из-за UI, а не из-за бэка.
>
> **Update 2026-06-18:** acceptance-поверхность (AC#1–#7) закрыта и в проде. Новый критический путь — **B5 Promotion Pipeline + B6 Anomaly Detection** (состав работ договора). Оба бэк-потока независимы и ведутся параллельно; UI (D4 + anomaly-лента) — следом за бэком.

---

# Appendix C — Сниппеты для B5/B6 (context7-verified)

### taskiq cron для новых watcher'ов (B5 `evaluate_promotion_gates`, B6 `sweep_strategy_anomalies`)
Лейбл-расписание парсится `LabelScheduleSource`; формат идентичен существующим кранам в [worker/tasks.py](../app/worker/tasks.py).
```python
@broker.task(
    task_name="app.worker.tasks.evaluate_promotion_gates",
    schedule=[{"cron": "*/30 * * * *", "schedule_id": "promotion_gates_every_30m"}],
)
async def evaluate_promotion_gates() -> dict[str, int]:
    ...

@broker.task(
    task_name="app.worker.tasks.sweep_strategy_anomalies",
    schedule=[{"cron": "*/15 * * * *", "schedule_id": "anomaly_sweep_every_15m"}],
)
async def sweep_strategy_anomalies() -> dict[str, int]:
    ...
# scheduler уже сконфигурирован LabelScheduleSource(broker) — новые task-модули
# обязаны быть импортированы CLI-интерфейсом, иначе лейблы не резолвятся.
```

### pandas rolling z-score + EWM-базлайн (B6 detector — чистая функция)
```python
import pandas as pd

def per_trade_pnl_zscore(net_pnls: list[float], window: int = 20) -> pd.Series:
    """z = (x - rolling_mean) / rolling_std. |z| > anomaly_z_threshold ⇒ аномалия."""
    s = pd.Series(net_pnls, dtype="float64")
    roll = s.rolling(window, min_periods=window)
    return (s - roll.mean()) / roll.std()

def trade_frequency_baseline(counts: list[float], alpha: float = 0.2) -> pd.Series:
    """EWM-базлайн частоты сделок; всплеск/тишина — отклонение от ewm-mean."""
    return pd.Series(counts, dtype="float64").ewm(alpha=alpha).mean()
# rolling().std() даёт NaN пока окно не заполнено (min_periods=window) → fail-safe:
# NaN-z трактуем как «недостаточно данных», не аномалия (зеркало insufficient_data).
```

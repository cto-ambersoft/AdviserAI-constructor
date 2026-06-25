# Milestone 4 — Независимый технический аудит (план vs. реализация)

> **Дата:** 2026-06-19 · **Аудит:** жёсткое сопоставление договорного «Plan of Works (M4)» с фактическим кодом.
> **Репозитории:** `constructor` (FastAPI «Trade Service», ветка `feature/init`, HEAD `3aa4c3a`) · `core` (NestJS + Mastra «Core Service») · `constructor-front` (Next.js).
> **Метод:** чтение исходников с фиксацией `файл:строка`; проверка корректности библиотек по официальной документации через context7 (pyotp, cryptography/Fernet, sse-starlette). Утверждения из внутренних доков (`tasks/m4-closeout-plan.md`) **не принимались на веру** — каждое проверялось по дереву.
> **Дисклеймер:** аудит read-only; код не менялся. Все вердикты обоснованы цитатами.

---

## 0. Как читать этот документ

Вердикты по каждому пункту:

| Метка | Значение |
|---|---|
| ✅ **IMPLEMENTED** | реализовано и подключено в боевой путь |
| 🟡 **PARTIAL** | есть, но с существенной оговоркой против формулировки договора |
| ⚠️ **STUB / DEAD** | код есть, но в проде не вызывается / no-op |
| 🔴 **MISSING** | обещано, отсутствует |
| 🔌 **OFF-BY-DEFAULT** | реализовано корректно, но в дефолтной конфигурации не срабатывает |

---

## 1. Главный вывод

Кодовая база M4 объёмная и в значительной части **действительно реализована и работает**. Однако заявление close-out плана — *«весь объём договора M4 закрыт и в проде, формального риска приёмки нет»* — **проверку не проходит**.

Расхождения с планом и обещаниями делятся на пять классов (от тяжёлого к лёгкому):

1. **Построено, но мертво в проде** — headline-фича «In-Position Indicator Monitoring» считает индикаторы и публикует триггеры в Redis, где **нет ни одного подписчика** (только в тестах).
2. **Семантика подменена** — 6 пунктов выдают за обещанное не то: Agent Accuracy на синтетической цене вместо реальных сделок; «применение весов» — orphan-no-op; `ai_trend` не агрегирует вклад агентов; Sandbox не paper, а реальные ордера; «Risk-on/off режим» = просто пауза; portfolio-DD = worst-strategy прокси; «live» дашборд = polling.
3. **Весь governance-слой выключен по умолчанию** — KPI-Guard, Kill-Switch, portfolio-DD, anomaly, AI-overlay, conflicting-signal: все флаги `False`/NULL. На реальном счёте «из коробки» **ни одна защита не активна**.
4. **Дропнуто молча** — email-confirmation (W11c) отсутствует полностью; per-strategy worker isolation (W7c) — один общий брокер; first-class lifecycle-audit таблица — нет.
5. **Security-дыры на реальном торговом счёте** — смена/удаление ключа биржи без step-up; step-up дремлет без 2FA; слабый дефолтный ключ шифрования без startup-guard; неаутентифицированный `/encrypt`; нет rate-limit на логин; CORS `*`.

**Объективно «зелёных» Acceptance Criteria — 2–3 из 7.** Остальные присутствуют в коде, но с оговорками, противоречащими формулировкам договора.

---

## 2. Система: что это и как связаны три сервиса

```
┌────────────────────────────────────────────────────────────────────┐
│ constructor-front  (Next.js / React)                                 │
│   UI: Backtest · Forecast Catalogue · Live Monitor · Strategy        │
│       Lifecycle · Risk Config · 2FA/Security                         │
│   BFF: app/api/* проксирует в backend, Bearer берётся из httpOnly    │
└───────────────┬──────────────────────────────────────────────────────┘
                │ HTTPS (+ SSE /events/stream)
                ▼
┌────────────────────────────────────────────────────────────────────┐
│ constructor  (FastAPI + Taskiq + PostgreSQL + Redis) «Trade Service» │
│   Backtest Engine · Auto-Trade · Position FSM · Dynamic SL/TP ·       │
│   Risk Governance · Promotion Pipeline · Portfolio Supervisor ·      │
│   SSE event-bus · Telegram-алертинг                                  │
└───────────────┬──────────────────────────────────────────────────────┘
                │ analysis_proxy (HTTP, internal_api_key)
                ▼
┌────────────────────────────────────────────────────────────────────┐
│ core  (NestJS + Mastra)  «Core Service»                              │
│   Мульти-агентный AI-pipeline · AI Forecast Catalogue (Mongo) ·      │
│   Reasoning Path / ai_trend · Agent Accuracy · Weight Suggestions ·  │
│   Exchange Layer (ccxt) — BTC/USDT · ETH/USDT                        │
└────────────────────────────────────────────────────────────────────┘
```

**Поток AI → торговля:** `core` генерирует `ai_trend` → пишется в `personal_analysis_history` → `constructor` (модуль `ai_overlay`) читает снапшот и **опционально** корректирует сторону входа / ATR-множитель SL / RSI-пороги. Этот мост — ключевой для «Adaptive Strategy Engine» (см. §4, W2/W4) и **по умолчанию выключен**.

---

## 3. Acceptance Criteria (7 договорных) — фактический статус

| # | Критерий (договор) | Вердикт | Резюме |
|---|---|---|---|
| 1 | Research Module: каталог + Δ vs Baseline, выбор и запуск с пользовательской стратегией | 🟡 PARTIAL | Каталог + Δ есть, trader-facing; но привязки forecast **к live-стратегии нет** — только в backtest-билдер |
| 2 | Autonomous Execution: SL trailing/breakeven/volatility | ✅ IMPLEMENTED | Реальная приоритетная цепочка SL/TP + multi-TP + reconnect-sync |
| 3 | Multi-Strategy ≥3 без коллизий | ✅ IMPLEMENTED | Изоляция через саб-аккаунты + unique-index на открытую позицию |
| 4 | Risk Enforcement: авто-пауза по KPI Guard | 🔌 OFF-BY-DEFAULT | Код корректен, но `kpi_guard_enabled=False`; portfolio-DD — прокси и тоже off |
| 5 | Security: API Vault + 2FA для критических операций | 🔴 ДЫРЫ | Vault шифрует, 2FA есть; но смена/удаление ключа без step-up, step-up дремлет без 2FA, слабый дефолтный ключ, email-confirm дропнут |
| 6 | Asset Expansion: BTC + ETH | ✅ IMPLEMENTED | Символ generic, ETH проходит (без curated-universe) |
| 7 | KPI Transparency: live dashboard (Sharpe/WR/ROI) | 🟡 PARTIAL | Дашборд есть, KPI считаются; но «live» = polling раз в 30 с, не SSE |

> Договор по AC#5 содержит лазейку «*либо другие решения, которые будут закрывать security часть*» — формально 2FA+step-up её закрывает, но перечисленные дыры это подрывают (см. §5).

---

## 4. Поштучный разбор по неделям (W1–W12)

### S1 — Deep Research & AI-Backtesting + AI Decision Analytics

#### W1(a) — AI Forecast catalog + метрики (Win Rate, Max DD, **Delta vs Baseline**), фильтр symbol/timeframe — ✅ IMPLEMENTED
- Mongo-коллекция `ai_forecast_catalogue` со схемой и compound-индексом: `core/src/analysis/schemas/ai-forecast-catalogue-entry.schema.ts:7-52`.
- Win Rate: `constructor/app/services/backtesting/common.py:128`; Max DD: `common.py:132-135`.
- **Delta vs Baseline реальна** (два пути): server-side `ai - baseline` в `constructor/app/api/v1/endpoints/internal_backtest.py:138-149`; клиентский fallback `computeDeltaMetrics()` `core/src/analysis/backtest-experiment.service.ts:463-493`; персист `catalogue-metrics.ts:76-80`.
- Фильтр symbol/timeframe: `ai-forecast-catalogue.controller.ts:15-16` → `ai-forecast-catalogue.service.ts:40-41`.
- **Оговорка:** file-only rebuild fallback пишет строки с `symbol:"UNKNOWN"`, `timeframe:"UNKNOWN"`, `metrics:null` (`ai-forecast-catalogue.service.ts:117-128`) — нефильтруемые/без метрик.

#### W1(b) — Внутренний инструментарий запуска AI Backtests (bash-скрипты, реестр) — 🟡 PARTIAL
- Движок бэктестов реален: `POST /api/v1/internal/backtest/compare` (api-key) `internal_backtest.py:95-149`; оркестратор `backtest-experiment.service.ts:109-256`.
- **bash-скрипта-раннера бэктестов нет** (поиск `*.sh` по backtest/forecast/catalog пуст). Единственный смежный — `core/scripts/personal_analysis_backfill.sh` (backfill, не раннер).
- **Сущности «registry» нет** — роль выполняют Mongo-коллекции `ai_forecast_catalogue`/`backtest_experiment`.
- **Мёртвый артефакт:** `constructor/app/services/ai_forecast_backtest/` содержит только устаревший `service.cpython-313.pyc` без `.py`-исходника; живой код — `app/services/backtesting/`.

#### W1(c) — Еженедельное авто-обновление AI Forecast — ✅ IMPLEMENTED
- Реальный недельный крон: `@Cron("0 2 * * 1", name:"ai-forecast-catalogue-weekly-rebuild")` `core/src/analysis/analysis-cron.service.ts:68-87` под Mongo-локом. Планировщик активен (`ScheduleModule.forRoot()` `app.module.ts:19`). Env-флага отключения нет — работает по умолчанию.

#### W2(a) — Reasoning Path / вклад агентов / ai_trend — 🟡 PARTIAL (нагруженная оговорка)
- Reasoning path из реальных выходов агентов: `analysis.service.ts:554-566`; пер-агентные записи `decision-analytics.ts:253-285`.
- **🔴 Главное:** `ai_trend` **НЕ является агрегацией вклада агентов**. Он выводится только из финального `analysis.confidence` + `analysis.bias` LLM (`decision-analytics.ts:119-141, 151-170`). Пер-агентные сигналы/веса собираются и **показываются**, но математически в `ai_trend` не входят. Это рвёт обещанную петлю «вклад агента → сигнал».
- Логика извлечения reasoning (`buildReasoningPath`/`parseReasoningText`/`inferSignal`) **не покрыта тестами** — `decision-analytics.spec.ts` тестирует только confidence→distribution.

#### W2(b) — Хранение истории AI-решений — ✅ IMPLEMENTED
- Модель `ai_decision_events`: `schemas/ai-decision-event.schema.ts:8-9`, `analysis.module.ts:75-80`; персист `.create({... aiTrend, perAgent ...})` `ai-decision-event.service.ts:46-62`; вызывается в обоих путях прогона (`analysis-runner.service.ts:109`, `personal-analysis.service.ts:214`).

#### W2(c) — Интеграция ai_trend с live: блокировка противоположных входов — ✅ IMPLEMENTED 🔌 OFF-BY-DEFAULT
- Реально жёстко гейтит вход: `should_block_entry` → `return` в `constructor/app/services/auto_trade/service.py:4238`, **до** риск-движка/сайзинга. Логика блока `ai_overlay/scaler.py:43-66`.
- Требует `enabled AND entry_side_lock_enabled` (`service.py:4206-4209`).
- **Default = DISABLED:** все флаги `default=False` (`app/schemas/ai_overlay.py:31-43`); NULL-конфиг → всё off. Fail-open на устаревшем/отсутствующем сигнале (`stale_max_minutes` default 240).

### S2 — Agent Accuracy + Adaptive Parameters

#### W3(a) — Agent Accuracy: AI-decision vs реальный исход сделки, 7d/30d — 🟡 PARTIAL (семантика подменена)
- 7d/30d окна реальны и персистятся: `agent-accuracy.service.ts:49,136-141,186-211`; крон каждые 6ч `@Cron("0 */6 * * *")` `analysis-cron.service.ts:89-108`.
- **🔴 «Реальный исход сделки» — синтетический.** Сопоставление идёт с **дневным движением цены из Snowflake** `price_history_daily` (`fetchMovePct`, `agent-accuracy.service.ts:218-252`, горизонт 72ч). **Реальные сделки/fills/PnL никогда не читаются.** Поля `outcomeJoinKey`/`resultSnapshot` мёртвые (пишутся, не читаются). Единственный спек стабит источник исхода (`jest.spyOn(...,"fetchMovePct").mockResolvedValue(2)`), так что реальный путь не тестируется.

#### W3(b) — Weight Suggestions с опциональным применением — 🟡 PARTIAL (применение — no-op)
- Расчёт реальный, применение через manual-gate (не авто): `suggestWeights()` `agent-accuracy.service.ts:57-104`; apply через controller POST, `dry_run` default false (`agent-accuracy.controller.ts:37-43`). Формула — хардкод-эвристика (0.7/0.3, ±5, floor 0.2; `:281-285`).
- **🔴 «Применение» не влияет на рантайм.** `applySuggestion` лишь `.create()` нового профиля `AW-SUGG-...` (`agent-accuracy.service.ts:120-124`, `ai-config.service.ts:167-171`). **Подтверждено grep'ом:** в `ai-config.service.ts` нет ни одного `updateOne/findOneAndUpdate/findByIdAndUpdate/.save()` — `AiConfig.agentWeightsId` **никогда не перепривязывается**. Применение создаёт orphan-профиль, на который ничего не ссылается; рабочие веса не меняются.

#### W4(a) — Dynamic Parameter Adjustment (ATR-mult, RSI thresholds, сторона входа) — ✅ IMPLEMENTED 🔌 OFF-BY-DEFAULT
- Все три фазы подключены в live-open path `service.py`: блок входа `4205-4238`; ATR-override `4345-4377` → применяется `service.py:394-395`; RSI-shift `4379-4412` → `service.py:400-408` (`shift_watcher_condition_threshold`). Ограничено `atr_scale_range`/`rsi_max_shift`; всё за off-by-default флагами.

#### W4(b) — Полная цепочка AI Forecast → personal analysis → ai_trend → live-параметры — 🟡 PARTIAL
Трассировка и слабые звенья:
1. Прогон (core) → `ai_trend` из финальной confidence+bias (**игнорирует пер-агентные веса** — W2a).
2. `ai_trend` → `personal_analysis_history` → читается `ai_overlay/resolver.py:145-197` (fail-open на stale >240мин/missing).
3. constructor резолвит снапшот раз (`service.py:4164-4181`) → блок/ATR/RSI.
4. **Весь адаптивный слой OFF by default** (`ai_overlay.py:31-43`) — чистая инсталляция не меняет поведение.
5. **Разрыв:** W3(b)-apply не перепривязывает конфиг + ai_trend не потребляет веса агентов → суб-цепочка «accuracy→веса→адаптация» разорвана в двух местах.

### S3 — Advanced Execution Core

#### W5(a) — Position State Machine + история SL/TP-сдвигов — ✅ IMPLEMENTED
- FSM с enforcement: `app/services/position/state_machine.py:111-164` — 10 состояний, 22 триггера, `VALID_TRANSITIONS` (`:59-104`); `transition()` бросает `InvalidTransitionError` на нелегальный триггер (`:137-141`). Инстанцируется per-position (`context.py:324`), драйвится из `auto_trade/service.py`, `ws/manager.py`, `multi_tp.py`.
- История переходов персистится: `_transition_log` (`:122`) → `transition_log_json` (`context.py:671`) → колонка `models/auto_trade_position.py:147` + миграция `0012`.
- История SL/TP: `SLHistoryEntry`/`TPHistoryEntry` (`context.py:160-182`) → `sl_history_json`/`tp_history_json` → колонки model `115-126`; TP-история активно дополняется на каждый fill (`multi_tp.py:139-149`).

#### W5(b) — In-Position Indicator Monitoring (RSI / MA-cross / MACD) — ⚠️ STUB / DEAD В ПРОДЕ
- **Считает 3 индикатора реально:** `indicator_watcher.py` на `pandas_ta` — RSI (`:125`), MACD (`:130`, `_normalize_macd:81`), EMA-crossover (`:152`, `_normalize_ema_cross:107`). Семантика cross_above/below реальна (`rule_engine.py:43-58`).
- **Запускается по крону:** `run_position_watcher_tick` (`watchers/service.py:301`) → Taskiq-task `position_watcher_tick` (`worker/tasks.py:270-272`); cron создаётся на открытии позиции (`service.py:988`).
- **🔴 КРИТИЧНО: на триггеры никто не реагирует.** Tick публикует события в Redis-канал `position.indicator_trigger` (`event_bus.py:48-52`). Потребитель `subscribe_watcher_events`/`handle_watcher_event` (где живут `tighten_sl`/`close_partial`/`alert`, `event_bus.py:55-206`) **вызывается ТОЛЬКО в `tests/unit/test_watcher_event_bus.py`** (подтверждено grep'ом: ноль продакшн-callers). `install_auto_trade_runtime` (`service.py:6181`) подписчика не стартует. → **В проде индикаторы внутри позиции считаются и летят в Redis, где их никто не слушает.** Договорное «отслеживающий … внутри открытой позиции» (с подразумеваемой реакцией) не функционирует.
- **Фикс:** запустить `subscribe_watcher_events(handle_watcher_event)` как фоновую задачу в `install_auto_trade_runtime`.

#### W6(a) — Dynamic SL/TP как конфигурируемая приоритетная цепочка — ✅ IMPLEMENTED
- Все три источника реальны: Trailing (`sl_tp/trailing.py:22`), Breakeven (`sl_tp/breakeven.py:11`), Volatility (`sl_tp/volatility.py:11`); каждый отказывает в не-защитном сдвиге.
- **Настоящая приоритетная цепочка:** `sl_tp/pipeline.py:19-43` перебирает `position.adjustment_priority` (default `["watcher","trailing","breakeven","volatility"]`), собирает всех кандидатов и выбирает **самого защитного** (max SL для long / min для short) — реальная конкуренция, не «один из».
- Драйвится: `RealtimeSLAdjuster.on_tick` (`live_tracker.py:174`) на kline-стриме → `ws/manager.py:1002-1050`. Слот `"watcher"` — намеренный документированный no-op (`pipeline.py:51-56`) — **но** путь, которому он делегирует (event-bus), мёртв в проде (см. W5b).

#### W6(b) — Multi-TP частичные закрытия — ✅ IMPLEMENTED
- `multi_tp.py`: `initialize_tp_levels` (`:45-67`) ставит `place_tp` на уровень с `qty = original × close_pct/100` — реальные scaled-exits. `handle_tp_triggered` (`:69-179`) вычитает qty, пишет TP-историю, двигает SL (numeric `sl_lock_pct`/`breakeven`/`tpN`), драйвит FSM. Инстанцируется на открытии (`service.py:945-955`), вызывается на WS TP-fills (`ws/manager.py:488..835`) + REST-fallback (`service.py:5519-5529`).

#### W6(c) — Fault-tolerance: синхронизация состояния при reconnect — ✅ IMPLEMENTED
- Реальная reconnect-синхронизация: `ws/manager.py:_handle_disconnect (536-593)` → `RECONNECTING` + персист → backoff-reconnect → `_full_state_sync()` → `_sync_position(reconnect_sync=True)` (`:1181-1247`): сверяет позицию с биржей, помечает закрытые CLOSED, **детектит потерянный SL и ставит emergency-replacement** (`:1228-1237`), затем `SYNC_COMPLETE`. На max-attempts → `_emergency_close_all`. Плюс периодический REST-reconciler (`service.py:5163`).
- **Стейл-заметка в close-out:** `m4-closeout-plan.md:252` называет `app/services/position/reconciliation.py` «empty stub» — **файла не существует вовсе**; логика в `ws/manager.py` + `service.py`. Заметка устарела.

### S4 — Multi-Worker Network + Pre-Trade Risk

#### W7(a) — Multi-Strategy Account Partitioning — ✅ IMPLEMENTED
- Изоляция = одна стратегия ↔ один exchange-саб-аккаунт: `UniqueConstraint("user_id","account_id")` (`auto_trade_config.py:22`), `account_id` FK на `exchange_credentials`. «Бюджет» = per-strategy `position_size_usdt` (default 100). Анти-коллизия: partial unique index `uq_auto_trade_positions_user_account_open` на `(user_id, account_id) WHERE status='open'` (`auto_trade_position.py:36-43`) + pre-trade conflicting-rule. Оговорка: документированный ±1 overshoot race (`engine.py:106-111`).

#### W7(b) — ETH/USDT (backtest + live) — ✅ IMPLEMENTED (без curated universe)
- Whitelist'а BTC-only нет. Символ течёт из профиля/сигнала; ETH-ветка нормализации (`ws/manager.py:291`). Backtest default `"BTC/USDT"` (`portfolio.py:91`) перекрывается `**config` (`:94`). **Оговорка:** generic-passthrough, нет enumerated allow-list и ETH-специфичной калибровки tick/step/min-notional сверх generic exchange-filter.

#### W7(c) — Worker-сеть: изолированные Taskiq-воркеры на стратегию — 🔴 MISSING (общий брокер)
- Один `RedisStreamBroker` (`broker.py:22`); все задачи на нём. Нет per-strategy воркеров/очередей/lifecycle. Очередь сигналов обрабатывается общим пулом, сериализация per-row через `SELECT ... FOR UPDATE SKIP LOCKED`. *(В договоре пункт помечен опциональным — легитимный defer, но «сетка изолированных воркеров» не существует.)*

#### W8(a) — Strategy Health Score — ✅ IMPLEMENTED
- `health.py:351 health_from_trades` — 0-100 из win-rate (0.30), DD (0.30), PnL (0.20), stability (0.20) (`:51-54`). `<10` сделок → `insufficient_data` (`:48,371`). Эндпоинт `GET /auto-trade/strategies/{id}/health` (`live.py:616-640`), персист снапшотов.

#### W8(b) — Data Freshness cron (каждые 4ч) — 🟡 PARTIAL (alert-only, не per-agent)
- Крон каждые 4ч: `tasks.py:150-152` cron `"0 */4 * * *"` → `sweep_agent_freshness` (`freshness.py:82`), порог `agent_freshness_threshold_minutes` default 240 (`config.py:68`). Эмитит `data_stale` `AutoTradeEvent` (`:140-165`).
- **Оговорки:** (1) **не действует** — staleness не паузит и не блокирует вход. (2) **не truly per-agent** — все агенты делят profile-level recency (документировано `freshness.py:9-12`); договорное «по каждому AI-агенту» аппроксимировано.

#### W8(c) — Pre-Trade Risk Engine — 🟡 PARTIAL (3 из 4 энфорсятся; conflicting частично interface-only)
Подключён до сайзинга/ордера: `check_pre_trade` (`service.py:4265`), блок → `risk_blocked` + ничего не открывается. Master `enabled` default **True** (`auto_trade_risk_config.py:153`), но лимиты nullable → NULL = правило off.
- **Leverage ceiling** — энфорсится `engine.py:90-96`. ✅
- **Exposure limit** — энфорсится (сумма posted-margin vs cap) `engine.py:198-225`. ✅
- **Daily loss limit** — энфорсится USDT (`:236-241`) и pct (`:242-267`; fail-open на отсутствии баланса). ✅
- **Conflicting-signal suppression** — 🔴 **только `block_opposite` блокирует** (`engine.py:158-184`). `net` и `replace` **interface-only**: есть в API-схеме (`auto_trade.py:56` `Literal["off","block_opposite","net","replace"]`) и в DB CHECK, но движок логирует warning и **пропускает** (`engine.py:185-191`). Default policy `"off"`.

#### W9(a) — Volatility Kill-Switch + Risk-on/off — ✅ IMPLEMENTED 🔌 OFF-BY-DEFAULT (режим — редуктивный)
- Авто-закрытие реально срабатывает end-to-end: детектор `kill_switch.py:34` → `live_tracker.py:284-292` → `ws/manager.py:1007` → `service.py:2594 _runtime_kill_switch_close` → `:2490 kill_switch_close_position` (реальный reduce-only market close) + `kill_switch_triggered`. Не no-op.
- **Default OFF:** `kill_switch_enabled=False` (`auto_trade_risk_config.py:177`); `_apply_kill_switch_config` early-return при off (`service.py:2586-2587`); `_check_kill_switch` скипает off-позиции (`live_tracker.py:271`).
- **🟡 «Risk-on/Risk-off режим» редуктивен:** есть risk-off защёлка (на trip → `_auto_pause_strategy(risk_off_entered)`, `service.py:2549-2561` → `is_running=False`), новые входы блокируются gate'ом enqueue (`service.py:3621`). **«Risk-on» = ручной `set_running(True)`** — авто-детектора режима и recovery нет. «Режим» = «защёлкнутая пауза до человека».

#### W9(b) — Post-Trade Audit (trace + KPI pipeline) — ✅ IMPLEMENTED
- Execution-trace signal→close: `build_position_trace` (`service.py:3583`) + все `AutoTradeEvent` хронологически; эндпоинт `GET /auto-trade/positions/{id}/trace` (`live.py:644-659`). KPI: Win Rate / Max DD / Sharpe-proxy / ROI в `health.py` (`health_from_trades`, `calculate_sharpe_proxy`, `calculate_equity_max_drawdown_pct`, `_roi_pct`).

#### W9(c) — Авто-пауза при нарушении KPI Guard — ✅ IMPLEMENTED 🔌 OFF-BY-DEFAULT
- Крон каждые 5 мин: `tasks.py:173-176` cron `"*/5 * * * *"` → `sweep_kpi_guards` (`service.py:2077`) → `apply_kpi_guard` (`:2009`) → чистое `evaluate_kpi_guard` (`kpi_guard.py:59`) → `_auto_pause_strategy` (`is_running=False` + `kpi_guard_triggered` + `strategy_auto_paused`, `service.py:1948-1978`). Пауза реально гейтит входы.
- **Default OFF:** `kpi_guard_enabled=False` (`auto_trade_risk_config.py:168`); статистические правила требуют `HEALTH_MIN_TRADES=10`; daily-loss правила могут халтить и свежую стратегию.

### S6 — Strategy Promotion + Portfolio Supervisor

#### W10 — Strategy Promotion Pipeline
**(a) FSM lifecycle + guard conditions — ✅ IMPLEMENTED.** `promotion/state_machine.py:30-103` — `LifecycleStage` (research/sandbox/validation/live/rejected/archived), `VALID_TRANSITIONS`, `apply_transition()` бросает `InvalidPromotionError` (`:99-103`). Promote драйвит `sandbox→validation→live` (`service.py:1706-1708`).

**(b) KPI Gate (min-win-rate / max-DD / min-trades / min-sandbox-days) — ✅ IMPLEMENTED.** `promotion/kpi_gate.py:70-140` — все 4 критерия; fail-safe (недостаточная выборка → win-rate и max-DD = failed, `:101-134`); `can_promote = all(...)` (`:138`). **Оговорка:** пороги nullable → fallback на встроенные дефолты (50% WR / 25% DD / 20 trades / 7 дней, `:34-37`) — некалиброванные.

**(c) Cron авто-оценки — ✅ IMPLEMENTED (notify-only).** `sweep_promotion_gates` каждые 30 мин (`tasks.py:191-199`); эмитит `promotion_ready` (6ч cooldown), сам не промоутит. **Оговорка:** скипает idle (`is_running=False`) sandbox-конфиги (`service.py:2292`) — доказанная, но паузнутая стратегия не получит readiness.

**(d) Promote-to-Live гейтится step-up — ✅ IMPLEMENTED.** `POST /auto-trade/strategies/{id}/promote` под `RequireStepUp` (`live.py:380-384`); re-check стадии + re-run gate → 422 на провале (`service.py:1680-1703`); `SELECT ... FOR UPDATE`. *(Эффективность step-up условна — см. §5.)*

**(e) Sandbox execution semantics — 🟡 PARTIAL (важная архитектурная оговорка).**
- **Sandbox НЕ paper/dry-run.** Docstring `state_machine.py:14-18` обещает «paper / dry-run … no real exchange orders», но фактический энфорсмент иной: не-live конфиг может стартовать **только на demo/testnet-аккаунте** (`service.py:1596-1606`, run-gate `:1791-1803`). То есть sandbox ставит **реальные ордера на demo-венью**, а не симулирует fills.
- **В пути размещения ордера нет проверки стадии** (`_place_entry_order:593`, `place_futures_market_order:630/1021` стадию не смотрят) — единственный барьер real-money стоит в `set_running`.
- **🔴 Fail-OPEN:** `lifecycle_stage` дефолт **`"live"`** на DB+ORM (`models/auto_trade_config.py:87-88` `server_default="live", default="live"`). «Новые конфиги стартуют в sandbox» держится **только** на явной передаче SANDBOX в `upsert_config` (`service.py:1184`); любой иной insert-путь падает **в live**, что противоречит заявленному fail-safe.

**(f) Аудит-история lifecycle — ⚠️ STUB (только события).** Выделенной таблицы нет (признано `service.py:2279-2283`): переходы только как `AutoTradeEvent` (`promotion_ready`/`strategy_promoted`/`strategy_demoted`/`promotion_gate_failed`). FSM `_transition_log` (`state_machine.py:116-143`) — в памяти, не персистится.

#### W11(a) — Portfolio Supervisor v2 — 🟡 PARTIAL + 🔌 OFF-BY-DEFAULT (прокси, не merged-equity)
- Cross-strategy агрегация реальна: `portfolio.py:176-380 compute_portfolio` (realized/unrealized/open/running по всем конфигам, балансы саб-аккаунтов параллельно).
- Крон `evaluate_portfolio_dd_guards` каждые 5 мин (`tasks.py:225-233`) → на breach `set_running_bulk(False)` (халт всех) + `portfolio_dd_halt`.
- **🔴 Это worst-strategy прокси, не merged-equity DD:** `service.py:2350-2351` (комментарий) + breach-loop берёт `max(health.max_dd_pct)` по конфигам (`:2405-2410`); `PortfolioSummary.portfolio_max_dd_pct` несёт тот же прокси (`portfolio.py:89-91`). → Равномерно «кровоточащий» портфель (ни одна стратегия не пробивает порог) **халт не триггерит**.
- **Default OFF:** `portfolio_dd_halt_enabled=False` (`config.py:81`), порог 20% (`:82`). Халт и алерт — в разных транзакциях (`service.py:2418-2426`): алерт может потеряться.

#### W11(b) — API Vault — ✅ IMPLEMENTED (шифрование) / 🔴 управление ключом слабое
- Реальный Fernet (AES128-CBC + HMAC): `app/core/security.py:1-19`. Шифрование перед персистом (`ExchangeCredentialsService.create_account/update_account`); API-key дополнительно SHA256-hash для uniqueness.
- **Слабости (см. §5 + context7-обоснование §6):** дефолтный плейсхолдер-ключ без startup-guard; `_normalize_key` SHA256-деривит **любую** строку в валидный Fernet-ключ (`security.py:21-32`) → слабый ключ не падает; нет ротации (нет MultiFernet); неаутентифицированный `/encrypt` (`exchange.py:23`).

#### W11(c) — 2FA (TOTP + email confirmation) — 🟡 PARTIAL + 🔴 дыры
- **(a) TOTP enroll/verify — ✅** `app/services/totp.py`: pyotp, секрет Fernet-шифрован (`:79,116`), pending-until-confirmed (`:118-119`), `valid_window=1` (`:117`).
- **(b) Step-up на критичных действиях — 🟡 + 🔴.** **Step-up — no-op для всех без 2FA** (`deps.py:91-92` «users without 2FA pass through unchanged»); 2FA опциональна, политики «обязательна для real-money» нет → по умолчанию защита **всех** критичных действий выключена. Покрытие: play ✅ (`live.py:498`), promote/demote ✅ (`:382,414`), edit-risk ✅ (`:342`), **создание** ключа ✅ (`exchange.py:60`). **🔴 PATCH `/accounts/{id}` (смена ключа) — НЕ гейтится** (`exchange.py:75 CurrentUser`); **🔴 DELETE — НЕ гейтится** (`:96`). Договор прямо называет «смену exchange-key» критической. *(Иронично: `config.py:38-41` комментарием декларирует step-up для «change exchange key», но эндпоинт его не требует.)* Single-use jti через Redis SETNX **fail-OPEN** на ошибке Redis (`deps.py:45-47`).
- **(c) Recovery codes — ✅** 10 кодов, hash at-rest, single-use атомарным UPDATE (`totp.py:40-41,161-187`).
- **(d) Lockout / throttle — 🟡** per-enrollment lockout 5 fails / 15 мин (`totp.py:28-33,124-131`). **🔴 Нет per-IP throttle и нет rate-limit на `/signin`** (`auth.py:99-123`) — password-spray без трения.
- **(e) Email confirmation — 🔴 MISSING.** Договор: «TOTP **+ email confirmation**». Нет SMTP/mailer/email-токена/флоу (grep по `app/` даёт только `EmailStr` и `User.email`). Половина пункта молча дропнута.
- **(f) Login-2FA — ✅** `/signin` отдаёт challenge при включённой 2FA (`auth.py:118-120`), `/2fa/login` меняет challenge+code на токены (`:126-152`).

### S7/S8 — Observability + Frontend

#### W12(a) — Real-time event tracing (SSE) — ✅ IMPLEMENTED
- Настоящий SSE: `EventSourceResponse` (`sse-starlette`) с `ping=15` (`events.py:63`); `GET /events/stream` auth-gated (`:28-29`). Per-user фильтр через Redis-канал `events:user:{id}` (`stream.py:52-53,131-136`). Источник — pub/sub; публикация на `after_commit` (`stream.py:113-123`), `STREAMABLE_EVENTS` включает `kill_switch_triggered`/`kpi_guard_triggered`/`portfolio_dd_halt`/`strategy_anomaly_detected` (`:32-49`). Hardening: per-user cap 5 → 429 (`events.py:21-43`), commit-gated publish. *(Соответствует документированным паттернам sse-starlette — §6.)*

#### W12(b) — Risk alerting (Telegram) — ✅ IMPLEMENTED
- Реальный outbox-диспетчер по `auto_trade_events` (`notifications/service.py:99,165,191`); крон `* * * * *` (`tasks.py:255`); идемпотентность по `event_id` + retry. `strategy_anomaly_detected` ∈ `RISK_EVENTS` (`formatting.py:42`).

#### W12(c) — Strategy Anomaly Detection — ✅ IMPLEMENTED 🔌 OFF-BY-DEFAULT
- Реальный статистический детектор: rolling z-score (pnl), drawdown-velocity z, win-rate-collapse z, EWM trade-frequency residual (`anomaly/detector.py:91-256`); fail-safe на коротком окне (NaN → no-flag, `:115-128`). Sweep cron `*/15` (`tasks.py:208-209`); дедуп 60-мин cooldown (`service.py:2144,2184-2195`); эмит `strategy_anomaly_detected` (`:2201-2225`).
- **Default OFF:** `anomaly_detection_enabled=False` (`schemas/auto_trade.py:85`); sweep берёт только конфиги с флагом true (`service.py:2156,2171`).

#### W12(d) — Production hardening — 🔴 CORS `*` по умолчанию
- `cors_allow_origins/methods/headers = ["*"]` (`config.py:90-92`); смягчает только `cors_allow_credentials=False` (`:93`). Должно переопределяться env на проде.

#### W12(e) — AI Forecast Catalogue UI — 🟡 PARTIAL (нет привязки к live)
- Trader-reachable: `/forecasts` → `ForecastCatalogue` (в `BASE_NAV`, `app-header.tsx:17`), не admin-only. Фильтры symbol/timeframe + Δ-vs-Baseline (`forecast-catalogue.tsx:44-59,82-86,190-198`).
- **🔴 «Attach to LIVE strategy» отсутствует.** «Use in strategy» ведёт в `/strategy?forecast=...` (`:100-102`, тултип «Preselect … in the strategy builder» `:245`) → `TradingDashboard` = backtest/paper-билдер. Форма live auto-trade конфига **не имеет ни одного** упоминания `forecast` (grep `components/auto-trade/*.tsx` пуст). Forecast привязывается к бэктесту/paper, не к live.

#### W12(f) — AI Decision Dashboard — 🟡 PARTIAL (нет отдельного экрана; accuracy/weights — admin-only)
- Только карточка на Auto Trade (`AutoTradeAiDecisionsCard`, `auto-trade-dashboard.tsx:1444`); отдельного роута нет. Показывает reasoning/ai_trend/confidence/per-agent weight (`ai-decisions-card.tsx:71-125`).
- **🔴 Agent accuracy и weight-suggestions — НЕ в трейдерской карточке.** Бэкенд-эндпоинты есть и `listAgentAccuracy` вызывается **только** в admin-дашборде (`admin-ai-backtest-config-dashboard.tsx:16,623`, admin-only). Трейдер их не видит.

#### W12(g) — Live Monitoring Dashboard — 🟡 PARTIAL (KPI = polling, не SSE)
- `/monitor` → `LiveMonitorDashboard` (trader-facing). KPI-карточки реальны: WR/ROI/Max DD/**Sharpe (proxy)**/running-DD/health/lifecycle badge (`strategy-monitor-card.tsx:117-128`). Play/stop/play-all/stop-all → реальные эндпоинты (`live-monitor-dashboard.tsx:155-226`).
- **🔴 KPI-цифры — `setInterval` polling, не SSE.** 30-сек poll `getAutoTradePortfolio()` (`:53 POLL_INTERVAL_MS=30_000`, `:128-149`; комментарий «AC#7 — auto-poll so the dashboard reads as 'live'» `:125`). SSE потребляется, но только для тостов риск-событий + триггер refetch (`:350-364`) — KPI-числа не несёт. Плашка «Live» — настоящий SSE-статус, что делает страницу «живее» данных. *(SSE-клиент сам по себе крепкий: один EventSource, backoff+jitter, 429-handling, Bearer из httpOnly, не в query-string — `stores/risk-events-store.ts:91-205`.)*

#### W12(h) — Strategy Lifecycle UI — ✅ IMPLEMENTED
- Stage badge (`LifecycleStageBadge`), gate-status (`PromotionGatePanel` + `getPromotionStatus`), promote/demote → реальные эндпоинты с 422-раскрытием критериев; step-up-модалка реальна (`lib/api/step-up.ts`, `step-up-modal.tsx`).

#### W12(i) — Risk Config UI — ✅ IMPLEMENTED (корректно прячет interface-only)
- Все 5 секций: Pre-Trade (`risk-section.tsx:58`), KPI Guard (`:164`), Kill-Switch (`:233`), Anomaly (`:298`), Promotion Gate (`:346`). Round-trip GET↔PUT (`buildRiskConfigPayload`, `auto-trade-dashboard.tsx:784`).
- **✅ Хорошее поведение:** conflicting-signal контрол предлагает **только энфорсимые** policy и исключает interface-only `net`/`replace` (комментарий `risk-section.tsx:21-24`) — чистая обработка ровно того поля, о котором предупреждает аудит.

---

## 5. Security — отдельный трек (реальный счёт!)

Приоритет, т.к. система торгует реальными деньгами на mainnet-аккаунте 17.

| # | Находка | Файл:строка | Severity |
|---|---|---|---|
| S1 | **Смена ключа биржи (PATCH) без step-up** — договор называет критической | `exchange.py:71-76` (`CurrentUser`) | 🔴 High |
| S2 | **Удаление ключа биржи (DELETE) без step-up** | `exchange.py:95-96` (`CurrentUser`) | 🔴 High |
| S3 | **Step-up — no-op для всех без 2FA**; 2FA опциональна, нет политики обязательности | `deps.py:91-92` | 🔴 High |
| S4 | **Дефолтный ключ шифрования** + нет startup-guard | `config.py:33` + отсутствие валидатора | 🔴 High |
| S5 | **`_normalize_key` SHA256-деривит любую строку** в валидный Fernet-ключ → слабый ключ не падает | `security.py:21-32` | 🔴 High |
| S6 | **Неаутентифицированный `/encrypt`-оракул** (ни `CurrentUser`, ни `RequireStepUp`) | `exchange.py:23-30` | 🟡 Medium |
| S7 | **Нет rate-limit на `/signin`** + нет per-IP throttle | `auth.py:99-123` | 🟡 Medium |
| S8 | **Step-up single-use fail-OPEN** на ошибке Redis (токен реюзабелен 5 мин) | `deps.py:45-47` | 🟡 Medium |
| S9 | **CORS дефолт `["*"]`** (спасает `credentials=False`) | `config.py:90-93` | 🟡 Medium |
| S10 | Нет ротации ключа шифрования (нет MultiFernet) — ротация осиротит ciphertext | `security.py` | 🟢 Low |
| S11 | (проверить) TOTP-replay внутри окна — pyotp его не даёт; нужно трекать last-used timecode | `totp.py` | 🟢 Low |

---

## 6. Doc-grounding (context7) — корректность ключевых библиотек

**pyotp** (`/websites/pyauth_github_io_pyotp`):
- `verify(otp, valid_window=1)` — расширяет валидность на N тиков до/после → ±30с. Реализация `totp.py:117` корректна по RFC 6238.
- `random_base32()` гарантирует ≥160 бит — корректно.
- ⚠️ **Документация подтверждает:** pyotp **не предоставляет replay-protection** — код валиден в своём окне многократно. Приложение должно само трекать использованный timecode (находка S11).

**cryptography / Fernet** (`/pyca/cryptography`):
- Ключ должен быть 32-байтным URL-safe base64 (`Fernet.generate_key()`).
- 🔴 **Документация прямо предписывает:** для ключа из пароля/строки использовать **KDF** — `Argon2id` или `PBKDF2HMAC` (соль 16+ байт, 1.2M итераций). Реализация же делает `hashlib.sha256(value)` без соли и итераций (`security.py:31`) — это **не** рекомендованный KDF: для низкоэнтропийного значения SHA256 даёт быстрый перебор. Обоснованно подтверждает находку S5.
- `MultiFernet` — штатный механизм ротации; в реализации отсутствует (S10).

**sse-starlette** (`/sysid/sse-starlette`):
- Документированные паттерны: `EventSourceResponse(gen, ping=15)`, `is_disconnected()`, `except asyncio.CancelledError: raise`, `shutdown_event`/grace. Реализация `events.py`/`stream.py` следует им (ping=15, commit-gated publish, per-user cap) — SSE-инфраструктура корректна.

---

## 7. Матрица «выключено по умолчанию» (governance)

Главная ценность M4 («real-money с полным риск-контролем») в дефолтной конфигурации **не работает**:

| Механизм | Флаг | Default | Без него |
|---|---|---|---|
| Pre-Trade master | `enabled` | **True** | но все лимиты NULL → правил нет |
| Conflicting-signal | `conflicting_signal_policy` | **"off"** | + `net`/`replace` interface-only даже при выборе |
| KPI-Guard авто-пауза | `kpi_guard_enabled` | **False** | AC#4 не срабатывает |
| Volatility Kill-Switch | `kill_switch_enabled` | **False** | W9 не срабатывает |
| Portfolio-DD halt | `portfolio_dd_halt_enabled` | **False** | W11 не срабатывает (+ прокси) |
| Anomaly detection | `anomaly_detection_enabled` | **False** | W12 не срабатывает |
| AI-overlay (адаптация) | все | **False** | ai_trend не влияет (W2c/W4) |

Перед включением на счёте требуется калибровка порогов с трейдерами (close-out это признаёт как «осталось» — что само по себе противоречит «весь объём закрыт»).

---

## 8. Расхождение документации с деревом

- `m4-closeout-plan.md:9-25` — «весь объём договора M4 закрыт, формального риска приёмки нет». Опровергается W5b (мертво), email-confirm (нет), §5 (дыры), §7 (всё off).
- `m4-closeout-plan.md:252` — `reconciliation.py` назван «empty stub»; **файла нет вовсе**.
- `pipeline.py:51-56` / live_tracker комментарии — «watcher-SL применяется через event bus»; bus **без потребителя** в проде → маскирует разрыв W5b.
- `state_machine.py:14-18` — docstring обещает sandbox «paper/dry-run no real orders»; фактически реальные ордера на demo-венью.
- `config.py:38-41` — комментарий обещает step-up на «change exchange key»; эндпоинт PATCH его не требует.

---

## 9. Top-находки по severity

1. **🔴 W5b In-Position Indicator Monitoring мертво в проде** — построено, но потребителя event-bus нет (только тесты). Договорный пункт не функционирует. Фикс ~1 строка.
2. **🔴 Security на реальном счёте** — смена/удаление ключа без step-up (S1/S2); step-up дремлет без 2FA (S3); слабый дефолтный ключ без guard (S4/S5, context7-обосновано); `/encrypt` без auth (S6); нет rate-limit логина (S7).
3. **🔴 Governance off-by-default** — KPI-Guard, Kill-Switch, portfolio-DD, anomaly, AI-overlay: ничего не активно из коробки (§7). AC#4 фактически дремлет.
4. **🟡 Семантика подменена** — Agent Accuracy на синтетической цене (W3a); weight-apply = orphan no-op (W3b); ai_trend не агрегирует агентов (W2a); Sandbox = real-orders, default `live` fail-open (W10e); portfolio-DD = прокси (W11a); «live» дашборд = polling (W12g).
5. **🔴 Дропнуто** — email-confirmation (W11c); worker isolation (W7c, опц.); lifecycle-audit таблица (W10f, defer).
6. **🟡 Frontend** — forecast→live attach нет (W12e); accuracy/weights admin-only (W12f).

---

## 10. Что действительно сделано хорошо (для баланса)

Подтверждено в коде, реально работает:
- Position FSM + enforcement + персист истории SL/TP (W5a).
- Динамический SL/TP как **настоящая** приоритетная цепочка (most-protective) + Multi-TP scaled exits + reconnect-sync с emergency-replace потерянного SL (W6).
- Изоляция мульти-стратегий через саб-аккаунты + unique-index (W7a / AC#3).
- SSE-инфраструктура по докам (W12a), идемпотентный Telegram-outbox (W12b).
- Anomaly-детектор — реальная математика (rolling z-score/EWM, fail-safe) (W12c).
- Promotion FSM + KPI-Gate с честным fail-safe + step-up на promote (W10 a/b/d).
- Каталог + Δ-vs-Baseline + еженедельный крон (W1 a/c).
- Vault: настоящий Fernet, шифрование at-rest (механизм; вопрос — к управлению ключом).
- Risk Config UI корректно прячет interface-only `net`/`replace` (W12i).

---

## 11. Рекомендации перед приёмкой

**Блокеры (must-fix):**
1. Подключить `subscribe_watcher_events(handle_watcher_event)` в `install_auto_trade_runtime` (W5b) — иначе пункт не выполнен.
2. Закрыть security: step-up на PATCH/DELETE `/accounts` (S1/S2); startup-guard на дефолтные secret/jwt/encryption ключи (S4); заменить SHA256-normalize на PBKDF2/Argon2id или требовать валидный 44-симв. Fernet-ключ (S5); закрыть `/encrypt` auth'ом (S6); rate-limit на `/signin` (S7).
3. Исправить fail-open `lifecycle_stage` default → `sandbox` (W10e), либо добавить stage-check в путь размещения ордера.

**Согласовать с заказчиком письменно (defer/change-request):**
4. «Enforcement по умолчанию off» — приемлемо ли (или включить с калиброванными порогами) (§7).
5. Дропнутый email-confirmation (W11c) — закрыт ли «либо другими решениями».
6. Portfolio-DD = прокси, не merged-equity (W11a) → M5.
7. Worker isolation (W7c) → M5 (опционально по договору).

**Желательно для соответствия формулировкам AC:**
8. Forecast → live-strategy attach на фронте (AC#1, W12e).
9. «Live» KPI через SSE, а не polling (AC#7, W12g).
10. Agent accuracy / weight-suggestions вынести в трейдерский UI (W12f).
11. Привести close-out-доки в соответствие с деревом (§8).

**Оценка приёмки:** объективно «зелёных» AC — **2–3 из 7** (#2, #3, частично #6); ещё 3–4 «жёлтых». Заявление «риска приёмки нет» не обосновано.

---

*Аудит подготовлен на основе чтения исходников трёх сервисов с фиксацией `файл:строка` и сверки библиотек по официальной документации (context7). Read-only; код не изменялся.*

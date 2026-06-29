# Outcome-Aware Agent (OA) — Spec

> Самообучающийся агент-анализатор прошлых прогнозов и их реальных исходов.
> Юзер включает его в своём personal-analysis; агент «дообучается» на парах
> `prediction → realized outcome` и влияет на решение «входить в сделку или нет».

Статус: **DRAFT — на согласовании.** Документ — единый источник правды для
фичи, охватывает оба сервиса (`core` — агент; `constructor` — исходы/shadow-лог).

---

## 0. Зафиксированные решения (locked decisions)

Четыре развилки согласованы с заказчиком до написания спеки:

| # | Развилка | Выбор | Следствие для дизайна |
|---|----------|-------|------------------------|
| D1 | Механизм «дообучения» | **In-context retrieval + калибровка вероятностей** (без fine-tuning LLM) | Эпизодическая память прошлых сделок + тонкий слой калибровки (`temperature → Platt/beta`). Веса LLM не трогаем. |
| D2 | Полномочия агента | **Advisory overlay** | OA отдаёт сигнал/уверенность и мягкий veto в analyst/writer (как RC/TM). Сделки сам не открывает. Hard-gate — out of scope (см. §12). |
| D3 | Объём данных для обучения | **Строго per-user / per-profile** | Калибровка и память — на уровне `(user_id, profile_id, symbol)`. **Критично:** данных мало → обязателен minimum-sample gate (§6.4, §7-P2). |
| D4 | Исходы пропущенных сделок | **Shadow-лог пропущенных** | Для решений «не входить» логируем «бумажный» исход, чтобы бить selection bias (§5.3). |
| D5 | Дефолтное состояние | **Выключено по умолчанию (opt-in)** | OA не сидится включённым ни в одном профиле; юзер включает тумблером в UI. Feature-flag глобальный + per-profile (§6.6, §8-UI). |
| D6 | EV-гейт в v1 | **Без издержек** | v1: гейт по калиброванной вероятности (порог), без fees/funding/slippage. Costs — отложенный задел (§6.2). |
| D7 | Прогнозы в NEUTRAL | **Логируются и оцениваются** | Даже когда OA в NEUTRAL (не влияет на решение), его прогноз всё равно пишется и потом сверяется с фактом — это и есть механизм накопления данных для выхода из NEUTRAL (§6.4). |

Обоснование D1/D4 — раздел §7 (подводные камни) и приложенный ресёрч-репорт
(López de Prado, Reflexion/Memento, калибровка Guo/Platt, Kelly/Thorp).

---

## 1. Objective & целевые пользователи

**Objective.** Дать пользователю опциональный агент `OA`, который:
1. собирает прошлые прогнозы его профиля (`ai_decision_events`) и их реальные
   исходы (`exchange_trade_ledger.realized_pnl` + shadow-лог пропущенных);
2. **калибрует** заявленную уверенность прогноза («70% bull») по фактической
   частоте попаданий этого профиля;
3. retrieval’ом подмешивает в контекст похожие прошлые ситуации и их исходы;
4. выдаёт advisory-сигнал `enter / skip / neutral` + откалиброванную
   вероятность + короткое объяснение, который мягко влияет на итоговый
   `aiTrend.bias`/`confidence` analyst/writer.

**Не-цель:** OA не торгует, не двигает SL/TP, не меняет размер позиции напрямую.

**Целевые пользователи.**
- *Конечный юзер* personal-analysis — включает `OA` тумблером в профиле, видит
  в выводе «исторически на похожих сетапах этот профиль был прав в X% случаев,
  edge Y% — calibrated confidence Z%».
- *Оператор/владелец платформы* — следит за governance (kill-switch, лимиты),
  смотрит метрики калибровки и точности.

---

## 2. Scope

### В scope
- Новый config-driven Mastra-агент `OA` в `core` (по шаблону `RC`/`TM`).
- Per-user/per-profile калибровка вероятностей (`temperature → Platt/beta`).
- Эпизодическая память + retrieval похожих прошлых сделок.
- Shadow-лог пропущенных сделок в `constructor`.
- Расширение `agent-outcomes`/accuracy-контура для нужд OA.
- Advisory-врезка в `gatherResearchData → analyst → writer`.
- AI-config поля: `oa_enabled` (дефолт OFF), `oa_model`, веса; глобальный
  feature-flag `OA_FEATURE_ENABLED` (дефолт OFF).
- Метрики и эндпоинт для просмотра калибровки/точности профиля.
- **Frontend (constructor-front):** тумблер on/off (дефолт OFF) + панель
  калибровки/accuracy с reliability-диаграммой (§8-UI).

### Вне scope (явно)
- Fine-tuning LLM (отклонено в D1).
- Hard-gate auto-trade / автоматический veto реальных ордеров (D2; задел в §12).
- Контекстные бандиты / RL (ресёрч: defer до стабильно высокого объёма сделок).
- Глобальный/кросс-юзер пул и Bayesian shrinkage (отклонено в D3; см. Open Q1).
- Изменение sizing/Kelly в auto-trade (только рекомендация в выводе, не действие).

---

## 3. Архитектура и размещение

OA живёт в **`core`** (там агенты, `ai_decision_events`, accuracy-контур).
`constructor` — источник истины по реальным исходам и место shadow-лога.

```text
                       ┌──────────────────────── core (NestJS + Mastra) ────────────────────────┐
                       │                                                                          │
 personal-analysis run │  gatherResearchData ──► [TW RQ RF NEWS TM RC  OA*]  ──► analyst ──► writer
                       │                                   │                                      │
                       │                                   ▼                                      │
                       │                     OutcomeAwareAgent.getSignal()                        │
                       │                       ├─ читает agent_accuracy_metrics (per profile)     │
                       │                       ├─ читает ai_decision_events (история прогнозов)    │
                       │                       ├─ CalibrationService (per-user temperature/Platt)  │
                       │                       └─ MemoryRecall (top-K похожих прошлых сетапов)     │
                       │                                                                          │
                       │  AgentAccuracyService.recompute()  ◄── GET /agent-outcomes ──┐           │
                       └──────────────────────────────────────────────────────────────┼──────────┘
                                                                                       │ HTTP (X-API-Key)
                       ┌──────────────────── constructor (FastAPI) ───────────────────┴──────────┐
                       │  internal_outcomes: realized исходы (decision_event_id → realized_move)  │
                       │  + NEW: shadow-исходы пропущенных сделок (skipped candidates)            │
                       │  auto_trade_positions.decision_event_id → exchange_trade_ledger.pnl      │
                       └─────────────────────────────────────────────────────────────────────────┘
```

`OA*` — новый агент. Звёздочка: участвует в live-пути; в **backtest пропускается**
(как `TM`/`RC`) во избежание look-ahead — см. §7-P1.

### Поток данных (end-to-end)
1. Personal-analysis tick → `gatherResearchData` параллельно дёргает агентов.
2. `OutcomeAwareAgent.getSignal(profile, symbol)`:
   - тянет accuracy/калибровку профиля и top-K похожих прошлых сетапов;
   - складывает «сырую» уверенность текущего ансамбля → калибрует → решает
     `enter/skip/neutral` по EV-гейту (§6).
3. Сигнал кладётся в `ResearchContext.outcomeAware` → в analyst-контекст и
   writer-промпт (мягко через bias/confidence; детерминированные `aiTrend`/
   `trendExtraction` не перетираются).
4. Прогноз пишется в `ai_decision_events` (как сейчас) + помечается
   `outcomeAware` блоком (calibrated p, decision, retrieved-ids).
5. Когда сделка закрывается (`constructor`) или, если пропущена, формируется
   shadow-исход → accuracy/калибровка профиля пересчитывается по расписанию.

---

## 4. Модель данных

### 4.1 Что уже есть (переиспользуем, не дублируем)
- `core` MongoDB `ai_decision_events` — прогноз: `symbol`, `occurredAt`,
  `aiConfigId`, `perAgent[]`, `aiTrend{direction,strength,probabilitiesPct}`,
  `resultSnapshot{action,confidence,bias}`, `outcomeJoinKey`.
- `core` MongoDB `agent_accuracy_metrics` — `hitRate`, `meanEdge`, `sampleSize`,
  `realSampleSize` по `(aiConfigId, agentKey, windowDays, horizonHours)`.
- `core` `AgentAccuracyService` — `fetchTradeOutcomes()`, `recomputeWindow()`,
  `suggestWeights()`, `bindAgentWeights()`.
- `constructor` `GET /api/v1/internal/agent-outcomes` → `decision_event_id →
  realized_move_pct`, `closed_at`, `side`, `symbol`.
- `constructor` join: `auto_trade_positions.decision_event_id` /
  `open_history_id` → `exchange_trade_ledger.realized_pnl` / `fee_cost`.

### 4.2 Новые сущности
**core (Mongo):**
- `oa_calibration` — per `(userId, profileId, symbol)`:
  `method ∈ {none, temperature, platt, beta}`, параметры (`T` / `a,b` / `a,b,c`),
  `sampleSize`, `fittedAt`, `holdoutBrier`, `holdoutLogLoss`, `ece`.
- `oa_signal_log` (опц., можно как поле в `ai_decision_events`) — что OA выдал:
  `rawConfidence`, `calibratedP`, `decision`, `evEstimate`, `retrievedEventIds[]`,
  `minSampleGateTripped: boolean`.

**constructor (Postgres, Alembic-миграция):**
- `oa_shadow_outcomes` — исходы **пропущенных** сделок (D4):
  `id`, `user_id`, `profile_id`, `symbol`, `decision_event_id` (nullable),
  `history_id` (FK), `signal_time_utc`, `predicted_direction`, `predicted_conf`,
  `horizon_end_utc`, `realized_move_pct` (заполняется по горизонту из OHLCV),
  `entered: false`, `created_at`. Уникальность по `(history_id)`.
  - Заполнение `realized_move_pct` — фоновой Taskiq-задачей по закрытию горизонта,
    `merge_asof(direction="backward")` строго по `signal_time_utc` (без look-ahead).

> Принцип: **не плодим join-таблиц.** Реальные исходы остаются в
> `exchange_trade_ledger`; новая таблица — только для censored/skipped кейсов.

### 4.3 Расширение `/agent-outcomes`
Добавить в выдачу `entered: true|false` и опционально включать shadow-исходы
(параметр `include_shadow=true`), чтобы accuracy/калибровка считались по полной
(executed + skipped) выборке профиля.

---

## 5. Механизм обучения (D1)

«Дообучение» = **обновление внешней памяти и калибратора, без изменения весов
LLM.** Три слоя:

### 5.1 Калибровка вероятностей (per-user, D3)
- Вход: пары `(rawConfidence, hit ∈ {0,1})` профиля за окно.
- Метод по объёму выборки:
  - `n < N_min` (дефолт **20**) → `method=none`, агент в NEUTRAL (см. §6.4).
  - `N_min ≤ n < 100` → **temperature scaling** (1 параметр — устойчив на малых n).
  - `n ≥ 100` → **Platt** или **beta** (beta содержит identity-map → безопасна).
  - **Никогда не isotonic** на этих объёмах (overfit < ~1000).
- Фит — на **time-ordered held-out** фолде (walk-forward), не на тех же данных.
- Хранилище: `oa_calibration`; пересчёт по расписанию (§ cadence).
- Метрики качества: reliability diagram, Brier (reliability-терм), log-loss,
  adaptive-bin ECE. Регрессию калибровки логируем.

### 5.2 Эпизодическая память + retrieval (in-context)
- Каждый закрытый/пропущенный сетап → запись `(context, prediction, confidence,
  outcome, lesson)` в vector-store (Mastra Memory: `LibSQLVector` +
  `text-embedding-3-small`, как `chat-memory.db`).
- На решении: `semanticRecall topK=3..5` похожих прошлых сетапов **того же
  профиля**, с **time-decay** и **regime-tag** фильтром (favor текущий режим).
- «Урок» (lesson) формируется только когда повторений достаточно для значимости
  — единичный исход не превращаем в «правило» (anti-Reflexion-noise, §7-P5).
- Retrieved-исходы кладём в промпт OA как few-shot контекст.

### 5.3 Shadow-лог пропущенных (D4)
- Решение `skip` → пишем `oa_shadow_outcomes` с `predicted_*` и `signal_time_utc`.
- По истечении горизонта фоновая задача проставляет `realized_move_pct`.
- Эти строки участвуют в калибровке/accuracy → агент «видит» цену пропусков и
  не страдает selection bias.
- ε-exploration (опц., дефолт выкл.): с малой вероятностью логировать как
  «would-enter» даже при skip, чтобы пробивать слепую зону 0.5–0.8 confidence.

---

## 6. Решающее правило (advisory, D2)

### 6.1 Из калиброванной p в сигнал
`calibratedP = calibrate(rawEnsembleConfidence)` для направления прогноза.

### 6.2 Гейт по калиброванной вероятности (v1 — без издержек, D6)
**v1 (текущая версия):** решение принимается только по калиброванной
вероятности, издержки НЕ учитываются.
- `enter` ⟺ `calibratedP ≥ pThreshold` (дефолт, напр. 0.55) в направлении прогноза.
- `skip` ⟺ `calibratedP < pThreshold`.
- `pThreshold` фиксируется in-sample, оценивается строго OOS (§7-P6).

**Отложенный задел (НЕ в v1):** полноценный EV-гейт над издержками
`EV = calibratedP·W − (1−calibratedP)·L − C_roundtrip`,
`C_roundtrip = 2·fees + 2·spread + 2·slippage + funding·(hold/8h)`.
Включается, когда появится источник costs/payoff на символ (см. OQ2 — закрыт:
в v1 не берём). Архитектурно `decide()` принимает опц. `costs`, в v1 = null.

### 6.3 Sizing — только рекомендация
OA **может** вернуть рекомендованный fractional-Kelly размер
`λ·f*, λ ≤ 0.5` (текстом, в выводе), но **не применяет** его (D2).
В v1 (без costs) — sizing-рекомендация опциональна/выключена.

### 6.4 Minimum-sample gate и оценка прогнозов в NEUTRAL (D3 + D7)
- Пока `oa_calibration.sampleSize < N_min` ИЛИ калибровка «протухла» →
  OA отдаёт **NEUTRAL**: `decision=neutral`, влияние на bias = 0,
  `minSampleGateTripped=true`, в выводе честно: «недостаточно истории профиля».
- NEUTRAL = агент технически работает, но не двигает решение. Это явно
  предотвращает «обучение на 3 сделках».
- **Критично (D7):** прогноз OA в режиме NEUTRAL **всё равно записывается**
  (`oa_signal_log` / поле в `ai_decision_events`) с тем направлением и сырой
  уверенностью, которые OA бы выдал, и **позже сверяется с фактическим исходом**
  (executed или shadow). Это ровно тот поток данных, который наполняет
  `oa_calibration.sampleSize` и в итоге выводит профиль из NEUTRAL.
  - Т.е. NEUTRAL «молчит» только для решения, но не для обучения/учёта точности.
  - Accuracy/калибровка в NEUTRAL-фазе считаются и доступны в UI (§8-UI), чтобы
    юзер видел: «агент пока копит данные, его теневая точность = X% за N прогнозов».

### 6.5 Влияние на pipeline (мягкое)
- `enter/skip` → корректирует `bias`/`confidence` в analyst/writer-контексте
  через вес `agent_weights.outcomeAware` (как RC).
- Детерминированные `aiTrend`/`trendExtraction` не перетираются.

### 6.6 Управление включением (D5 — opt-in, по умолчанию OFF)
- **По умолчанию выключено** во всех профилях; seed НЕ добавляет `"OA"` в
  `enabled_agents` по дефолту (в отличие от RC).
- Двухуровневый контроль:
  - глобальный feature-flag (env `OA_FEATURE_ENABLED`, дефолт `false`) —
    рубильник всей фичи;
  - per-profile `oa_enabled` — тумблер юзера (дефолт `false`).
- Оба должны быть `true`, чтобы OA вызывался. UI-тумблер (§8-UI) управляет
  per-profile флагом.

---

## 7. Подводные камни и митигации

Каждый пункт — из ресёрча, с конкретной защитой в этой фиче.

- **P1. Look-ahead / data leakage.** OA пропускается в backtest (как TM/RC);
  калибровка/shadow используют `merge_asof(backward)` строго по `signal_time_utc`;
  любые трансформации фитятся только внутри train-фолда; purge+embargo в
  walk-forward. **Acceptance:** тест, что backtest-путь не вызывает OA.
- **P2. Overfitting на малой свежей выборке (усилено D3).** Minimum-sample gate
  (§6.4); temperature вместо гибких калибраторов; time-decay; regime-segmentation.
  **Acceptance:** при `n<N_min` сигнал строго NEUTRAL.
- **P3. Censored / selection bias.** Shadow-лог (§5.3) + `include_shadow` в
  accuracy. **Acceptance:** калибровка считается по executed+skipped.
- **P4. Performative feedback (агент меняет распределение).** OA advisory (D2),
  не масштабирует ордера → околонеперформативен по построению.
- **P5. Reflexion-шум (ложные «уроки»).** «Урок» только при ≥ порога повторений;
  единичный исход не правило.
- **P6. Multiple-testing / p-hacking порогов.** `pThreshold` (v1) фиксируется
  in-sample; оценка строго OOS; логируем все испробованные конфиги; при оценке
  edge — deflated Sharpe с числом trials.
- **P7. Goodhart на метрике.** Не оптимизируем один hitRate; следим за связкой
  hitRate + meanEdge + drawdown/CVaR; ECE — мониторим, **не оптимизируем** напрямую.
- **P8. Class imbalance / base-rate.** Сравнение с naive-базлайном (always-on /
  buy-hold); precision/recall, не только accuracy.
- **P9. Concept drift.** Distribution-детектор (ADWIN/KSWIN) по фичам +
  error-rate (DDM) по исходам; при дрейфе — пересчёт калибровки/уход в NEUTRAL.
- **P10. Governance.** Hard kill-switch `oa_enabled`, лимиты — вне обучающейся
  петли; калибратор не может сам себя «разрешить» при недостатке данных.

---

## 8. Команды (dev workflow)

**core** (Node ≥22, nvm v24.16.0 — см. memory `core-node-version-tests`):
```bash
cd core
npm run build
npm test                    # jest
npm test -- agent-accuracy   # таргетный прогон
npm run start:dev
```

**constructor** (uv; Postgres через docker-compose):
```bash
cd constructor
uv sync
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "oa_shadow_outcomes"
uv run pytest tests/ -k "oa or shadow or outcomes"
uv run ruff check . && uv run mypy app
uv run uvicorn app.main:app --reload
```

Локальная Alembic-проверка — на throwaway PG :55432 (memory
`alembic-migration-local-verify`).

**constructor-front** (Node ≥18, nvm v20.20.2 — memory `constructor-front-node-version`):
```bash
cd constructor-front
npm run gen:api-types   # после изменения openapi.json (Node ≥18!)
npm run dev
npm test                # vitest
npm run lint
```

---

## 8-UI. Frontend (constructor-front) — OQ5 закрыт: делаем UI

Стек: React 19 + Vite + Tailwind v4 + shadcn/radix + zustand +
**lightweight-charts** (TradingView) + lucide-react. Типы генерятся из
`openapi.json` (`npm run gen:api-types`, memory `front-openapi-codegen`).

### Что добавляем
1. **Тумблер OA on/off** в форме personal-analysis-профиля (D5).
   - shadcn `Switch` + label «Outcome-Aware (обучение на прошлых исходах)».
   - `PUT .../personal/profiles/{id}` с `oa_enabled`. **Дефолт OFF.**
   - Disabled + tooltip, если глобальный `OA_FEATURE_ENABLED=false`.
2. **Панель калибровки/точности профиля** (видна при включённом OA; в NEUTRAL
   показывает «копим данные», D7):
   - shadcn `Card`: `sampleSize`, `hitRate` vs naive-базлайн, `meanEdge`,
     `Brier`/`logLoss`, статус (`NEUTRAL`/`active`), `method`.
   - **Reliability diagram** — предсказанная вероятность (X 0–1) vs фактическая
     частота (Y 0–1) + диагональ идеальной калибровки.
     - **Реализация: кастомный SVG**, НЕ lightweight-charts. Причина (подтв.
       доку lightweight-charts через context7): ось X у lightweight-charts —
       временная, произвольный числовой X (вероятность) не поддержан из коробки.
       Диаграмма простая (диагональ + точки по бинам с counts) → маленький SVG.
   - **Accuracy-over-time** (опц.) — здесь уместен lightweight-charts
     `addLineSeries` по времени (hitRate по окнам).
3. **Бейдж в выводе анализа** — active: «OA: calibrated p = Z%, решение
   enter/skip, истор. точность профиля = X%»; NEUTRAL: «OA копит данные (N/N_min)».

### API для UI (новое в constructor)
- `GET /api/v1/analysis/personal/profiles/{id}/oa-calibration` — агрегирует из
  core: `method`, params, `sampleSize`, `hitRate`, `meanEdge`, `brier`,
  `logLoss`, `ece`, `reliabilityBins[]`, `status`.
- `oa_enabled` в схему `PersonalAnalysisProfile` → openapi → ре-ген типов фронта.

### Файлы фронта
| Файл | Изменение |
|------|-----------|
| `src/.../profile-form/*` | shadcn `Switch` для `oa_enabled` + tooltip |
| `src/components/oa/OaCalibrationPanel.tsx` | карточки + reliability SVG |
| `src/components/oa/ReliabilityDiagram.tsx` | кастомный SVG (диагональ+бины) |
| `src/components/oa/OaSignalBadge.tsx` | бейдж в выводе анализа |
| `src/api/*` (gen) | ре-ген типов после правки `openapi.json` |
| zustand store | состояние тумблера/калибровки профиля |

---

## 9. Структура проекта (файлы)

### core (агент — чек-лист по шаблону RC/TM)
| Шаг | Файл | Изменение |
|-----|------|-----------|
| 1 | `src/mastra/agents/outcome-aware-agent.ts` | новый Agent-класс (копия `risk-control-agent.ts`) |
| 2 | `src/mastra/tools/outcome-aware.ts` | tool: читает accuracy/calibration/recall |
| 3 | `src/mastra/mastra.module.ts` | provider + DI tool |
| 4 | `src/mastra/mastra.service.ts` | inject, register, `getOutcomeAwareSignal()` |
| 5 | `src/analysis/ai-config.types.ts` | `"OA"` в кодах, `oa_enabled`/`oa_model`, map `OA→outcomeAware` |
| 6 | `src/analysis/ai-config-schema.ts` | UI-группа + поля |
| 7 | `src/analysis/ai-config-seed.ts` | `oaEnabled: false` в BASE; `"OA"` НЕ в дефолтных `enabled_agents` (opt-in, D5) |
| 7b | env `OA_FEATURE_ENABLED` | глобальный feature-flag (дефолт `false`) в обоих сервисах |
| 8 | `src/chat/agents.config.ts` | `AgentKey += outcomeAware`, AVAILABLE_AGENTS |
| 9 | `src/analysis/analysis.service.ts` | task в `gatherResearchData`, `ResearchContext.outcomeAware` |
| 10 | `src/analysis/oa-calibration.service.ts` | новый: fit/apply temperature/Platt/beta + хранилище |
| 11 | `src/analysis/schemas/oa-calibration.schema.ts` | Mongo-схема |

### constructor (исходы/shadow)
| Файл | Изменение |
|------|-----------|
| `app/models/oa_shadow_outcome.py` | новая модель |
| `migrations/versions/*_oa_shadow_outcomes.py` | Alembic |
| `app/api/v1/endpoints/internal_outcomes.py` | `entered` + `include_shadow` |
| `app/services/.../oa_shadow.py` | запись skip-кандидатов + бэкфилл realized по горизонту |
| `app/worker/tasks.py` | Taskiq-задача бэкфилла shadow-исходов |

---

## 10. Code style
- Следовать стилю окружения: `core` — NestJS DI + Mastra-агенты «один класс =
  один агент», как `tech-model-agent.ts`; `constructor` — FastAPI + SQLAlchemy
  async + Pydantic v2, `ruff` + `mypy` чисто.
- Никаких новых тяжёлых зависимостей: калибровку (temperature/Platt/beta)
  реализуем на чистом TS (несколько формул), без ML-фреймворка.
- Все новые env-флаги — в `.env.example` обоих сервисов с дефолтами.
- Комментарии и плотность — как в соседнем коде; bilingual RU/EN ок (как в репо).

---

## 11. Стратегия тестирования
- **Unit (core):** калибраторы (temperature/Platt/beta) на синтетике — проверка
  что reliability улучшается на held-out; minimum-sample gate → NEUTRAL.
- **Unit (core):** гейт `calibratedP ≥ pThreshold` → enter/skip; NEUTRAL по gate.
- **Unit (core, D7):** в NEUTRAL прогноз всё равно записывается и сверяется с
  исходом (поток данных не прерывается).
- **Integration (core):** OA пропускается в backtest-пути (anti-look-ahead).
- **Integration (constructor):** `oa_shadow_outcomes` бэкфилл `merge_asof`
  без forward-leak; `/agent-outcomes?include_shadow=true`; `oa-calibration` эндпоинт.
- **Property/regression:** калибровка не ухудшает Brier на OOS-фолде.
- **E2E:** `oa_enabled=true` → сигнал/калибровка видны в выводе и в UI-панели;
  `oa_enabled=false` или `OA_FEATURE_ENABLED=false` → OA не вызывается.
- **Frontend (vitest):** тумблер шлёт `oa_enabled`; панель/reliability-SVG
  рендерится из `oa-calibration`; NEUTRAL-состояние «копим данные».
- **Eval (Mastra scorers):** качество объяснений OA (опц.).
- Прогон quality-gates всех сервисов (`pytest/ruff/mypy`, `jest/build`, `vitest/lint`).

---

## 12. Rollout, governance, acceptance

### Поэтапный rollout
1. **Shadow / paper (= NEUTRAL-фаза, D7):** OA считает и логирует сигнал, но
   влияние на bias = 0; копим калибровку и shadow-исходы; прогнозы оцениваются
   по факту. UI показывает «копим данные».
2. **Advisory-on:** при достижении `N_min` и валидной калибровке — мягкое
   влияние через `agent_weights.outcomeAware` на opt-in профилях.
3. **(Будущее, вне scope)** EV-гейт над издержками (D6-задел) и/или hard-gate
   auto-trade — только после shadow-валидации, за отдельным kill-switch и
   human-in-the-loop (D2 задел).

### Governance (hard guardrails вне петли)
- `oa_enabled` kill-switch (профиль) + глобальный feature-flag.
- OA не имеет доступа к ордерам/риск-лимитам и не может их менять.
- Калибратор не «самоодобряется» при недостатке данных (gate).

### Acceptance criteria
- [ ] AC1: юзер включает `OA` тумблером в UI профиля (дефолт OFF) → агент
      участвует в personal-analysis, сигнал и calibrated-confidence видны в выводе.
- [ ] AC2: при `sampleSize < N_min` OA строго NEUTRAL (влияние = 0), НО прогноз
      записывается и оценивается по факту (D7).
- [ ] AC3: калибровка фитится на time-ordered held-out; Brier/log-loss на OOS
      не хуже сырых вероятностей.
- [ ] AC4: shadow-исходы пропущенных пишутся и попадают в accuracy.
- [ ] AC5: OA не вызывается в backtest-пути (anti-look-ahead тест зелёный).
- [ ] AC6: `oa_enabled=false` ИЛИ `OA_FEATURE_ENABLED=false` полностью отключает
      агент (нет вызовов, нет влияния).
- [ ] AC7: UI — тумблер on/off + панель калибровки/accuracy (reliability SVG)
      рендерятся; в NEUTRAL показывают «копим данные (N/N_min)».
- [ ] AC8: quality-gates всех трёх сервисов зелёные.

---

## 13. Boundaries

**Always (делаем всегда):**
- Калибровать на held-out, не на тех же данных.
- Уважать as-of / point-in-time; пропускать OA в backtest.
- Держать minimum-sample gate и NEUTRAL по умолчанию при нехватке данных.
- Логировать пропущенные сделки (shadow) и считать по полной выборке.

**Ask-first (спросить заказчика):**
- Любой переход к hard-gate реальных ордеров.
- Включение EV-гейта над издержками (D6-задел) — нужен источник costs.
- Включение ε-exploration (тратит реальные деньги/искажает).
- Дефолтное включение `OA` (сейчас строго OFF, D5) для всех профилей.
- Включение глобального пула/shrinkage (меняет D3).

**Never (не делаем):**
- Не fine-tune'им LLM на исходах (D1).
- OA не открывает/не закрывает сделки и не двигает SL/TP (D2).
- Не оптимизируем пороги на той же выборке, где меряем edge.
- Не используем isotonic на малых выборках.
- Не превышаем full-Kelly в рекомендациях по размеру.

---

## 14. Open questions

### Закрыто заказчиком
- **OQ1 — ЗАКРЫТО:** строгий per-user ок; профили часто будут в NEUTRAL —
  это приемлемо. **Но** прогнозы в NEUTRAL обязаны логироваться и оцениваться
  по факту (→ D7, §6.4). Переход к global-prior — позже, Ask-first.
- **OQ2 — ЗАКРЫТО:** в v1 издержки (fees/funding/slippage) НЕ учитываем; гейт по
  калиброванной вероятности (→ D6, §6.2). EV-гейт над costs — отложенный задел.
- **OQ5 — ЗАКРЫТО:** UI делаем (§8-UI); фича opt-in, по умолчанию OFF, с
  тумблером on/off в UI (→ D5).

### Ещё открыто
- **OQ3:** хранить `oa_signal_log` отдельной коллекцией или полем в
  `ai_decision_events`? (предпочтение: поле/субдок в `ai_decision_events`).
- **OQ4:** горизонт оценки исхода (`horizonHours`) — единый (как сейчас 72ч) или
  настраиваемый на профиль?
- **OQ6 (новый):** `N_min` и `pThreshold` — какие дефолты? (в спеке 20 и 0.55 как
  стартовые, уточнить под рынок/символ).

---

*Источники ресёрча (ключевые): López de Prado — Deflated Sharpe / PBO /
Purged CV; Reflexion (2303.11366), Memento (2508.16153); калибровка — Guo 2017
(temperature), Platt, Niculescu-Mizil & Caruana, beta-calibration; Kelly —
MacLean/Thorp/Ziemba; selection bias / MNAR; performative prediction; concept
drift (ADWIN/DDM). Полный список — в ресёрч-отчёте сессии.*

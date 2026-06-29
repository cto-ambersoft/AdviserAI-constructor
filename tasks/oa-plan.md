# Outcome-Aware Agent (OA) — План реализации

Источник истины по дизайну: [OUTCOME_AGENT_SPEC.md](../OUTCOME_AGENT_SPEC.md).
Этот документ — декомпозиция на вертикальные срезы, граф зависимостей,
чекпоинты и критерии приёмки. Чек-лист: [oa-todo.md](oa-todo.md).

Затрагивает 3 сервиса: `core` (агент + калибровка), `constructor` (shadow-исходы
+ API), `constructor-front` (тумблер + панель).

Принципы: каждый срез — **полный путь** (наблюдаемый инкремент), не горизонтальный
слой. Дефолт фичи — **OFF** на всех этапах. Сначала замыкаем data-loop в режиме
NEUTRAL (нулевое влияние), и только потом включаем влияние на решение.

---

## 1. Граф зависимостей

```text
                ┌─────────────────────────────────────────────┐
                │ S1  Walking skeleton (NEUTRAL, zero-influence)│
                │  core: OA agent + oaEnabled + pipeline врезка │
                │  + oa_signal_log (D7)                         │
                │  constructor: PersonalAnalysisProfile.oa_enabled
                │  → http_provider прокидывает в core           │
                └───────────────┬─────────────────────────────┘
                                │ (предсказания логируются)
            ┌───────────────────┴───────────────────┐
            ▼                                        ▼
 ┌────────────────────────────┐        ┌──────────────────────────────┐
 │ S2 constructor: shadow      │        │ (S6 retrieval — независим,   │
 │  oa_shadow_outcomes + миграц│        │  можно параллельно после S1) │
 │  + Taskiq backfill realized │        └──────────────────────────────┘
 │  + /agent-outcomes:entered, │
 │    include_shadow           │
 └───────────────┬─────────────┘
                 ▼
 ┌────────────────────────────┐
 │ S3 core: OA accuracy        │  (executed + shadow → hit/edge per profile)
 │  per (user,profile,symbol)  │
 └───────────────┬─────────────┘
   ── CHECKPOINT A: data-loop замкнут (всё ещё NEUTRAL) ──
                 ▼
 ┌────────────────────────────┐
 │ S4 core: OaCalibrationService│  temperature→Platt/beta, held-out, min-sample gate
 │  + oa_calibration schema     │
 └───────────────┬─────────────┘
                 ▼
 ┌────────────────────────────┐
 │ S5 core: decision + advisory │  calibratedP≥pThreshold → enter/skip/neutral;
 │  влияние via agent_weights;  │  backtest-skip (anti-look-ahead)
 │  включение влияния           │
 └───────────────┬─────────────┘
   ── CHECKPOINT B: агент «умный», advisory-on для opt-in ──
                 ▼
 ┌────────────────────────────┐     ┌────────────────────────────┐
 │ S7 constructor: oa-calibration│   │ S6 core: episodic memory +  │
 │  API + openapi + типы         │   │  semantic recall (опц.)     │
 └───────────────┬───────────────┘   └────────────────────────────┘
        ┌────────┴────────┐
        ▼                 ▼
 ┌─────────────┐   ┌────────────────────────────┐
 │ S8 front:   │   │ S9 front: calibration panel │
 │  toggle on/off│  │  + reliability SVG + badge  │
 └─────────────┘   └────────────────────────────┘
   ── CHECKPOINT C: full UI, opt-in, end-to-end ──
```

**Критический путь:** S1 → S2 → S3 → S4 → S5 → (S7 → S9). S6 и S8 — боковые ветки.

---

## 2. Срезы (vertical slices)

Версии/тулинг: `core` Node ≥22 (nvm v24.16.0); `constructor` uv + PG (throwaway
:55432 для миграций); `constructor-front` Node ≥18 (nvm v20.20.2). См. memory-файлы.

### S1 — Walking skeleton: OA в NEUTRAL, end-to-end, нулевое влияние
**Цель:** включаемый per-profile агент `OA`, который вызывается, выдаёт NEUTRAL,
логирует свой прогноз, и НЕ влияет на решение. Дефолт OFF.

Файлы (core): `mastra/agents/outcome-aware-agent.ts` (копия `risk-control-agent.ts`),
`mastra/tools/outcome-aware.ts` (заглушка → NEUTRAL), `mastra/mastra.module.ts`,
`mastra/mastra.service.ts` (`getOutcomeAwareSignal()`), `analysis/ai-config.types.ts`
(`oa_enabled`/`oa_model`, map `OA→outcomeAware`; **НЕ** в дефолтных `enabled_agents`),
`analysis/ai-config-schema.ts`, `analysis/ai-config-seed.ts` (`oaEnabled:false`),
`chat/agents.config.ts`, `analysis/analysis.service.ts` (task в `gatherResearchData`,
`ResearchContext.outcomeAware`, влияние=0), `analysis/schemas/ai-decision-event.schema.ts`
(+ субдок `outcomeAware`: rawConfidence, direction, decision, minSampleGateTripped),
env `OA_FEATURE_ENABLED` (default false).

Файлы (constructor): `models/personal_analysis_profile.py` (+`oa_enabled` bool, default
false) + Alembic-миграция, `schemas/personal_analysis.py`, `services/personal_analysis/
http_provider.py` (прокинуть `oa_enabled` в payload к core).

**Acceptance:**
- `oa_enabled=true` И `OA_FEATURE_ENABLED=true` → OA вызывается, пишет субдок-прогноз
  в `ai_decision_events`, возвращает NEUTRAL.
- любой из флагов false → OA не вызывается (нет субдока).
- bias/aiTrend/trendExtraction идентичны прогону без OA (нулевое влияние) — golden-тест.

**Verify:** `cd core && npm run build && npm test`; ручной personal-analysis с
профилем `oa_enabled=true`, проверить субдок в Mongo и неизменность bias.
`cd constructor && uv run alembic upgrade head` (на :55432), `uv run pytest -k personal`.

---

### S2 — constructor: shadow-исходы пропущенных + расширение outcomes
**Цель:** замкнуть censored-данные (D4) и отдать полную выборку наружу.

Файлы: `models/oa_shadow_outcome.py` (user_id, profile_id, symbol, decision_event_id?,
history_id FK, signal_time_utc, predicted_direction, predicted_conf, horizon_end_utc,
realized_move_pct null, entered=false; uq по history_id) + Alembic-миграция;
`services/.../oa_shadow.py` (запись skip-кандидата при decision=skip/neutral-shadow;
backfill `realized_move_pct` через `merge_asof(backward)` строго по signal_time_utc);
`worker/tasks.py` (Taskiq-задача бэкфилла — по образцу существующей
`sync_auto_trade_exchange_trades`, раз в минуту/по горизонту);
`api/v1/endpoints/internal_outcomes.py` (+поле `entered`, +параметр `include_shadow`).

**Acceptance:**
- skip-прогноз пишет строку в `oa_shadow_outcomes` (idempotent по history_id).
- backfill проставляет `realized_move_pct` только по закрытию горизонта, без
  forward-leak (тест на as-of).
- `GET /agent-outcomes?include_shadow=true` отдаёт executed+shadow с `entered`.

**Verify:** `uv run pytest -k "shadow or outcomes"`, `uv run ruff check . && uv run mypy app`.

---

### S3 — core: OA accuracy per profile (executed + shadow)
**Цель:** считать hit/edge OA по полной выборке профиля для будущей калибровки.

Файлы: расширить `analysis/agent-accuracy.service.ts` (`fetchTradeOutcomes` с
`include_shadow=true`; агрегировать по `(userId,profileId,symbol,windowDays,horizon)`),
переиспользовать `agent-accuracy-metric.schema.ts` (или новая `oa_accuracy` коллекция
если ключи иные). Сопоставление прогноз↔исход по `decisionEventId`/`outcomeJoinKey`.

**Acceptance:**
- accuracy OA считается по executed+shadow, ключ per-profile.
- прогнозы из NEUTRAL-фазы учитываются (D7) — тест.
- пустая история → метрики пустые, без падений.

**Verify:** `npm test -- agent-accuracy`; unit на join prediction↔outcome.

> **CHECKPOINT A** — data-loop замкнут: предсказания (вкл. NEUTRAL) логируются,
> исходы (вкл. shadow) втягиваются, accuracy считается per-profile. Влияние = 0.
> Демо: профиль копит N прогнозов → видно accuracy в Mongo/логе. Ревью заказчика.

---

### S4 — core: OaCalibrationService (калибровка вероятностей)
**Цель:** превратить сырую уверенность в честную вероятность; min-sample gate.

Файлы: `analysis/oa-calibration.service.ts` (fit temperature `n∈[N_min,100)`,
Platt/beta `n≥100`; **никогда isotonic**; фит на time-ordered held-out фолде),
`analysis/schemas/oa-calibration.schema.ts` (method, params, sampleSize, fittedAt,
holdoutBrier, holdoutLogLoss, ece, reliabilityBins[], status). Чистый TS, без ML-либ.
Пересчёт по расписанию (cron/по N новых исходов).

**Acceptance:**
- `n<N_min` → method=none, status=NEUTRAL (gate).
- калибровка не ухудшает Brier/logLoss на OOS-фолде (regression-тест на синтетике).
- фит только на held-out, не на тех же данных (тест на отсутствие in-sample утечки).

**Verify:** `npm test -- oa-calibration` (синтетика: переоверенный вход → улучшение
reliability на held-out).

---

### S5 — core: decision rule + advisory-влияние + backtest-skip
**Цель:** включить мягкое влияние OA для opt-in профилей.

Файлы: `mastra/tools/outcome-aware.ts` (реальная логика: читает calibration+accuracy
→ `calibratedP` → гейт `≥pThreshold` → enter/skip/neutral; min-sample gate→NEUTRAL),
`analysis/analysis.service.ts` (влияние на bias/confidence через
`agent_weights.outcomeAware`, как RC; детерминированные aiTrend/trendExtraction не
трогаем; **пропуск OA в backtest-пути**), дефолты `N_min=20`, `pThreshold=0.55` (OQ6).

**Acceptance:**
- active-профиль с `calibratedP≥pThreshold` → enter, мягко двигает bias по весу.
- `n<N_min` → NEUTRAL, влияние=0, но прогноз логируется (D7).
- OA не вызывается в backtest-пути — anti-look-ahead тест зелёный.
- выключение любого флага → нулевое влияние.

**Verify:** `npm run build && npm test`; backtest-интеграционный тест (OA не дёргается);
E2E personal-analysis: active vs NEUTRAL отражается в выводе.

> **CHECKPOINT B** — агент «умный»: калибруется, гейтит, advisory-on для opt-in.
> Демо: профиль с историей → calibrated p и enter/skip в выводе; новый профиль →
> NEUTRAL «копим данные». Ревью заказчика перед фронтом.

---

### S6 — core: эпизодическая память + retrieval (опционально, боковая ветка)
**Цель:** подмешивать top-K похожих прошлых сетапов в промпт OA (in-context, D1).

Файлы: Mastra `Memory` (`LibSQLVector` + `text-embedding-3-small`, как `chat-memory.db`);
запись `(context,prediction,confidence,outcome,lesson)` по закрытию/skip; recall
`topK=3..5` с time-decay + regime-tag фильтром; «урок» только при ≥ порога повторений
(anti-Reflexion-noise). Док Mastra Memory — через context7 при реализации.

**Acceptance:** recall возвращает релевантные прошлые сетапы того же профиля;
единичный исход не превращается в «правило». Может включаться флагом.

**Verify:** `npm test -- oa-memory`; ручная проверка recall на сид-данных.

---

### S7 — constructor: API калибровки + openapi + типы
**Цель:** отдать фронту данные калибровки/accuracy профиля.

Файлы: `api/v1/endpoints/personal_analysis.py`
(`GET /personal/profiles/{id}/oa-calibration` → агрегирует из core: method, params,
sampleSize, hitRate, meanEdge, brier, logLoss, ece, reliabilityBins[], status);
прокинуть `oa_enabled` в read/update схему профиля; регенерация `openapi.json`.

**Acceptance:** эндпоинт отдаёт калибровку/accuracy; `oa_enabled` в схеме профиля;
openapi обновлён.

**Verify:** `uv run pytest -k oa_calibration`; `openapi.json` содержит новые поля.

---

### S8 — front: тумблер OA on/off (default OFF)
**Цель:** юзер включает/выключает OA в профиле.

Файлы: `gen:api-types` (Node ≥18!); shadcn `Switch` в форме профиля → `PUT
.../personal/profiles/{id}` c `oa_enabled`; disabled+tooltip при
`OA_FEATURE_ENABLED=false`; zustand-состояние.

**Acceptance:** тумблер шлёт `oa_enabled`; дефолт OFF; при глобально выключенной
фиче — disabled с подсказкой.

**Verify:** `cd constructor-front && npm test && npm run lint`; ручной клик в dev.

---

### S9 — front: панель калибровки/accuracy + reliability SVG + бейдж
**Цель:** показать юзеру, как OA откалиброван и насколько точен.

Файлы: `components/oa/OaCalibrationPanel.tsx` (shadcn Card: sampleSize, hitRate vs
naive, meanEdge, Brier/logLoss, status, method); `components/oa/ReliabilityDiagram.tsx`
(**кастомный SVG** — диагональ + точки по бинам с counts; НЕ lightweight-charts, у него
ось X временная — подтв. context7); `components/oa/OaSignalBadge.tsx` (в выводе анализа:
active → «calibrated p=Z%, enter/skip, точность X%»; NEUTRAL → «копим данные N/N_min»);
accuracy-over-time опц. на lightweight-charts `addLineSeries`.

**Acceptance:** панель рендерится из `oa-calibration`; reliability-SVG корректен;
NEUTRAL-состояние показывает прогресс; бейдж в выводе анализа.

**Verify:** `npm test` (vitest на панель/SVG/badge); ручная проверка в dev на профиле
с историей и без.

> **CHECKPOINT C** — финал: opt-in фича end-to-end, UI on/off + калибровка/accuracy.
> Прогон всех quality-gates (core jest/build, constructor pytest/ruff/mypy,
> front vitest/lint). Ревью + решение о rollout (shadow→advisory-on).

---

## 3. Глобальные критерии приёмки (из спеки §12)
AC1 включение в UI → участие; AC2 NEUTRAL при n<N_min, но прогноз логируется (D7);
AC3 калибровка на held-out не хуже сырых на OOS; AC4 shadow-исходы в accuracy;
AC5 OA не в backtest; AC6 любой флаг OFF → полностью выкл; AC7 UI тумблер+панель;
AC8 quality-gates всех 3 сервисов зелёные.

## 4. Риски / открытые вопросы (нести в реализацию)
- OQ3: `oa_signal_log` — субдок в `ai_decision_events` (предпочтение) vs отдельная
  коллекция. **План исходит из субдока** (S1).
- OQ4: `horizonHours` единый 72ч vs per-profile. **План: единый 72ч в v1.**
- OQ6: дефолты `N_min=20`, `pThreshold=0.55` — уточнить на данных.
- Разреженность per-user (D3): много профилей останутся в NEUTRAL — это ожидаемо
  (D7 обеспечивает накопление). Global-prior — отдельный будущий трек (Ask-first).
- Двойной флаг (core `oaEnabled` ↔ constructor `oa_enabled`) — следить за
  консистентностью через http_provider.

## 5. Порядок исполнения
S1 → S2 → S3 → **CHECKPOINT A** → S4 → S5 → **CHECKPOINT B** → S7 → S8/S9 →
**CHECKPOINT C**. S6 — в любой момент после S1 (боковая ветка, опционально).
Коммиты — без Co-Authored-By (memory `no-claude-commit-attribution`).

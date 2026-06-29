# OA — TODO (чек-лист)

План: [oa-plan.md](oa-plan.md) · Спека: [../OUTCOME_AGENT_SPEC.md](../OUTCOME_AGENT_SPEC.md)
Дефолт фичи — OFF на всех этапах. Коммиты без Co-Authored-By.

## S1 — Walking skeleton (NEUTRAL, zero-influence) · core+constructor ✅ DONE
Коммиты: core `18f70ca` (S1a), core `f9614d5` (S1b override-канал), constructor `b68baca` (S1b).
- [x] core: `mastra/agents/outcome-aware-agent.ts` (копия RC-агента)
- [x] core: `mastra/tools/outcome-aware.ts` — заглушка → NEUTRAL
- [x] core: регистрация в `mastra.module.ts` + `mastra.service.ts` (`getOutcomeAwareSignal()`)
- [x] core: `ai-config.types.ts` — `oa_enabled`/`oa_model`, map `OA→outcomeAware`, НЕ в дефолтных enabled
- [x] core: `ai-config-schema.ts` + `ai-config-seed.ts` (`oaEnabled:false`)
- [x] core: `chat/agents.config.ts` — `AgentKey += outcomeAware`
- [x] core: `analysis.service.ts` — task в `gatherResearchData`, влияние=0; double-gate + backtest-skip
- [x] core: `schemas/ai-decision-event.schema.ts` — субдок `outcomeAware` (D7-лог)
- [x] core: env `OA_FEATURE_ENABLED` (default false) + чтение в pipeline
- [x] **core (обнаружено в ходе S1):** dedicated `oaEnabledOverride` канал (DTO→job→run→gate),
      т.к. constructor шлёт legacy /agents-путь где `resolvedAiConfig=null` → oaEnabled из ai_config не применяется. Зеркало `debateOverride`.
- [x] constructor: `models/personal_analysis_profile.py` +`oa_enabled` + Alembic `20260628_0045` (up/down verified :55432)
- [x] constructor: `schemas/personal_analysis.py` (4 класса) + `service.py` create/update/_build_payload forward
- [x] Тест: core override-канал (2 теста); constructor forward/omit/manual-override (3 теста); OA-tool NEUTRAL; config-wiring (6)
- [x] Verify: core build clean + 150 passed (1 known binance); constructor 20 passed, ruff/mypy clean on touched

> ⚠️ Зависимость для S5: чтобы OA реально влиял, нужен `OA_FEATURE_ENABLED=true` в core env (глобальный kill-switch, дефолт false).

## S2 — constructor: shadow-исходы + outcomes API ✅ DONE (commit `1824e73`)
- [x] `models/oa_shadow_outcome.py` + Alembic `20260629_0046` (uq history_id; up/down verified :55432)
- [x] `services/oa_shadow.py` — idempotent `record_candidate`; pure `compute_realized_move_pct`
      (backward `Series.asof`, hard-cut at horizon → no look-ahead); `backfill_due`
- [x] `worker/tasks.py` — Taskiq `backfill_oa_shadow_outcomes` every 15m
- [x] `internal_outcomes.py` — `entered` flag + `include_shadow` param (auth. excludes entered)
- [x] хук в personal-analysis completion (best-effort, не ломает персист)
- [x] Тест: 12 (as-of/no-leak, idempotent, backfill, endpoint entered/include_shadow/exclude-entered)
- [x] Verify: full suite 1142 passed (2 pre-existing strategy-health fails, unrelated); ruff/mypy clean

> Note: design — shadow row written for EVERY forecast; endpoint filters to genuinely-skipped
> by excluding history_ids that became positions (decoupled from S5's enter/skip decision).

## S3 — core: OA accuracy per profile ✅ DONE (constructor `1389c44`, core `c10dd92`)
- [x] constructor `/agent-outcomes`: +`user_id`/`profile_id`/`predicted_direction` (executed: side→dir; shadow: explicit)
- [x] core `OaAccuracyService` + `oa_accuracy_metrics` (uq user+profile+symbol+window); include_shadow=true
- [x] агрегат per (user, profile, symbol); join прогноз↔исход выполнен на стороне constructor (rows pre-joined)
- [x] wired в 6h `agent-accuracy-recompute` cron (под тем же Mongo-локом)
- [x] Тест: per-profile группировка; shadow/NEUTRAL учтён (D7); пустая история → no crash; 4 core + 1 endpoint
- [x] Verify: core build clean + 154 passed (1 known binance); constructor 8 endpoint tests; ruff/mypy clean

### ▣ CHECKPOINT A — ✅ ДОСТИГНУТ. data-loop замкнут (всё ещё NEUTRAL/zero-influence):
предсказания (вкл. NEUTRAL) логируются (S1) → исходы executed+shadow втягиваются (S2) →
accuracy per-profile считается (S3). **Ревью заказчика перед S4 (калибровка).**

## S4 — core: OaCalibrationService ✅ DONE (constructor `51563bd`, core `f284e62`)
- [x] constructor `/agent-outcomes`: +`predicted_conf` (executed: entry_confidence_pct/100; shadow: predicted_conf)
- [x] `oa-calibration.math.ts` — pure: logit/sigmoid, temperature (1-D search), Platt (Newton/IRLS), brier/logLoss/ece/reliabilityBins; **no isotonic**, no ML lib
- [x] `oa-calibration.service.ts` — fetch→group→time-ordered held-out split→fit/eval→store
- [x] `schemas/oa-calibration.schema.ts` (method, params, holdoutBrier(+Raw), holdoutLogLoss(+Raw), ece, reliabilityBins, status)
- [x] min-sample gate: `n<OA_CALIBRATION_MIN_SAMPLES`(20) → method=none/NEUTRAL; method by size (temp [20,100), platt ≥100)
- [x] wired в 6h recompute cron (после accuracy)
- [x] Тест: 11 (held-out no-worsening Brier/logloss, gate→NEUTRAL, time-ordered split 14/6, no order-leak, method selection, empty no-crash)
- [x] Verify: core build clean + 165 passed (1 known binance); constructor 7 endpoint tests; ruff/mypy clean

## S5 — core: decision + advisory + backtest-skip ✅ DONE (constructor `c60088a`, core `235e1ea`)
- [x] `oa-decision.ts` (pure) — calibratedP via profile calibrator → гейт `≥pThreshold`(0.55) → enter/skip/neutral; gate→NEUTRAL; blend confidence by weight; weak-skip → soft-veto bias=NEUTRAL
- [x] `OaDecisionService` — резолвит (userId,profileId,symbol) calibrator; profile-blind→NEUTRAL
- [x] influence — **post-analysis** (confidence форкаста есть только после analyst), в `executeJob`; мягко двигает `analysisStructured.confidence/bias`; **aiTrend/trendExtraction не трогаются**
- [x] thread user_id/profile_id constructor→core (DTO/job); double-gate (oaEnabled||override AND OA_FEATURE_ENABLED); backtest-skip
- [x] дефолты `N_min=20` (S4), `pThreshold=0.55`, influence weight 0.5 (env OA_INFLUENCE_WEIGHT)
- [x] D7: OA-сигнал логируется в decision-event subdoc + resultJson даже в NEUTRAL
- [x] Тест: 15 (decision math enter/skip/veto/gate/zero-weight; service lookup; integration: enter blends confidence, NEUTRAL logs zero-influence, flag-off/backtest/no-opt-in → no OA call)
- [x] Verify: core build clean + 179 passed (1 known binance); constructor 1143 passed (2 pre-existing strategy-health, unrelated); ruff/mypy clean

### ▣ CHECKPOINT B — ✅ ДОСТИГНУТ. Агент advisory-on для opt-in профилей:
калиброванная вероятность мягко двигает confidence/bias по весу; NEUTRAL до набора данных; backtest-skip.
**Включается только при `OA_FEATURE_ENABLED=true` + per-profile `oa_enabled`. Ревью перед фронтом (S7-S9).**

## S6 — core: эпизодическая память + retrieval ✅ DONE (core `f7f4cd3`)
- [x] **структурированный recall на Mongo** (НЕ Mastra semantic recall — сверено через context7:
      message-recall не даёт time-decay/repetition-gated lessons; эпизоды структурированы)
- [x] `oa_episodes` коллекция; sync из `/agent-outcomes?include_shadow=true` (executed+shadow, не цензурировано)
- [x] `oa-memory.ts` (pure): time-decay, relevance score (direction/regime/confidence proximity), rankEpisodes topK
- [x] `summarizeLessons` — «урок» только при ≥`OA_MEMORY_MIN_REPETITIONS`(3) повторениях (anti-Reflexion noise)
- [x] `OaMemoryService` — opt-in `OA_MEMORY_ENABLED`; recall возвращает episodes+lessons+hitRate; disabled/profile-blind→empty; в 6h cron
- [x] regime-tag nullable пока (recall деградирует мягко; market-state tagging — позже)
- [x] Тест: 10 (time-decay, ranking, regime match, lesson gate single-vs-repeated, disabled/empty)
- [x] Verify: core build clean + 189 passed (1 known binance)
> Примечание: recall построен и протестирован; **врезка в промпт OA-агента** (few-shot контекст) — когда reasoning-агент будет это потреблять (отдельно).

## S7 — constructor: oa-calibration API + openapi ✅ DONE (core `7ff1454`, constructor `ee20685`)
- [x] core `OaController`: `GET /api/v1/oa-calibration?user_id&profile_id&symbol` (ApiKeyGuard) → {calibration, accuracy[]}
- [x] constructor `GET /personal/profiles/{id}/oa-calibration` — резолвит user-scoped профиль → проксирует core → typed `OaCalibrationResponse`; 404/502
- [x] `OaProxyClient` (X-API-Key client к core); `get_profile()` helper; typed schemas (extra core fields ignored)
- [x] `oa_enabled` в Read/Update/Create/ManualTrigger схемах (было в S1b) — теперь и в openapi
- [x] регенерация `openapi.json` (+322 строк: endpoint + 4 OA-схемы + oa_enabled поля)
- [x] Тест: 3 core (controller) + 2 constructor (typed view+extra-strip, 404)
- [x] Verify: core 192 passed (1 known binance); constructor 1146 passed (2 pre-existing); ruff/mypy clean

## S8 — front: тумблер on/off (default OFF) ✅ DONE (front `1a30057`)
- [x] синк `openapi.json` из бэкенда + `npm run gen:api-types` (Node v20.20.2); `oa_enabled` в ручных Create/Update/Read типах
- [x] тумблер в форме профиля (нативный checkbox, как `debate_enabled`, НЕ shadcn Switch — так в репо) → payload `oa_enabled`; default OFF
- [x] disabled + `title`-tooltip при `NEXT_PUBLIC_OA_ENABLED != "true"` (Tooltip-компонента нет; нативный title). zustand не понадобился — форма уже на локальном state
- [x] рефактор: экспортируемые pure `toPersonalProfileForm` + `buildPersonalProfilePayload` для юнит-тестов без рендера дашборда
- [x] Verify: tsc clean, lint clean, 125 тестов (incl. 2 новых OA wiring)
> env: добавлен `NEXT_PUBLIC_OA_ENABLED` (.env.example, default false). Это зеркало core `OA_FEATURE_ENABLED` — гейтит только интерактивность тумблера.

## S9 — front: панель калибровки + reliability SVG + бейдж ✅ DONE (front `68f020f`)
- [x] `components/oa/oa-calibration-panel.tsx` — self-fetching Card: NEUTRAL «collecting data» vs active-метрики (method, samples train/holdout, ECE, Brier/logloss cal-vs-raw, per-window hit/edge) + reliability-диаграмма
- [x] `components/oa/reliability-diagram.tsx` — **кастомный SVG** (диагональ + точки по бинам, размер ~count), НЕ lightweight-charts
- [x] `components/oa/oa-signal-badge.tsx` — `OaSignalBadge` (статус калибратора/hit-rate) + `OaDecisionBadge` (per-forecast enter/skip/neutral + calibrated p из result.outcomeAware)
- [x] врезка: панель в форму профиля (после OA-тумблера); decision-бейдж в вывод анализа; `extractPersonalAnalysisPayload` отдаёт `outcomeAware`
- [x] `getProfileOaCalibration` сервис + OA-типы re-export из openapi
- [x] Verify: tsc clean, lint clean, 137 тестов (incl. 13 новых OA-компонент-тестов)
> accuracy-over-time на lightweight-charts (опц.) — не делал; reliability-SVG покрывает acceptance.

### ▣ CHECKPOINT C — ✅ ДОСТИГНУТ. Полная фича S1–S9 готова. Все quality-gates зелёные.
**Включение:** `OA_FEATURE_ENABLED=true` (core) + `NEXT_PUBLIC_OA_ENABLED=true` (front) + per-profile `oa_enabled`.
**Решение о rollout** (shadow→advisory-on) — за заказчиком.

## Финальная приёмка (AC1–AC8 из спеки §12)
- [ ] AC1 включение в UI → участие · [ ] AC2 NEUTRAL+лог (D7) · [ ] AC3 калибровка OOS
- [ ] AC4 shadow в accuracy · [ ] AC5 OA не в backtest · [ ] AC6 флаги OFF → выкл
- [ ] AC7 UI тумблер+панель · [ ] AC8 quality-gates всех 3 сервисов

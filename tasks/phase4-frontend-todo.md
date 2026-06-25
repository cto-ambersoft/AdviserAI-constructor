# Phase 4 Frontend — TODO (Promotion UI + Anomaly UI)

> Полный план: [phase4-frontend-plan.md](phase4-frontend-plan.md). Бэкенд Phase 4 — готов/закоммичен.
> Сервис: `constructor-front`. Верификация: `npm run build` (tsc) + lint + ручной смоук (фронт-тестов нет).
> Легенда: `[ ]` todo · `[~]` в работе · `[x]` готово · 🔴 must · 🟡 опц.

## 0. 🔴 БЛОКЕР — контракт фронта устарел
Нет типов Phase 4 в `lib/api/openapi-types.ts`. FE-0 обязателен до FE-2/FE-3.

## FE-0 ✅ Реген API-контракта (фундамент) — ГОТОВО (commit `4869204`)
- [x] Бэк `app.openapi()`→`openapi.json` + cp во фронт + `gen:api-types` (Node 20.20.2). ✅
- [x] Проверено: `PromotionStatusRead`/`PromotionGateCriterionRead`/`lifecycle_stage`/`promote_*`/`anomaly_*`/paths — в `openapi-types.ts` (11 совпадений). ✅
- [x] Реконсайл data-layer (чтобы tsc был зелёный): 7 полей в `AutoTradeRiskFormState`/`DEFAULT_RISK_CONFIG`/`toRiskForm`/`buildRiskConfigPayload`. UI-секции — FE-3. ✅
- [x] `tsc --noEmit` + `next build` зелёные. ✅

### 🔲 CHECKPOINT 1 ✅ — контракт обновлён, build зелёный, поведение не изменено

## FE-1 ✅ SSE-события promotion/anomaly — ГОТОВО (commit `05e78d0`)
- [x] `RISK_EVENT_TYPES` += 5 типов. ✅
- [x] `RISK_EVENT_TITLES` + severity (gate_failed/anomaly→WARNING; ready/promoted/demoted→INFO). ✅
- [x] `REFETCH_EVENT_TYPES` += `strategy_promoted`,`strategy_demoted`. ✅
- [x] tsc + next build зелёные. (SSE-смоук — на демо при ручном QA.) ✅

## FE-2 ✅ Lifecycle UI / D4 — ГОТОВО (commits `2d98896` бэк + `d91c418` фронт)
- [x] Бэк-добор: `lifecycle_stage` в `StrategyPortfolioEntry(Read)` → бэйдж без доп-запроса. Регресс 42. ✅
- [x] Сервисы `promoteStrategy`/`demoteStrategy`/`getPromotionStatus`. ✅
- [x] Step-up `GATED_PATHS` — **pattern-match** для `{config_id}` promote/demote (точечный матч не сработал бы). ✅
- [x] Бэдж `lifecycle_stage` (цвет по стадии: live/sandbox/validation/…). ✅
- [x] Gate-status drill-down: критерии (name/actual/threshold/passed) + sandbox_days + ready/not-ready. ✅
- [x] Кнопки Promote (sandbox→live, disabled если gate известно-провален, step-up) / Demote (live→sandbox); busy через `pendingByConfig`. ✅
- [x] Ошибки: 422 gate-fail → тост + авто-раскрытие Gate-панели со свежими критериями; refetch после успеха. ✅
- [x] tsc + eslint + next build зелёные. (Ручной флоу на демо — при QA.) ✅

### 🔲 CHECKPOINT 2 ✅ — промоушен виден/управляем + события тостятся (билд зелёный; демо на QA)

## FE-3 ✅ Risk Config — Anomaly + Promotion Gate — ГОТОВО (commit `dadb3f1`)
- [x] `AutoTradeRiskFormState`/`toRiskForm`/`buildRiskConfigPayload` += 7 полей — **сделано в FE-0** (чтобы tsc был зелёный). ✅
- [x] Секция **Anomaly Detection** (enable + z-threshold ≤20 + window 2–1000). ✅
- [x] Секция **Promotion Gate** (min-win-rate / max-dd / min-trades / min-sandbox-days; без toggle; NULL⇒дефолт). Границы под бэк-CHECK. ✅
- [x] tsc + eslint + next build зелёные. (Round-trip GET↔PUT на демо — при QA.) ✅
- [ ] *(опц., рекоменд.)* smoke-тест мапперов `toRiskForm`/`buildRiskConfigPayload` — нужен фронт-раннер (vitest), сейчас гейт = tsc.

## FE-4 ✅ Anomaly feed — ГОТОВО (commit `bafd8ba`)
- [x] Лента последних `strategy_anomaly_detected` на Live Monitor (severity + метрики + время; из буфера store; рендерит null пока нет аномалий). tsc + build зелёные. ✅

## Review fixes (Phase 4 frontend review) — ✅ ВСЕ СДЕЛАНЫ
- [x] **I1** (bug): promotion gate-status больше не кэшится бессрочно — рефетч на каждом раскрытии + инвалидация на demote (`e2aab3d`). ✅
- [x] **S4**: фронт-раннер vitest + jsdom + testing-library; 19 unit-тестов (matcher, мапперы, findingMetrics, gate-panel, sandboxConfigIds) (`e3d2f5b`…`7664311`). ✅
- [x] **S2**: `findingMetrics` экспортирован+тестирован, anomaly-список в `useMemo` (`50a3478`). ✅
- [x] **S1**: вынесены `PromotionGatePanel` + `LifecycleStageBadge`→kpi-format (`e13fd45`). ✅
- [x] **S3**: префетч gate-status для sandbox (keyed на membership) + Promote не блокируется на возможно-устаревшем not-ready (`7664311`). ✅

### 🔲 CHECKPOINT 3 — финальный QA: build зелёный, прогон всех 3 направлений, скриншоты, acceptance

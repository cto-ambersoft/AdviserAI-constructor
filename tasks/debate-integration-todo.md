# TODO — Debate Integration

План: [debate-integration-plan.md](debate-integration-plan.md) · Спека: [../DEBATE_INTEGRATION_SPEC.md](../DEBATE_INTEGRATION_SPEC.md)

## Срез 1 — core prereq (Вариант A, debate отдельным каналом)  [репо: core]
- [x] **C1.1** Опциональный объект `debate` в `CreatePersonalAnalysisJobRequestSchema` (Zod; topology enum; кламп раундов; диапазон timeout)
- [x] **C1.2** `ResolvedDebateConfig` + `resolveDebateConfig(aiConfig, override)` (дефолты ← aiConfig.debate_* ← override)
- [x] **C1.3** Проводка `debateOverride`: normalizeCreateRequest (обе ветки) → job-схема → executeJob → runPersonalAnalysis → generateStructuredAnalysis (resolvedAiConfig НЕ трогаем)
- [x] **C1.4** `generateStructuredAnalysis`: gate по `debateConfig.enabled`, передать `debateConfig` в DebateService
- [x] **C1.5** Рефактор `DebateService.runDebate` под явный `debateConfig` + обновить debate.service.spec / analysis.service.spec
  - [x] **тест-инвариант**: override НЕ меняет выбор агентов/веса на legacy-пути (resolvedAiConfig остаётся null)
  - [x] тест: без `debate` ⇒ идентично; `ai_config.debate_*` всё ещё работает (resolveDebateConfig); override > ai_config
  - [x] тест: невалидная topology / timeout вне диапазона ⇒ safeParse fail; раунд > cap ⇒ кламп (resolver)
- [x] **CHECKPOINT A** — core build/jest зелёные (140 passed, 1 pre-existing binance flake; lint чистый)

## Срез 2 — constructor  [репо: constructor]
- [x] **C2.1** Alembic-миграция `20260626_0044`: `personal_analysis_profiles.debate_enabled BOOLEAN NULL` (up/down) — проверено на throwaway PG :55432
- [x] **C2.2** Модель `PersonalAnalysisProfile.debate_enabled` + wiring create/update
- [x] **C2.3** Схемы: `debate_enabled` в ProfileCreate/Update/Read + ManualTriggerRequest
- [x] **C2.4** `_build_payload_for_profile` шлёт `debate={"enabled":true}` при включённом флаге (override > профиль; только True)
- [x] **C2.5** тесты: payload-проброс (3) + pass-through summary в normalize (2) + backward-compat (None→off)
- [x] **C2.6** Перегенерирован `constructor/openapi.json` (0 потерь описаний; +4 поля debate_enabled)
- [x] **CHECKPOINT B** — pytest 1130 passed (2 pre-existing auto-trade health-флака, в изоляции тоже падают); ruff/mypy чисто на моих файлах; миграция up/down на throwaway PG; openapi обновлён

## Срез 3 — constructor-front  [репо: constructor-front]
- [x] **C3.1** Типы: `debate_enabled` добавлен в рукописные `PersonalAnalysisProfile*` в `lib/api/types.ts` (эти типы hand-maintained, не из openapi → regen не нужен; полный regen тянул бы unrelated backtest cost-model drift, ломающий build — вне scope)
- [x] **C3.2** Тумблер «Enable decision debate» на профиле (`PersonalProfileFormState` + мапперы + payload `debate_enabled`)
- [x] **C3.3** `summarizeDebate()` + показ summary (winner/topology/раунды/termination/Δconf/action-changed) в панели Personal forecast
- [x] **CHECKPOINT C** — vitest 123 passed (+5 debate); `tsc --noEmit` чисто; eslint чисто. (`next build` блокируется только офлайн-fetch'ем Google-шрифтов в песочнице — не связано)

## Пост-ревью фиксы
- [x] **I1** Ресинк `constructor-front/openapi.json` с бэкендом + `DEFAULT_BACKTEST_COSTS` во все 7 backtest-сайтов (cost-поля стали required из-за `default-non-nullable` openapi-typescript). Штатный `gen:api-types` снова даёт зелёный `next build`. tsc/lint/vitest(123) чисто. (front e247f43)

## Вне scope
- app (без изменений) · полный редактор `debate_*` · drill-down транскрипта (`debate_records` read-эндпоинт) · отображение в cron/Analysis-API путях

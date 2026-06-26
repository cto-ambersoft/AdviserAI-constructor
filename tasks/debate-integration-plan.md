# PLAN — Debate Integration

> Спека: [../DEBATE_INTEGRATION_SPEC.md](../DEBATE_INTEGRATION_SPEC.md) (APPROVED)
> Метод: вертикальные срезы по сервисам, TDD. Чекпоинты между репозиториями.

## Граф зависимостей (жёсткий порядок между репо)

```
[Срез 1: core prereq] — debate-override в personal-analysis request + overlay
        │  (меняет контракт core; разблокирует constructor)
        ▼
======================= CHECKPOINT A (core зелёный) =======================
        │
        ▼
[Срез 2: constructor] — миграция + модель + схемы + payload + openapi.json regen
        │  (обновлённый openapi.json разблокирует фронт)
        ▼
=================== CHECKPOINT B (constructor зелёный, openapi обновлён) ===================
        │
        ▼
[Срез 3: constructor-front] — gen:api-types → тумблер на профиле + показ summary
        ▼
======================= CHECKPOINT C (готово к ревью/мержу) =======================

[app] — НЕ участвует.
```

## Срез 1 — core prereq (Вариант A, debate отдельным каналом)
Репо: `core`. Прогон: Node ≥22.
> Дизайн: НЕ overlay на ResolvedAiConfig (сломал бы выбор агентов на legacy-пути,
> [analysis.service.ts:509](../core/src/analysis/analysis.service.ts:509)). Debate — независимый канал.
- **C1.1** Опциональный объект `debate` в `CreatePersonalAnalysisJobRequestSchema` (Zod; topology enum; кламп раундов; диапазон timeout; не задевает legacy↔ai_config `superRefine`).
- **C1.2** `ResolvedDebateConfig` + `resolveDebateConfig(aiConfig, override)` (дефолты ← `aiConfig.debate_*` ← `override`).
- **C1.3** Проводка `debateOverride`: `normalizeCreateRequest` (обе ветки) → job-схема (`debateOverride`) → `executeJob` → `runPersonalAnalysis({debateOverride})` → `generateStructuredAnalysis`. `resolvedAiConfig` НЕ трогаем.
- **C1.4** `generateStructuredAnalysis`: `debateConfig = resolveDebateConfig(...)`, gate по `debateConfig.enabled`, передать в `DebateService`.
- **C1.5** Рефактор `DebateService.runDebate`: явный `debateConfig` (gate+параметры); `resolvedAiConfig` только для requestContext (модели) + aiConfigId. Обновить `debate.service.spec` + hook-тесты `analysis.service.spec`.
- Тесты:
  - `debate.enabled=true` на legacy-входе ⇒ debate выполняется, summary в результате;
  - **инвариант**: debate-override НЕ меняет `selectedAgents`/`agentWeights` на legacy-пути;
  - без `debate` ⇒ идентично текущему; debate через `ai_config.debate_*` всё ещё работает;
  - невалидная topology ⇒ 400; раунд > cap ⇒ кламп; override побеждает `ai_config.debate_*`.
- Verification: `npm run build`, `npx jest` (1 pre-existing binance flake — не блокер).
- **CHECKPOINT A.**

## Срез 2 — constructor
Репо: `constructor`. Прогон: `uv run pytest` / `ruff` / `mypy`; миграция через throwaway PG :55432.
- **C2.1** Alembic-миграция: `personal_analysis_profile.debate_enabled BOOLEAN NULL` (up/down).
- **C2.2** Модель `PersonalAnalysisProfile.debate_enabled`.
- **C2.3** Схемы: `debate_enabled` в `ProfileCreate/Update/Read` + `PersonalAnalysisManualTriggerRequest`.
- **C2.4** `_build_payload_for_profile`: при включённом флаге (профиль или override) добавить `payload["debate"] = {"enabled": True}`.
- **C2.5** Характеризационный тест pass-through `debate` summary в `/personal/history` и `/personal/latest`; тест payload-проброса; backward-compat (старый профиль → off).
- **C2.6** Перегенерировать `openapi.json`.
- Verification: `uv run pytest`; `alembic upgrade head` + `downgrade` на throwaway PG; ruff/mypy.
- **CHECKPOINT B.**

## Срез 3 — constructor-front
Репо: `constructor-front`. Прогон: Node ≥18.
- **C3.1** `npm run gen:api-types` (из обновлённого `constructor/openapi.json`).
- **C3.2** Профиль: `PersonalProfileFormState += debateEnabled`; тумблер «Debate» в форме; проброс в create/update payload (`components/trading/trading-dashboard.tsx`).
- **C3.3** Показ summary в панели результата анализа (winner, rounds/riskRounds, actionChanged, terminationReason, Δconfidence) через `extractPersonalAnalysisPayload`.
- Verification: `npm run gen:api-types`, `npm run build`, `npm run lint`.
- **CHECKPOINT C.**

## Риски / заметки
- **Точка overlay в core (C1.2)** — нужно найти, где legacy-запрос превращается в `ResolvedAiConfig` для запуска; оверлей делать там же. Уточнить по `personal-analysis.service.ts` / `analysis-runner` при реализации.
- Миграцию проверять локально на throwaway PG (:55432), т.к. dev PG cred-mismatch (память проекта).
- Фронт-UI полностью блокируется CHECKPOINT B (типы из openapi.json). До этого — опц. hand-written типы в `lib/api/types.ts`.
- `app` не трогаем; если всплывёт запрос показать debate в app — отдельная задача (новый core-эндпоинт или проброс в trading-модуль).

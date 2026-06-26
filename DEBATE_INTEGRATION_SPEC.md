# SPEC — Debate Integration (core → constructor → constructor-front)

> Статус: **APPROVED** (решения подтверждены 2026-06-26)
> Сервисы: `core` (prereq), `constructor`, `constructor-front`. `app` — вне scope.
> Связано с: `core/DEBATE_SPEC.md` (сам debate-слой, уже реализован).

---

## 0. Решения (зафиксированы пользователем)

1. **Включение дебатов — Вариант A**: маленькое изменение в `core` — `CreatePersonalAnalysisJobRequest` принимает опциональный объект `debate`, который **оверлеится** на разрешённый AI-config перед запуском анализа. `constructor` шлёт `debate.enabled` в этот объект. Legacy-контракт constructor (`agents`/`agent_weights`) не трогаем.
2. **Глубина UI — тумблер + summary**: фронт даёт ON/OFF переключатель дебатов на профиле персонального анализа и **показывает** debate-summary в результате. Полный редактор параметров и drill-down транскрипта — вне scope.
3. **Хранение конфига — на профиле**: `personal_analysis_profile` получает nullable-колонку `debate_enabled` (Alembic-миграция). Остальные `debate_*` параметры берутся из дефолтов core (topology=`directional_risk`, раунды 3/2, таймаут 120с, adaptive, persist).

---

## 1. Objective

Дать трейдеру включать опциональные дебаты для своих персональных прогнозов и видеть их исход, не меняя поведения по умолчанию (дебаты выключены) и не ломая существующий контракт.

### Что уже работает без изменений (проверено по коду)
- `constructor` пробрасывает и хранит `analysisStructured.debate` as-is: `normalize_analysis_payload` — shallow-copy без отбрасывания ключей ([analysis_normalization.py:77](app/core/analysis_normalization.py:77)); `result_json` — сырой dict; `analysis_data` — JSON-колонка; Pydantic-поле `dict[str, Any]`.
- Auto-trade AI-overlay потребляет уже **уточнённое** решение (`bias/confidence/action`) из сырого dict → менять не нужно.

### Out of scope
- `app` — не касается analysis-контура core (чат без дебатов; admin → trading-модуль :3002). Изменений нет.
- Полный редактор `debate_*` (топология/раунды/таймаут) в UI.
- Drill-down полного транскрипта (`debate_records`) — потребовал бы нового read-эндпоинта в core.
- Дебаты в cron/Analysis-API пути отображать в constructor-front — вне текущей задачи (UI только для personal-analysis профилей).

---

## 2. Дизайн

### 2.1 core (prereq, Вариант A) — debate-конфиг ОТДЕЛЬНЫМ каналом

**Почему не «overlay на ResolvedAiConfig»:** на legacy-пути (путь constructor) `normalizeCreateRequest` возвращает `resolvedAiConfig: null` ([personal-analysis.service.ts:359](../core/src/analysis/personal-analysis.service.ts:359)), а `runPersonalAnalysis` выбирает агентов так: `selectedAgents = resolvedAiConfig ? resolvedAiConfig.legacyAgentSelection : normalizeSelectedAgents(options.agents)` ([analysis.service.ts:509](../core/src/analysis/analysis.service.ts:509)). Синтез непустого `resolvedAiConfig` ради `debateEnabled` **переключил бы** выбор агентов и веса с пользовательских на конфиговые → регрессия. Поэтому debate проводим независимым каналом.

- **Request DTO** `CreatePersonalAnalysisJobRequestSchema` (`core/src/analysis/dto/personal-analysis.dto.ts`) — добавить опциональное поле (все поля optional; **не** legacy и **не** ai_config → `superRefine` exclusivity не задет):
  ```
  debate?: { enabled?, topology?: "directional"|"directional_risk",
             max_rounds_directional?, max_rounds_risk?, timeout_ms?,
             adaptive_stop?, persist_transcript? }
  ```
- **`ResolvedDebateConfig` + `resolveDebateConfig(aiConfig, override)`** (новый, в `debate/` или `ai-config`): `{enabled, topology, maxRoundsDirectional, maxRoundsRisk, timeoutMs, adaptiveStop, persistTranscript}`. Приоритет: дефолты ← `aiConfig.debate_*` (если `aiConfig` есть) ← `override` (побеждает). Клампы раундов к `DEBATE_ROUND_HARD_CAP`, диапазон timeout, enum topology (иначе 400 на этапе DTO).
- **Проводка `debateOverride`**: `normalizeCreateRequest` парсит `debate` в `debateOverride` **в обеих ветках**; кладётся в `NormalizedCreate` и в job-схему (новое поле `debateOverride`); `executeJob` → `runPersonalAnalysis({..., debateOverride})` → `generateStructuredAnalysis`. `resolvedAiConfig` **не трогаем** → `selectedAgents`/`agentWeights` неизменны.
- **`generateStructuredAnalysis`**: `const debateConfig = resolveDebateConfig(resolvedAiConfig, debateOverride)`; gate `if (debateConfig.enabled)`; передать `debateConfig` в `DebateService`.
- **`DebateService.runDebate`** (рефактор сигнатуры): принимает явный `debateConfig: ResolvedDebateConfig` (gate + все параметры); `resolvedAiConfig` оставляем ТОЛЬКО для `createRequestContext` (модели дебатёров → на legacy `null` → дефолтный `anModel`, как у tradingAnalysisAgent сейчас) и `aiConfigId` для записи транскрипта. Обновить `debate.service.spec` и hook-тесты в `analysis.service.spec`.
- **Инварианты:**
  - `debate` отсутствует ⇒ поведение и результат идентичны текущим;
  - debate-override **не меняет** выбор агентов/веса на legacy-пути (явный тест);
  - debate через `ai_config.debate_*` (admin/cron) продолжает работать — `resolveDebateConfig(aiConfig, null)` его читает.

### 2.2 constructor
- **Миграция (Alembic):** `personal_analysis_profile` += `debate_enabled BOOLEAN NULL` (`null`/`false` = выкл). Без backfill.
  ```python
  def upgrade(): op.add_column("personal_analysis_profile",
      sa.Column("debate_enabled", sa.Boolean(), nullable=True))
  def downgrade(): op.drop_column("personal_analysis_profile", "debate_enabled")
  ```
- **Модель:** `PersonalAnalysisProfile.debate_enabled: Mapped[bool | None]`.
- **Схемы** (`app/schemas/personal_analysis.py`): `debate_enabled: bool | None = None` в `*ProfileCreate`, `*ProfileUpdate`, `*ProfileRead`; `debate_enabled: bool | None = None` в `PersonalAnalysisManualTriggerRequest` (override на ручной триггер).
- **Payload в core** (`_build_payload_for_profile`, [service.py:408](app/services/personal_analysis/service.py:408)): если `debate_enabled` истинно (профиль или override), добавить `payload_json["debate"] = {"enabled": True}`. (Только `enabled` — остальное на дефолтах core.)
- **Summary наружу:** уже доступен внутри `analysis_data.analysisStructured.debate` (pass-through). Доп. типобезопасное поле не требуется (фронт читает из JSON). Добавить характеризационный тест, чтобы зафиксировать контракт.
- **openapi.json** — перегенерировать (разблокирует фронт).

### 2.3 constructor-front
- `npm run gen:api-types` (после обновления `constructor/openapi.json`).
- Профиль персонального анализа (`components/trading/trading-dashboard.tsx`): `PersonalProfileFormState += debateEnabled`; секция с **тумблером** «Debate» в форме профиля; прокинуть в create/update payload.
- Отображение **summary** там, где рендерится результат анализа: winner, rounds (+riskRounds), actionChanged, terminationReason, Δconfidence. Источник — `extractPersonalAnalysisPayload` → `analysis_data...debate`.

---

## 3. Acceptance criteria

1. ✅ core: запрос без `debate` ⇒ поведение и результат идентичны текущим.
2. ✅ core: `debate.enabled=true` в запросе personal-analysis ⇒ дебаты выполняются (на legacy-пути constructor тоже), `debate` summary присутствует в результате; невалидная `topology`/`timeout_ms` ⇒ 400.
3. ✅ constructor: профиль с `debate_enabled=true` ⇒ в core уходит `debate.enabled=true`; иначе поле не отправляется.
4. ✅ constructor: `debate` summary доходит до `/personal/history` и `/personal/latest` (pass-through, без потери).
5. ✅ constructor: старые профили/записи без поля читаются как «дебаты выкл», без миграции данных; миграция up/down проходит.
6. ✅ front: тумблер на профиле создаёт/обновляет `debate_enabled`; summary рендерится при наличии и отсутствует, когда дебатов не было.
7. ✅ тесты/билд/линт зелёные во всех трёх репо; `app` не менялся.

---

## 4. Boundaries

**Always:** дебаты выкл по умолчанию (opt-in на профиле); не ломать legacy-payload constructor↔core; backward-compat старых профилей/записей; pass-through summary без потерь.
**Ask first:** расширять UI до полного редактора параметров; добавлять read-эндпоинт `debate_records` и drill-down; трогать cron/Analysis-API отображение; любые изменения в `app`.
**Never:** комбинировать `debate` так, чтобы задеть legacy↔ai_config exclusivity в core; включать дебаты по умолчанию; терять `debate` при нормализации/хранении.

---

## 5. Команды/прогон

- core: Node ≥22 (`nvm v24.16.0`), `npm run build` / `npx jest`.
- constructor: `uv run alembic upgrade head` (локальная проверка миграции — спинать throwaway postgres на :55432, см. память проекта); `uv run pytest`; `uv run ruff` / `mypy`.
- constructor-front: Node ≥18 (`nvm v20.20.2`), `npm run gen:api-types`, `npm run build`, `npm run lint`.

## 6. Context7 (этап реализации)
- core: Zod (optional object schema, superRefine) — при правке DTO.
- constructor: Pydantic v2 (optional fields), SQLAlchemy/Alembic (add nullable column — паттерн подтверждён: `op.add_column(..., nullable=True)` / `op.drop_column`).
- front: `openapi-typescript` (codegen).

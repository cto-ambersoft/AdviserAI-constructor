# Phase 4 — Frontend Plan: Promotion Pipeline + Anomaly Detection UI

> **Скоуп:** UI в `constructor-front` для бэкенда Phase 4 (B5 Promotion Pipeline + B6 Anomaly Detection), который уже готов и закоммичен. Делает пайплайн видимым и демонстрируемым.
> **Связанные доки:** [phase4-plan.md](phase4-plan.md) (бэк), [phase4-todo.md](phase4-todo.md), [m4-closeout-plan.md](m4-closeout-plan.md).
> **Сервис:** `constructor-front` (Next.js 16, app router, React 19, Zustand, Sonner-тосты, **i18n нет** — строки хардкодом по существующему паттерну).
> **Режим:** план составлен read-only. Код не менялся. CLI openapi-typescript — context7-verified.

---

## 0. Цель и DoD

| Направление | DoD |
|---|---|
| **D4 Lifecycle UI** | На карточке стратегии виден `lifecycle_stage`; есть панель Gate-status (критерии pass/fail + дни в sandbox); кнопки Promote (step-up) / Demote работают |
| **Anomaly + Promotion events** | `promotion_ready`/`strategy_promoted`/`strategy_demoted`/`promotion_gate_failed`/`strategy_anomaly_detected` приходят по SSE → тост с правильной severity |
| **Risk Config UI** | Секции Anomaly Detection + Promotion Gate в форме риск-конфига; round-trip GET↔PUT |

---

## 1. 🔴 БЛОКЕР — контракт фронта устарел

**Критично:** `constructor-front/lib/api/openapi-types.ts` **не содержит** ни одного Phase 4 эндпоинта/поля. Бэк их отдаёт (`app.openapi()` подтверждено), но фронт-`openapi.json` старый. Без регена у фронта **нет типизированного доступа** к:
- эндпоинты `/promote`, `/demote`, `/promotion-status`
- `PromotionStatusRead`, `PromotionGateCriterionRead`
- `AutoTradeConfigRead.lifecycle_stage` (в типах)
- `AutoTradeRiskConfig`: `promote_*` (4) + `anomaly_*` (3) поля

→ **FE-0 (реген контракта) — обязательный первый шаг, блокирует FE-2 и FE-3.**
FE-1 (события) **не** зависит от контракта (`RISK_EVENT_TYPES` — рукописный string-union, `RiskEvent.payload` = `Record<string,unknown>`).

> ⚠️ Реген требует **Node ≥18** (дефолтный node v14 падает на `??=` — см. memory [Front OpenAPI codegen]). Команды — [m4-closeout-plan.md Appendix B](m4-closeout-plan.md).

---

## 2. Граф зависимостей

```
FE-0 Контракт (реген openapi.json + gen:api-types)   FE-1 SSE-события (независим)
        │                                                   │
        ├──────────────┬──────────────┐                     │
        ▼              ▼              ▼                     ▼
   FE-2 Lifecycle   FE-3 Risk     (типы для           (тосты promotion/
   UI (D4)          Config         FE-2/FE-3)           anomaly работают)
        │              секции
        ▼
   FE-4 Anomaly feed (опц., deps FE-1)
```

**Критический путь:** FE-0 → FE-2. FE-1 параллелится с FE-0. FE-3 после FE-0.

---

## 3. Вертикальные срезы (полный путь на задачу)

### ▶ FE-0 · Реген API-контракта 🔴 (фундамент)
**Полный путь:** бэк `app.openapi()` → `openapi.json` → cp во фронт → `npm run gen:api-types` → типы.
- **Где:** [package.json](../../constructor-front/package.json) (`gen:api-types`), [lib/api/openapi-types.ts](../../constructor-front/lib/api/openapi-types.ts).
- **Шаги:** (1) `cd constructor && uv run python -c "...json.dump(app.openapi()...)"`; (2) `cp constructor/openapi.json constructor-front/openapi.json`; (3) `export PATH=…node20…; npm run gen:api-types`.
- **Acceptance:** в `openapi-types.ts` присутствуют `PromotionStatusRead`, `PromotionGateCriterionRead`; `lifecycle_stage` в `AutoTradeConfigRead`; `promote_*`/`anomaly_*` в `AutoTradeRiskConfig`; paths `…/promote`, `…/demote`, `…/promotion-status`. `npm run build` (tsc) зелёный.
- **Verify:** `grep -E "PromotionStatusRead|promotion-status|anomaly_z_threshold|promote_min_trades" lib/api/openapi-types.ts`; `npm run build`.

> **🔲 CHECKPOINT 1 — review.** Контракт обновлён, build зелёный, **поведение не изменено** (только типы). Дальше — UI.

---

### ▶ FE-1 · SSE-события promotion/anomaly (независим)
**Полный путь:** бэк эмитит (готово) → store ингестит → тост с severity → (refetch где нужно).
- **Где:** [stores/risk-events-store.ts:7](../../constructor-front/stores/risk-events-store.ts) (`RISK_EVENT_TYPES`), [components/risk-events/risk-event-display.ts](../../constructor-front/components/risk-events/risk-event-display.ts) (`RISK_EVENT_TITLES`, severity-сеты, `REFETCH_EVENT_TYPES`).
- **Задачи:**
  - [ ] Добавить 5 типов в `RISK_EVENT_TYPES`.
  - [ ] Тайтлы в `RISK_EVENT_TITLES` (напр. «Strategy ready for promotion», «Strategy promoted to live», «Promotion gate not satisfied», «Strategy anomaly detected»).
  - [ ] Severity: `strategy_anomaly_detected`/`promotion_gate_failed` → WARNING; `promotion_ready`/`strategy_promoted`/`strategy_demoted` → INFO.
  - [ ] `REFETCH_EVENT_TYPES` += `strategy_promoted`,`strategy_demoted` (меняют стадию → перерисовать монитор).
- **Acceptance:** при приходе каждого события в открытый `EventSource` — тост нужной severity; promoted/demoted перезагружают портфель.
- **Verify:** `npm run build`; ручной SSE-смоук (эмит события на бэке/демо → тост во фронте).

---

### ▶ FE-2 · Lifecycle UI (D4) 🔴 — deps FE-0
**Полный путь:** API-сервисы → step-up gating → бэдж стадии → Gate-status → Promote/Demote.
- **Где:** [lib/api/services/live-auto-trade.ts](../../constructor-front/lib/api/services/live-auto-trade.ts), [lib/api/step-up.ts:35](../../constructor-front/lib/api/step-up.ts), [components/monitor/strategy-monitor-card.tsx](../../constructor-front/components/monitor/strategy-monitor-card.tsx), [components/monitor/live-monitor-dashboard.tsx](../../constructor-front/components/monitor/live-monitor-dashboard.tsx).
- **Задачи:**
  - [ ] Сервисы: `promoteStrategy(configId)` (POST `/strategies/{id}/promote`), `demoteStrategy(configId)`, `getPromotionStatus(configId)` (GET `/strategies/{id}/promotion-status` → `PromotionStatusRead`).
  - [ ] **Step-up gating:** добавить promote/demote в `GATED_PATHS`. ⚠️ Пути содержат `{config_id}` — текущий матчер статический; нужен матч по шаблону (regex/`includes('/promote')`), не точному равенству. Проверить логику матчинга в `step-up.ts`.
  - [ ] Бэдж `lifecycle_stage` на `StrategyMonitorCard` (цвет по стадии: live=green, sandbox=amber, validation=blue, rejected/archived=muted).
  - [ ] Gate-status drill-down (по аналогии с health-expand): таблица критериев из `getPromotionStatus` (`name`/`actual`/`threshold`/`passed`) + `sandbox_days` + `can_promote`.
  - [ ] Кнопки **Promote** (видна только для `sandbox`; `can_promote` → активна; клик → POST, step-up-модалка отрабатывает прозрачно в `apiRequest`) и **Demote** (видна для `live`). Busy-флаг через `pendingByConfig`.
  - [ ] Обработка ошибок: 422 (gate-fail) → тост с failed-критериями; 409 (wrong stage) → тост; refetch портфеля после успеха.
- **Acceptance:** карточка показывает стадию; раскрытие показывает критерии gate; Promote sandbox-стратегии с `can_promote=true` → step-up → стадия `live` + тост; Promote при невыполненном гейте → 422 с критериями; Demote live → `sandbox`.
- **Verify:** `npm run build`; ручной флоу в браузере (демо-аккаунт): sandbox→promote(step-up)→live→demote.

> **🔲 CHECKPOINT 2 — review + demo.** Промоушен виден и управляем с UI; события тостятся. Демо полного цикла sandbox→live. Калибровка строк/цветов с трейдерами.

---

### ▶ FE-3 · Risk Config — Anomaly + Promotion Gate секции 🔴 — deps FE-0
**Полный путь:** form-state → toForm/buildPayload → 2 UI-секции → round-trip GET↔PUT (PUT уже step-up gated).
- **Где:** [components/auto-trade/types.ts:49](../../constructor-front/components/auto-trade/types.ts) (`AutoTradeRiskFormState`), [components/auto-trade/utils.ts:220](../../constructor-front/components/auto-trade/utils.ts) (`toRiskForm`/`buildRiskConfigPayload`), [components/auto-trade/auto-trade-risk-section.tsx](../../constructor-front/components/auto-trade/auto-trade-risk-section.tsx).
- **Задачи:**
  - [ ] `AutoTradeRiskFormState` += `anomaly_detection_enabled`,`anomaly_z_threshold`,`anomaly_window`,`promote_min_win_rate_pct`,`promote_max_dd_pct`,`promote_min_trades`,`promote_min_sandbox_days`.
  - [ ] `toRiskForm` + `buildRiskConfigPayload` += эти 7 полей (паттерн `toNullableNumber`/passthrough).
  - [ ] Секция **Anomaly Detection** (enable + z-threshold + window) по образцу Kill-Switch.
  - [ ] Секция **Promotion Gate** (min-win-rate / max-dd / min-trades / min-sandbox-days) — без enable (всегда активен), подпись «NULL ⇒ дефолт движка».
- **Acceptance:** правка порогов → PUT (step-up) → перезагрузка показывает сохранённое; границы полей под бэк-CHECK (z 0–20, window 2–1000, wr/dd 0–100).
- **Verify:** `npm run build`; ручной round-trip; (опц.) проверка значения в `auto_trade_risk_configs`.

---

### ▶ FE-4 · Anomaly feed (опц.) — deps FE-1
- [ ] Лента/бэйдж последних `strategy_anomaly_detected` на Live Monitor (из буфера `risk-events-store`).
- **Acceptance:** аномалия-событие появляется в ленте с severity/метрикой.

> **🔲 CHECKPOINT 3 — финальный QA.** Полный `npm run build` зелёный; ручной прогон всех трёх направлений; скриншоты; acceptance-демо. Контракт-инвариант: SSE-токен только через cookie/BFF (уже так).

---

## 4. Оценка и параллелизм

| Срез | Оценка | Зависимости |
|---|---|---|
| FE-0 Контракт | ~0.5 дн | — |
| FE-1 События | ~0.5 дн | ║ независим |
| FE-2 Lifecycle UI | ~2 дн | FE-0 |
| FE-3 Risk Config | ~1 дн | FE-0 |
| FE-4 Anomaly feed | ~0.5 дн | FE-1 |

**Итого ~3.5–4.5 дн.** FE-0 и FE-1 — первыми (параллельно), затем FE-2 ∥ FE-3.

---

## 5. Верификация (нет фронт-тестов)

Во фронте **нет unit-набора** (по аудиту — defer). Поэтому верификация каждого среза:
1. `npm run build` (tsc-typecheck — главный гейт; контракт-типы ловят рассинхрон).
2. `npm run lint`.
3. Ручной смоук в браузере (или через skill `run`/`verify`/Chrome MCP): пройти полный путь среза на демо-аккаунте.
4. (Рекомендация) добавить хотя бы smoke-тест на `toRiskForm`/`buildRiskConfigPayload` round-trip (чистые функции — легко).

---

## 6. Риски и предпосылки
- **Контракт-реген (FE-0) — жёсткий блокер** FE-2/FE-3; Node ≥18 обязателен.
- **GATED_PATHS с `{config_id}`** — матчер может быть точечным; проверить и при необходимости сделать паттерн-матч (иначе step-up не сработает на promote/demote).
- **i18n нет** — строки хардкодом по существующему паттерну (`RISK_EVENT_TITLES`).
- **Демо-аккаунт** нужен для ручной проверки полного цикла (sandbox торгует на demo).
- **Калибровка** строк/цветов стадий и порогов — с трейдерами.

---

## 7. Для ревью (вопросы перед стартом)
1. Promote/Demote — на карточке монитора (рекоменд.) или отдельный экран Lifecycle?
2. Нужен ли FE-4 (anomaly feed) в M4 или достаточно тостов?
3. Цветовая схема стадий (live/sandbox/validation/…) — согласовать с дизайном?

# Phase 1 — план реализации (W10 backend, AC#4 + AC#7)

> Срез плана из [m4-closeout-plan.md](m4-closeout-plan.md) → **Phase 1**. Read-only аудит выполнен и сверен с кодом (`file:line`); taskiq-планировщик сверен через Context7.
> ⚠️ Файлы `tasks/plan.md` / `tasks/todo.md` заняты активным W9-PnL-планом — этот документ намеренно отдельный (`phase1-*`), чтобы их не затереть.
> **Чеклист задач:** [phase1-todo.md](phase1-todo.md).

---

## 1. Что входит в Phase 1

Закрыть два оставшихся **бэкенд**-разрыва, которые делают AC#4 и AC#7 полными, плюс non-code change-request.

| ID | Задача | Закрывает | Статус |
|---|---|---|---|
| **B2** | Portfolio-DD watcher — авто-пауза всех стратегий пользователя при портфельном DD | AC#4 (портфельный уровень) | 🔴 todo |
| **B4** | «Live» KPI в portfolio summary (убрать ≤5-мин снапшот / `None`-до-первого-крона) | AC#7 (live dashboard data) | 🔴 todo |
| F1 | Risk Config UI | AC#4 (config) | ✅ готово (`63b5b9a`) — вне Phase 1 |
| **CR** | Change-request: перенос W10 Promotion Pipeline + W12 anomaly в M5 | состав работ | 🟡 non-code, PM |

**Вне Phase 1:** B1 (2FA), B3 (SSE), весь фронт F2–F5 — это Phase 2–3.

---

## 2. Контекст кода (что переиспользуем)

- **Cron-паттерн** — taskiq label-schedule на `@broker.task(schedule=[{"cron": ..., "schedule_id": ...}])` ([../app/worker/tasks.py:173](../app/worker/tasks.py)). Эталон — `evaluate_kpi_guards`. Планировщик один (`TaskiqScheduler` + `LabelScheduleSource`) — инвариант «один scheduler» (Context7) уже соблюдён.
- **Bulk-пауза** — `AutoTradeService.set_running_bulk(user_id, is_running=False)` ([../app/services/auto_trade/service.py:1500](../app/services/auto_trade/service.py)): идемпотентна, пропускает уже остановленные, эмитит `bulk_stop_all_invoked`.
- **Системная пауза + событие** — `_auto_pause_strategy` ([service.py:1593](../app/services/auto_trade/service.py)); расчёт health per-config (без биржи) — `compute_strategy_health` (используется в `sweep_kpi_guards` [service.py:1784](../app/services/auto_trade/service.py)).
- **Portfolio-агрегация** — `compute_portfolio` ([../app/services/auto_trade/portfolio.py:109](../app/services/auto_trade/portfolio.py)); KPI берутся из снапшота `latest_health_snapshots_for_configs` → `None` до первого крона (portfolio.py:57-65, 246-251). `portfolio_max_dd_pct` = **worst-strategy** DD из снапшотов; «true merged-equity DD» сам код откладывает на W11 (portfolio.py:75-77).
- **Алертинг = durable outbox** — `TelegramNotificationService.dispatch_pending` ([../app/services/notifications/service.py:105](../app/services/notifications/service.py)) выбирает `auto_trade_events` с `event_type ∈ NOTIFIABLE_EVENTS` без терминальной доставки и шлёт. ⇒ **новое risk-событие автоматически уходит в Telegram**, если добавить его в `RISK_EVENTS` ([../app/services/notifications/formatting.py:30](../app/services/notifications/formatting.py)) + форматтер. Гейт — тоггл `notify_on_risk`.
- **Settings-паттерн** — поля-пороги в `class Settings` ([../app/core/config.py:6](../app/core/config.py)), напр. `agent_freshness_threshold_minutes: int = 240`.

---

## 3. Граф зависимостей

```
P1-T1 (settings) ──┬─► P1-T2 (B2 sweep-сервис) ─► P1-T3 (B2 cron+event+formatter) ─► ▣ CP-A
                   │
                   └─► P1-T4 (B4 compute_portfolio freshness) ─► P1-T5 (B4 API kpi_as_of) ─► ▣ CP-B ─► P1-T6 (regen contract)

P1-CR  (non-code, независимо)
```

B2 и B4 связаны только общим P1-T1 → после него идут **параллельно**. Вертикальные срезы: T2 — полный путь watcher→пауза→событие; T4 — полный путь freshness-fallback→ответ.

---

## 4. Ключевые решения (обозначить перед стартом)

1. **Сигнал портфельного DD (B2):** Phase 1 = **worst-strategy running max-DD**, считается request-time per running-config (`compute_strategy_health`, без биржи) — самодостаточно, без зависимости от порядка kpi_guard-крона. **True merged-equity** портфельный DD — явно на W11 Portfolio Supervisor (совпадает с комментарием в portfolio.py:75-77).
2. **Порог (B2):** глобальная env-настройка `portfolio_dd_halt_threshold_pct` + `portfolio_dd_halt_enabled` — **без миграции**. Per-user порог отложен (нужна колонка/таблица; пойдёт с W11). **Калибровать с трейдерами до включения на реальных деньгах.**
3. **Freshness (B4):** дешёвый снапшот остаётся, но при отсутствии/устаревании (> `kpi_freshness_seconds`, default 300) — **fallback на request-time `compute_strategy_health`** для этого конфига; в ответе — `kpi_as_of` per-strategy. `/strategies/{id}/health` уже live — устаревал только агрегат в summary.
4. **Миграций в Phase 1 нет** — обе задачи это settings + производные поля. Низкий риск.

---

## 5. Задачи (вертикальные срезы)

### P1-T1 [S] — Settings: пороги для B2 и B4 · deps: —
**Файлы:** [../app/core/config.py](../app/core/config.py).
- `portfolio_dd_halt_enabled: bool = False`, `portfolio_dd_halt_threshold_pct: float = ...` (off-by-default, безопасно).
- `kpi_freshness_seconds: int = 300`.
**Acceptance:** настройки читаются из env; дефолты безопасны (watcher выключен).
**Verify:** `uv run python -c "from app.core.config import get_settings; s=get_settings(); print(s.portfolio_dd_halt_enabled, s.kpi_freshness_seconds)"`.

### P1-T2 [M] — B2: сервис `sweep_portfolio_dd_guards` · deps: P1-T1
**Файлы:** [../app/services/auto_trade/service.py](../app/services/auto_trade/service.py) (или новый `auto_trade/portfolio_guard.py`).
- Выбрать distinct `user_id` с running+enabled конфигами.
- Per user: посчитать worst max-DD по running-конфигам через `compute_strategy_health` (best-effort, try/except + rollback per user — как `sweep_kpi_guards`).
- Если `enabled` и worst_dd ≥ `portfolio_dd_halt_threshold_pct` → `set_running_bulk(user_id, is_running=False)` и, если реально что-то остановлено (`succeeded>0`), эмит `portfolio_dd_halt` (user-level, `config_id=None`, как bulk-события; payload: worst_dd, threshold, paused_count, breaching_config_id).
- Вернуть `{"users": n, "halted": k, "errors": e}`.
**Acceptance:** портфель с worst-DD ≥ порога → все running-стратегии пользователя `is_running=False`; одно `portfolio_dd_halt`; идемпотентно (нет running → повторный sweep no-op).
**Verify:** юнит-тест на сервис (мок health), без биржи.

### P1-T3 [S] — B2: cron + event-type + formatter · deps: P1-T2
**Файлы:** [../app/worker/tasks.py](../app/worker/tasks.py), [../app/services/notifications/formatting.py](../app/services/notifications/formatting.py).
- Новый `@broker.task(schedule=[{"cron":"*/5 * * * *","schedule_id":"portfolio_dd_every_5m"}])` → `auto_trade_service.sweep_portfolio_dd_guards(session)`; лог только при `halted>0` (паттерн `_stats_has_non_zero`).
- `RISK_EVENTS |= {"portfolio_dd_halt"}` → автоматически `NOTIFIABLE` + гейт `notify_on_risk`.
- Ветка форматтера `format_event` для `portfolio_dd_halt` (worst_dd, threshold, paused_count).
**Acceptance:** крон зарегистрирован; событие проходит `toggle_for_event → "risk"`; форматтер даёт читаемый текст.
**Verify:** `uv run python -c "from app.services.notifications.formatting import NOTIFIABLE_EVENTS, toggle_for_event; print('portfolio_dd_halt' in NOTIFIABLE_EVENTS, toggle_for_event('portfolio_dd_halt'))"`.

### ▣ Checkpoint A — B2 end-to-end
- [ ] `uv run pytest tests/ -q` (новые B2-тесты зелёные, регрессий нет).
- [ ] Сценарий: синтетический worst-DD ≥ порога → все стратегии пользователя на паузе → одно `portfolio_dd_halt` в `auto_trade_events` → `dispatch_pending` создаёт delivery (или `skipped`, если Telegram не привязан).
- [ ] Идемпотентность: второй sweep подряд — halt=0.
- [ ] `uv run ruff check .` · `uv run mypy app` — без новых ошибок.

### P1-T4 [M] — B4: freshness-fallback в `compute_portfolio` · deps: P1-T1
**Файлы:** [../app/services/auto_trade/portfolio.py](../app/services/auto_trade/portfolio.py).
- Для каждого конфига: если снапшота нет ИЛИ `now - snapshot.created_at > kpi_freshness_seconds` → request-time `compute_strategy_health` (без биржи) и взять KPI оттуда; иначе снапшот.
- Добавить `kpi_as_of: datetime | None` в `StrategyPortfolioEntry` (время снапшота или `now` при request-time).
**Acceptance:** свежезапущенная стратегия (снапшота нет) → KPI не `None`, `kpi_as_of ≈ now`; «свежий» снапшот по-прежнему используется без перерасчёта.
**Verify:** юнит-тест `compute_portfolio(fetch_balances=False)` на конфиге без снапшота → KPI заполнены.

### P1-T5 [S] — B4: `kpi_as_of` в API · deps: P1-T4
**Файлы:** схема/эндпоинт portfolio в [../app/api/v1/endpoints/live.py](../app/api/v1/endpoints/live.py) + соответствующая Pydantic-схема.
- Прокинуть `kpi_as_of` в `PortfolioSummaryResponse`/per-strategy-итем.
**Acceptance:** `GET /live/auto-trade/portfolio` отдаёт `kpi_as_of` на каждую стратегию.
**Verify:** локальный запрос или тест эндпоинта.

### ▣ Checkpoint B — B4 end-to-end
- [ ] `uv run pytest tests/ -q`.
- [ ] Старт стратегии → сразу `GET /portfolio` → KPI непустые + `kpi_as_of ≈ now`.
- [ ] lint/typecheck чисто.

### P1-T6 [S] — Регенерация контракта · deps: P1-T5
Схема portfolio изменилась (`kpi_as_of`) → обновить контракт фронта (см. Appendix B в [m4-closeout-plan.md](m4-closeout-plan.md)). **Node ≥18.**
**Acceptance:** `kpi_as_of` присутствует в `constructor-front/lib/api/openapi-types.ts`.

### P1-CR — Change-request (non-code) · deps: —
- [ ] Письмо заказчику: согласовать перенос **W10 Strategy Promotion Pipeline** и **W12 anomaly detection** в M5. Без согласования — провал приёмки по составу работ. Owner: PM. **Отправить на этой неделе.**

---

## 6. Риски / заметки
- **Реальные деньги:** `portfolio_dd_halt_threshold_pct` — защита счёта; не включать (`portfolio_dd_halt_enabled=True`) без калибровки с трейдерами.
- **Worst-strategy DD ≠ merged-equity DD** — Phase 1 сознательно проще; настоящий портфельный DD = W11.
- **B4 стоимость:** request-time health только когда снапшот устарел/отсутствует — на «тёплом» дашборде перерасчётов нет.
- **Повторный halt** после ручного рестарта при высоком DD — ожидаемое защитное поведение, задокументировать в UI.

---

## 7. Команды проверки
```bash
cd constructor
uv run pytest tests/ -q
uv run ruff check .
uv run mypy app
# регенерация контракта (Node >=18):
uv run python -c "import json; from app.main import app; json.dump(app.openapi(), open('openapi.json','w'), indent=2, ensure_ascii=False)"
cp openapi.json ../constructor-front/openapi.json
cd ../constructor-front && export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && npm run gen:api-types
```

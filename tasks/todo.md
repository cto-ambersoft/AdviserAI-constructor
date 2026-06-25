# TODO — точность PnL / комиссий / фандинга

Источник истины: биржевой `realizedPnl` (трейды) + `/fapi/v1/income` (фандинг).
Границы: **не трогаем** execution/open/close/risk-gate. Детали → [plan.md](plan.md).

## Фаза 1 — Авторитетный realized
- [x] **T1** [S] Колонка `realized_pnl` в `exchange_trade_ledger` + миграция `_0022` + backfill из `raw_trade.info.realizedPnl` · deps: — · `models/exchange_trade_ledger.py`, `migrations/` · ✅ `ecdc9b7`
- [x] **T2** [S] Sync пишет `realized_pnl` на upsert (+ в `update_columns`) · deps: T1 · `auto_trade/trade_sync.py` · ✅ `48ef4b7`
- [x] **T3** [M] Futures-движок: `gross_realized = Σ realized_pnl` (FIFO → fallback) → `/accounts/{id}/trades` · deps: T2 · `execution/futures_pnl.py`, `execution/account_trades_service.py` · ✅ `ff560a2`

### ▣ Checkpoint A
- [x] alembic up→down→up (на чистом PG) · `pytest tests/ -q` (720 passed) · lint/typecheck — без новых ошибок (есть pre-existing debt)
- [ ] `/accounts/{id}/trades` realized = биржевой realizedPnl (демо — нужны live-ключи)
- [ ] ⚑ Ревью с человеком: согласовать контракт `compute_realized_breakdown` и формулу net (перед Фазой 2/T7-T8)

## Фаза 2 — Фандинг (только Binance, только FUNDING_FEE)
- [x] **T4** [S] Таблица/модель `exchange_income_ledger` + миграция `_0023` (курсор = `MAX(income_at)`, без state-таблицы) · deps: — · ✅ `1dab4bf`
- [x] **T5** [M] Income-sync (Binance-only, FUNDING_FEE) + адаптер `fetch_futures_income` + Taskiq `sync_auto_trade_exchange_income`; не-Binance = no-op · deps: T4 · ✅ `79539ed`
- [x] **T6** [S] Агрегатор `sum_funding(account, symbol, window)` (знаковый) · deps: T5 · ✅ `8f02160`

### ▣ Checkpoint B
- [x] миграции 0022+0023 up→down→up (на чистом PG) · `pytest -q` (727 passed) · lint/typecheck — без новых ошибок
- [ ] FUNDING_FEE строки появляются в БД на демо (нужны live-ключи) · ⚑ Ревью с человеком

## Фаза 3 — Открытые позиции + унификация
- [x] **T7** [M] OPEN-снапшот: realized от закрывающих fill'ов + funding (не `−fees`); CLOSED: ledger Σ realized_pnl + funding; fallback при отсутствии ledger · deps: T3,T6 · ✅ `534fbbe`
- [x] **T8** [M] Унификация: `calculate_futures_pnl_fifo` → тонкая обёртка над `compute_realized_breakdown`; FIFO только fallback внутри · deps: T3,T7 · ✅ `11e4c77`

### ▣ Checkpoint C
- [x] Оба движка через `compute_realized_breakdown` (тест-сверка) · `pytest -q` (730 passed) · lint/typecheck — без новых ошибок (futures_pnl mypy 2→0)
- [ ] ⚑ Ревью с человеком: подтвердить семантику — account `realized`=gross, position `realized`=net (+funding); поля разложения добавит T10

## Фаза 4 — Комиссии, отдача, мелочи
- [x] **T9** [S] Не-USDT (BNB) комиссия → quote по mark: futures `_fee_to_quote`/`compute_realized_breakdown` + `sum_fee_cost_quote` + best-effort `fetch_mark_prices` + `total_fee_usdt` · deps: — · ✅ `46c4343`
- [x] **T10** [M] Схемы: `gross_realized_usdt`/`commission_usdt`/`funding_usdt`/`net_pnl_usdt` на position+trades; `openapi.json` регенерён (untracked) · deps: T3,T6,T9 · ✅ `0d3f889`
- [x] **T11** [S] `margin_used_usdt = Σ notional/leverage` (а не `position_size_usdt`) · deps: — · ✅ `4f8db6a`
- [x] **T12** [S] Spot `average_entry_price` без комиссий · deps: — · ✅ `100a6fc`
- [x] **T14** [S] ⚠️ Daily-loss gate: `_today_realized_pnl_usdt` = net (gross−commission+funding), **pure-DB, без exchange-вызовов** + fallback · deps: T2,T6 · ✅ `9b8cfed`

### ▣ Checkpoint D — торговый гейт
- [x] `pytest -q` (733 passed) · pure-DB подтверждён (тест с «взрывающимся» адаптером — не вызывается) · lint/typecheck без новых ошибок
- [ ] ⚑ Ревью с человеком: раннее срабатывание daily-loss при учёте фандинга/комиссий — желаемо?

## Фаза 5 — Тесты
- [x] **T13** [M] Тест-матрица `test_pnl_invariants.py`: realized=Σ realizedPnl · фандинг · BNB/base-fee · SHORT (tagged+FIFO) · mixed · net=gross−comm+funding · empty · согласие движков; DB-сценарии (OPEN частичные/multi-TP/daily-loss) — в feature-тестах · deps: T3,T7,T8,T9,T10,T14 · ✅ `e8ebeb3`

### ▣ Checkpoint Final
- [x] `pytest -q` (747 passed, 1 skipped) · lint/typecheck — без новых ошибок · миграции 0022+0023 up→down→up (чистый PG) · `openapi.json` регенерён (untracked)
- [ ] Демо-сверка `/positions` `/portfolio` `/accounts/{id}/trades` против Binance UI (нужны live-ключи)
- [x] Подтверждено: diff не трогает order/open/close/`engine.py`; единственное касание гейта — числитель `_today_realized_pnl_usdt` (T14, pure-DB)
- [ ] ⚑ Финальное ревью с человеком

## Решения (закрыто, см. plan.md §9)
- ✅ `_today_realized_pnl_usdt` — полный учёт (T14), pure-DB
- ✅ Только Binance (Bybit — backlog)
- ✅ Из `/income` только FUNDING_FEE (комиссия уже в `fee_cost`, realized в `realized_pnl`)
- ✅ Атрибуция фандинга — окно позиции по (account_id, symbol)
- ⧗ Источник mark для BNB-комиссии — финализировать в T9

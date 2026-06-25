# План реализации: точность PnL / комиссий / фандинга и отдача клиенту

> Read-only аудит выполнен и **сверен с кодом** (см. ссылки `file:line`) и с
> Binance income API через Context7. Этот план описывает только **отчётные /
> расчётные / sync пути**. **Логика торговли не затрагивается** (см. раздел
> «Границы: что НЕ трогаем»).

## 1. Что делает проект (контекст)

FastAPI-сервис авто-трейдинга поверх ccxt (Binance USDⓈ-M фьючерсы и Bybit,
**реальные деньги**). Стратегия = одна строка `AutoTradeConfig`, привязанная к
своему суб-аккаунту (`ExchangeCredential`). Сигналы кладутся в очередь, Taskiq
исполняет открытие/закрытие, watcher следит за SL/TP, отдельный pre-trade
risk-engine гейтит сделки. Фактические fill'ы биржи зеркалятся в БД таблицей
`exchange_trade_ledger` каждую минуту (`sync_auto_trade_exchange_trades`).

## 2. Как сейчас устроен «поток денег» (карта, сверено с кодом)

```
Binance fill (raw содержит info.realizedPnl, fee)
        │  fetch_my_trades (ccxt)
        ▼
ExchangeTradeSyncService.sync_running_configs            ← Taskiq, раз в минуту
   app/services/auto_trade/trade_sync.py                   (worker/tasks.py:104)
        │  upsert
        ▼
exchange_trade_ledger  (ТОЛЬКО трейды: side/price/amount/cost/fee_cost/
   app/models/exchange_trade_ledger.py                     fee_currency + raw_trade JSON)
        │                                                   ✗ нет realized_pnl
        │                                                   ✗ нет фандинга
        ├──────────────► Движок A: calculate_futures_pnl_fifo
        │                  app/services/execution/futures_pnl.py
        │                  → GET /accounts/{id}/trades  (accounts.py:18)
        │                  realized = FIFO(цены) − fees;  BNB-fee → 0
        │
        └──────────────► Движок B: build_position_pnl_snapshot
                           app/services/auto_trade/service.py:2121
                           → GET /auto-trade/positions  (live.py:561)
                           → GET /auto-trade/portfolio   (live.py:703 → portfolio.py)
                           OPEN:   realized = −fees (теряет частичные закрытия)
                           CLOSED: directional(close_price) или inferred(realizedPnl-ish)

Спот (вне основного фокуса): trading_service.get_spot_pnl → calculate_spot_pnl
   app/services/execution/pnl.py  (avg_entry включает комиссии)
```

`/fapi/v1/income` (incomeType `FUNDING_FEE` / `REALIZED_PNL` / `COMMISSION`) —
авторитетный источник, подтверждён Context7; ccxt 4.5.37 отдаёт его через
`fetch_funding_history()` (→ `fapiPrivateGetIncome`, incomeType=FUNDING_FEE) и
implicit `fapiPrivateGetIncome` для прочих типов. **Платформа не использует его
нигде** (grep `funding|/fapi/v1/income|incomeType` по `app/` — пусто).

## 3. Баги, подтверждённые в коде

| # | Серьёзность | Где | Суть |
|---|---|---|---|
| 1 | 🔴 крит | весь `app/` | Фандинг не учитывается нигде. Каждая позиция, пережившая funding-таймштамп (раз в 8ч), имеет неверный realized. |
| 2 | 🔴 крит | `service.py:2228` | OPEN-позиция: `realized = −fees`; realized от уже закрытых частей (multi-TP) теряется. |
| 3 | 🟠 выс | `futures_pnl.py:30-38`, `live.py:645-648` | Комиссия не в USDT/quote/base (BNB, скидка 25%) → 0 → PnL завышен. Spot (`pnl.py:40-43`) это умеет, фьючерс — нет. |
| 4 | 🟠 выс | `futures_pnl.py:41-118` | realized пересчитывается FIFO из цен вместо авторитетного `raw_trade.info.realizedPnl`. Дрейфует при нехватке истории (backfill 30 дней). |
| 5 | 🟠 выс | движки A vs B | Два рассинхронных движка → разный realized для одного аккаунта на разных эндпоинтах. |
| 6 | 🟡 сред | `portfolio.py:192-207` | `margin_used_usdt` суммирует `position_size_usdt`, но `quantity = position_size_usdt/price` (`service.py:3141`) ⇒ это **ноционал**, а маржа = ноционал/leverage. Расхождение в `leverage` раз. |
| 7 | 🟡 сред | `service.py:2149-2158` | realized закрытой позиции = `directional(close_price)`×полный объём; для multi-TP на разных ценах — приближение (точный путь только в `_infer_closed_position_from_trades`, и он ищет Bybit-ключи `closedPnl/execPnl`, не Binance `realizedPnl`). |
| 8 | 🟡 сред | `pnl.py:84-85` | Spot `average_entry_price` включает комиссии (`effective_cost = price*qty + fee`) → завышенная цена входа клиенту. |
| D | 🟢 отдача | схемы | Нет `funding_usdt`/`commission_usdt`/`gross_realized`/`net_pnl` — клиент не видит фандинг и не может разложить PnL. `total_fee_usdt` — только USDT. `realized_pnl_usdt` у OPEN = `−fees` (мислейбл). `total_pnl` местами считается на клиенте. |

**Ключевая декомпозиция (канон, к которому приводим всё):**
```
gross_realized_usdt = Σ realizedPnl (закрывающие fill'ы, ценовой PnL биржи)
commission_usdt     = Σ fee→quote (open+close fill'ы; BNB конвертим по mark)
funding_usdt        = Σ FUNDING_FEE income в окне позиции (знаковый)
net_pnl_usdt        = gross_realized − commission + funding (+ unrealized для OPEN)
```
Замечание: Binance `realizedPnl` per-trade — это **ценовой** PnL, БЕЗ комиссии и
БЕЗ фандинга; их добавляем отдельно. Это снимает баги 1–5,7 одним источником истины.

## 4. Архитектурные решения

- **Источник истины — биржа, не пересчёт.** realized берём из
  `raw_trade.info.realizedPnl`; funding — из `/fapi/v1/income`. FIFO остаётся
  только как fallback, когда `realizedPnl` отсутствует (старые/чужие fill'ы).
- **Аддитивная схема БД.** Новая колонка `realized_pnl` (nullable) в
  `exchange_trade_ledger` и **новая** таблица `exchange_income_ledger`. Ничего
  существующего не ломаем; backfill `realized_pnl` из уже сохранённого
  `raw_trade` — без повторных запросов к бирже.
- **Один движок PnL.** Свести A и B к общему хелперу
  `compute_realized_breakdown(ledger_rows, income_rows, live_position)` →
  `(gross_realized, commission, funding, net, unrealized)`. Оба эндпоинта зовут его.
- **Только Binance.** Income-sync и весь новый учёт — для Binance (реальные
  деньги). Не-Binance аккаунты тихо пропускаем (no-op, без спама в логах). Bybit —
  отдельный backlog, в этом плане не реализуем. _(решение пользователя)_
- **Комиссия уже в леджере.** Context7 подтвердил: Binance `userTrades` отдаёт
  per-trade `realizedPnl` И отдельно `commission`/`commissionAsset`; ccxt кладёт
  их в `fee.cost`/`fee.currency` → в `exchange_trade_ledger.fee_cost`/`fee_currency`.
  Значит COMMISSION income тянуть НЕ нужно — комиссия уже есть, чиним только её
  оценку в USDT (BNB, T9). Из `/income` тянем только `FUNDING_FEE`.
- **Атрибуция фандинга — окно позиции по (account_id, symbol).** _(решение
  «лучше и проще»)_ Каждый config = свой суб-аккаунт + один символ профиля, позиции
  обычно последовательны ⇒ `Σ FUNDING_FEE where account_id=…, symbol=…,
  income_at ∈ [opened_at, closed_at|now]` точно бьётся в позицию без матчинга по
  `tranId`. Аккаунт/символ-уровень (портфель, daily-loss) — всегда точен. Caveat:
  при `max_open_positions>1` и нескольких позициях одного символа в одном окне
  per-position split — приближение (аккаунт-уровень остаётся точным); задокументировать.
- **Без изменения смысла `position_size_usdt`.** Это execution-вход (sizing).
  Чиним только отчётное `margin_used_usdt` в агрегации портфеля.

## 5. Границы: что НЕ трогаем (требование пользователя)

НЕ изменять: размещение/отмену ордеров (`ccxt_adapter` order-методы,
`trading_service` order-методы), lifecycle открытия/закрытия позиций
(open/close во `service.py`, `watchers/`, `sl_tp/`), pre-trade risk-engine
(`auto_trade/risk/engine.py`), обработку очереди сигналов, sizing
(`quantity = position_size_usdt/price`). Все остальные изменения — в
read/report/sync/calc и в Pydantic-схемах ответа.

> ⚠️ **Исключение (по решению пользователя): `_today_realized_pnl_usdt`
> (`service.py:1278`) ВХОДИТ в scope** — добавляем полный учёт (gross realized −
> commission + funding) в daily-loss gate (задача T14). Это меняет поведение
> торгового гейта (потери считаются точнее → лимит может срабатывать чуть раньше).
> **Жёсткое ограничение:** функция остаётся **чистым SQL-агрегатом без вызовов
> биржи** (как сейчас, см. её docstring про K live round-trips на hot path) — берём
> данные только из локально синканных `exchange_trade_ledger.realized_pnl`,
> `fee_cost` и `exchange_income_ledger`. Никаких новых exchange-вызовов на пути
> сигнала. Покрывается тестами риск-движка + отдельный checkpoint.

## 6. Граф зависимостей

```
T1 ledger.realized_pnl (колонка+миграция+backfill из raw_trade)
  └─ T2 sync пишет realized_pnl на upsert
        └─ T3 futures-движок: gross_realized = Σ realized_pnl → /accounts/{id}/trades
T4 exchange_income_ledger (таблица+миграция+модель)
  └─ T5 income-sync сервис + Taskiq-задача (FUNDING_FEE [+COMMISSION/REALIZED_PNL])
        └─ T6 funding-агрегатор: Σ FUNDING_FEE по аккаунту/символу/окну позиции
T9 BNB/не-USDT комиссия → quote по mark (futures _fee_to_quote + total_fee_usdt)   [независимо]
(T3,T6) ─ T7 OPEN-снапшот: realized от закрывающих fill'ов + funding (не −fees)
(T3,T7) ─ T8 унификация: общий compute_realized_breakdown для обоих движков
(T3,T6,T9) ─ T10 схемы: gross_realized/commission/funding/net_pnl + server total_pnl
(T2,T6) ─ T14 daily-loss gate: net = gross realized − commission + funding (pure-DB!)  [торговый гейт]
T11 portfolio margin_used = notional/leverage                                       [независимо]
T12 spot avg_entry без комиссий                                                     [независимо]
T13 тест-матрица/регресс
```

Порядок реализации — снизу вверх по графу. Независимые T9/T11/T12 можно делать
параллельно в любой момент (fail-fast: T11/T12 — дешёвые, делаем рано).

---

## 7. Задачи

### Фаза 1 — Авторитетный realized (ядро точности)

#### T1: Колонка `realized_pnl` в `exchange_trade_ledger` + миграция + backfill
**Описание:** Добавить nullable `realized_pnl: float | None` в модель и таблицу.
В `upgrade()` после `add_column` сделать data-backfill: для строк, где
`raw_trade->'info'->>'realizedPnl'` парсится в число — записать его (Binance).
Идемпотентно (guard на наличие колонки, как в существующих миграциях).

**Acceptance criteria:**
- [ ] Модель `ExchangeTradeLedger` имеет `realized_pnl: Mapped[float | None]`.
- [ ] Миграция `migrations/versions/2026...._0022_*.py` (down_revision = `20260603_0021`) добавляет колонку и бэкфиллит из `raw_trade` (Postgres+SQLite, guard на повторный запуск).
- [ ] `downgrade()` дропает колонку.

**Verification:**
- [ ] `uv run alembic upgrade head` и `... downgrade -1` проходят на чистой БД.
- [ ] `uv run typecheck`, `uv run lint` чисто.
- [ ] Ручная проверка: после upgrade у строк с `info.realizedPnl` значение перенесено (unit на backfill-парсер).

**Dependencies:** None
**Files:** `app/models/exchange_trade_ledger.py`, `migrations/versions/2026..._0022_add_ledger_realized_pnl.py`
**Scope:** S

#### T2: Sync заполняет `realized_pnl` на upsert
**Описание:** В `trade_sync.py` извлечь realized из `trade.raw["info"]["realizedPnl"]`
(хелпер `_extract_realized_pnl`, аналог `_extract_client_order_id`), положить в
`rows[...]["realized_pnl"]` и добавить `realized_pnl` в `update_columns` upsert'а.

**Acceptance criteria:**
- [ ] Оба пути (`sync_symbol_trades`, `sync_account_symbol_trades`) пишут `realized_pnl`.
- [ ] `realized_pnl` в `_upsert_ledger_rows.update_columns` (обновляется при ре-синке).
- [ ] Отсутствие/нечисловой `realizedPnl` → `None`, не падает.

**Verification:**
- [ ] `uv run pytest tests/test_auto_trade_trade_sync.py` (расширить фикстуру raw с `info.realizedPnl`).
- [ ] `uv run typecheck` чисто.

**Dependencies:** T1
**Files:** `app/services/auto_trade/trade_sync.py`, `tests/test_auto_trade_trade_sync.py`
**Scope:** S

#### T3: Futures-движок отдаёт `gross_realized = Σ realized_pnl`
**Описание:** В `calculate_futures_pnl_fifo` (или новой `compute_futures_pnl`)
если у всех/части строк есть `realized_pnl` — realized = `Σ realized_pnl`
(биржевой), FIFO оставить как fallback на строки без `realized_pnl`. Вернуть
расширенный снапшот: `gross_realized`, `commission`, (`funding` пока 0),
`unrealized`. Прокинуть в `AccountTradesService` и `/accounts/{id}/trades`.

**Acceptance criteria:**
- [ ] При наличии `realized_pnl` у строк realized = их сумма (не FIFO-из-цен).
- [ ] Строки без `realized_pnl` доезжают через FIFO-fallback (обратная совместимость).
- [ ] `AccountTradesPnlRead.realized` == биржевой Σ realizedPnl в тесте.

**Verification:**
- [ ] `uv run pytest tests/test_futures_pnl.py tests/test_account_trades_service.py tests/test_accounts_trades_endpoint.py`.
- [ ] Регресс: старые тесты (FIFO без realized_pnl) зелёные.

**Dependencies:** T2
**Files:** `app/services/execution/futures_pnl.py`, `app/services/execution/account_trades_service.py`, `tests/test_futures_pnl.py`
**Scope:** M

### ▣ Checkpoint A (после T1–T3)
- [ ] `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
- [ ] `uv run pytest tests/ -q` — зелено; `uv run lint && uv run typecheck` — чисто
- [ ] `/accounts/{id}/trades` отдаёт realized из биржевого `realizedPnl` (проверить на демо-аккаунте)
- [ ] **Ревью с человеком: согласовать декомпозицию net и контракт хелпера до Фазы 2**

### Фаза 2 — Фандинг (новый источник)

#### T4: Таблица/модель `exchange_income_ledger` + миграция
**Описание:** Новая таблица: `id, user_id, account_id, exchange_name,
market_type, income_type, asset, income (Float), symbol (nullable), tran_id,
trade_id (nullable), info (nullable), income_at (tz), ingested_at (tz),
raw (JSON)`. Unique `(account_id, exchange_name, income_type, tran_id)`
(Binance: `tranId` уникален per user+type). Индексы:
`(account_id, symbol, income_at)`, `(account_id, income_type, income_at)`.
Зеркалит структуру `exchange_trade_sync_state`/ledger по стилю.

**Acceptance criteria:**
- [ ] Модель `ExchangeIncomeLedger` + регистрация в `app/models/__init__.py`.
- [ ] Миграция `_0023_*` (down_revision = `_0022`), guard-идемпотентна, с `downgrade()`.
- [ ] Расширить/переиспользовать `exchange_trade_sync_state` или добавить `income`-курсор (решение зафиксировать в задаче; предпочтительно отдельный `last_income_ts_ms` через новый scope `market_type='futures_income'`).

**Verification:**
- [ ] `uv run alembic upgrade head && ... downgrade -1` ок.
- [ ] `uv run typecheck && uv run lint` чисто.

**Dependencies:** None (логически перед T5)
**Files:** `app/models/exchange_income_ledger.py`, `app/models/__init__.py`, `migrations/versions/..._0023_add_income_ledger.py`
**Scope:** S

#### T5: Income-sync сервис + Taskiq-задача
**Описание:** `ExchangeIncomeSyncService.sync_running_configs` по образцу
trade-sync: для каждого running-config (**только Binance**) тянуть
`fetch_funding_history(symbol, since, limit)` (Binance → `fapiPrivateGetIncome`
incomeType=FUNDING_FEE), upsert в `exchange_income_ledger`, продвигать курсор.
**Только `FUNDING_FEE`** — комиссия уже в trade-ledger (`fee_cost`), `REALIZED_PNL`
уже в `realized_pnl` (T1). Адаптер: добавить `fetch_futures_income(symbol, since,
limit)` в `CcxtAdapter`/`TradingService` (read-only). Зарегистрировать Taskiq-задачу
`sync_auto_trade_exchange_income` (`cron "* * * * *"`) в `worker/tasks.py`.
Не-Binance config — тихий skip (no-op), задача не падает.

**Acceptance criteria:**
- [ ] Новый метод адаптера тянет funding постранично, нормализует `{income_type, asset, income, symbol, tran_id, trade_id, time, raw}` из `info`.
- [ ] Idempotent upsert по `(account_id, exchange_name, income_type, tran_id)` — повторный sync не дублирует.
- [ ] Taskiq-задача зарегистрирована, появляется в расписании, логирует ненулевую статистику (как trade-sync).
- [ ] Не-Binance config пропускается без ошибки (no-op).

**Verification:**
- [ ] `uv run pytest tests/test_auto_trade_tasks.py` + новый `tests/test_income_sync.py` (мок адаптера с funding-строками; повторный sync не дублирует).
- [ ] `uv run typecheck && uv run lint`.

**Dependencies:** T4
**Files:** `app/services/auto_trade/income_sync.py` (новый), `app/services/execution/ccxt_adapter.py`, `app/services/execution/trading_service.py`, `app/worker/tasks.py`, `tests/test_income_sync.py`
**Scope:** M

#### T6: Агрегатор фандинга
**Описание:** Хелпер `sum_funding(session, account_id, symbol, start, end)` →
знаковая сумма `income` по `income_type='FUNDING_FEE'`. Вариант для окна позиции
(`[opened_at, closed_at|now]`) и для аккаунт/символ-уровня. Документировать
caveat перекрытия (несколько позиций одного символа в одном окне).

**Acceptance criteria:**
- [ ] Функция возвращает знаковую сумму, 0.0 при отсутствии строк.
- [ ] Покрыта unit-тестом (положительный и отрицательный фандинг).

**Verification:** `uv run pytest tests/test_income_sync.py -k funding_sum`
**Dependencies:** T5
**Files:** `app/services/auto_trade/income_sync.py` (или `funding.py`), тест
**Scope:** S

### ▣ Checkpoint B (после T4–T6)
- [ ] Миграции up/down ок; `uv run pytest tests/ -q`; lint+typecheck чисто
- [ ] На демо-аккаунте после прогона задачи в `exchange_income_ledger` появляются FUNDING_FEE строки
- [ ] **Ревью с человеком**

### Фаза 3 — Открытые позиции + унификация

#### T7: OPEN-снапшот — realized от закрывающих fill'ов + funding
**Описание:** В `build_position_pnl_snapshot` (ветка OPEN, `service.py:2190-2252`)
заменить `realized = −fees` на:
`gross_realized = Σ realized_pnl` закрывающих fill'ов позиции (из ledger по
`auto_trade_position_id`/окну), `commission = Σ fee→quote`,
`funding = sum_funding(окно)`; `realized = gross_realized − commission + funding`;
`net total = realized + unrealized`. CLOSED-ветка (`2136-2188`): добавить funding и
использовать ledger Σ realized_pnl как приоритет над `directional(close_price)`.

**Acceptance criteria:**
- [ ] OPEN с частичными TP: realized включает реализованную часть (повторяет кейс «поза 236»: ~+12 USDT, не −fees).
- [ ] CLOSED: realized = биржевой Σ realizedPnl − commission + funding (multi-TP не теряется).
- [ ] При отсутствии ledger-данных — graceful fallback на текущую логику.

**Verification:**
- [ ] `uv run pytest tests/test_auto_trade_service.py tests/test_auto_trade_endpoints.py` + новые кейсы (multi-fill close, OPEN с частичным закрытием, позиция через funding-таймштамп).
- [ ] Ручная сверка `/auto-trade/positions` на демо против Binance UI.

**Dependencies:** T3, T6
**Files:** `app/services/auto_trade/service.py`, `tests/test_auto_trade_service.py`
**Scope:** M

#### T8: Унификация движков (single source of truth)
**Описание:** Вынести общий `compute_realized_breakdown(...)` →
`(gross_realized, commission, funding, net, unrealized)`, использующий ledger
`realized_pnl` + income funding + live unrealized. Оба пути
(`calculate_futures_pnl_fifo`/account_trades и `build_position_pnl_snapshot`)
зовут его → одинаковый realized для одного аккаунта/символа.

**Acceptance criteria:**
- [ ] `/accounts/{id}/trades` и `/auto-trade/positions` дают согласованный realized на одних данных (тест сверки).
- [ ] FIFO остаётся только fallback внутри хелпера.

**Verification:**
- [ ] Новый `tests/test_pnl_engines_consistency.py` (один датасет → оба пути → равенство в пределах эпсилон).
- [ ] Регресс всех существующих PnL-тестов.

**Dependencies:** T3, T7
**Files:** `app/services/execution/futures_pnl.py` (общий хелпер), `app/services/auto_trade/service.py`, `app/services/execution/account_trades_service.py`, новый тест
**Scope:** M

### ▣ Checkpoint C (после T7–T8)
- [ ] Оба эндпоинта согласованы; `uv run pytest tests/ -q`; lint+typecheck
- [ ] **Ревью с человеком**

### Фаза 4 — Комиссии, отдача, мелкие фиксы

#### T9: Не-USDT комиссии (BNB) → quote по mark
**Описание:** В futures `_fee_to_quote` (`futures_pnl.py:30-38`) добавить ветку
конвертации по mark-price (как spot `pnl.py:40-43`, через `mark_prices` dict).
В `live.py:645-648` `total_fee_usdt` — суммировать через тот же конвертер, не
только `fee_currency=="USDT"`. Источник mark: уже доступный live mark/ccxt
ticker (read-only) либо `COMMISSION` income.

**Acceptance criteria:**
- [ ] Комиссия в BNB конвертируется в USDT (не 0).
- [ ] `total_fee_usdt` включает не-USDT комиссии.
- [ ] Нет mark → 0 с warning (как сейчас), не падает.

**Verification:** `uv run pytest tests/test_futures_pnl.py -k fee` + кейс BNB-fee; ручной `/auto-trade/trades`.
**Dependencies:** None (желательно после T8 для общего конвертера)
**Files:** `app/services/execution/futures_pnl.py`, `app/api/v1/endpoints/live.py`, тест
**Scope:** S

#### T10: Схемы/отдача — разложение PnL + server-side total
**Описание:** Добавить в `AutoTradePositionPnlRead`, `AccountTradesPnlRead`,
сводки и `StrategyPortfolioEntryRead`/`PortfolioSummaryResponse` поля:
`gross_realized_usdt`, `commission_usdt`, `funding_usdt`, `net_pnl_usdt`.
`total_pnl_usdt` считать на сервере. `AutoTradeLedgerTradesSummaryRead`:
`total_fee_usdt` (все валюты) + опц. `total_funding_usdt`. Все новые поля —
nullable/`default`, чтобы не ломать существующих клиентов.

**Acceptance criteria:**
- [ ] Эндпоинты возвращают новые поля; `openapi.json` регенерён.
- [ ] OPEN-позиция: `realized_pnl_usdt` больше не равен `−fees` (берёт частичные закрытия), `net_pnl_usdt` явный.
- [ ] Существующие поля сохранены (обратная совместимость).

**Verification:** `uv run pytest tests/test_auto_trade_endpoints.py tests/test_accounts_trades_endpoint.py`; diff `openapi.json`.
**Dependencies:** T3, T6, T9
**Files:** `app/schemas/auto_trade.py`, `app/schemas/exchange_trading.py`, `app/api/v1/endpoints/live.py`, `app/api/v1/endpoints/accounts.py`, `openapi.json`, тесты
**Scope:** M

#### T11: `margin_used_usdt` = notional / leverage
**Описание:** В `portfolio.py:192-207` вместо суммы `position_size_usdt`
считать маржу: тянуть также `leverage` (и `entry_price`/`quantity` при наличии)
открытых позиций и суммировать `position_size_usdt / max(leverage,1)`
(`position_size_usdt` = ноционал, см. `service.py:3141,3811`). Обновить
комментарий-док.

**Acceptance criteria:**
- [ ] `margin_used_usdt` = Σ notional/leverage по открытым позициям конфига.
- [ ] Тест: leverage=10, notional=1000 → margin=100 (раньше было 1000).

**Verification:** `uv run pytest tests/test_auto_trade_multi_strategy.py -k margin` (или новый).
**Dependencies:** None
**Files:** `app/services/auto_trade/portfolio.py`, тест
**Scope:** S

#### T12: Spot `average_entry_price` без комиссий
**Описание:** В `calculate_spot_pnl` (`pnl.py:84-85`) хранить lot по цене fill'а
(`entry_price = price`), комиссию учитывать отдельно в realized/fees (как уже
делается для realized), не «зашивать» в entry. Сохранить корректность realized.

**Acceptance criteria:**
- [ ] `average_entry_price` == фактическая взвешенная цена fill'ов (без fee).
- [ ] realized/fees не изменились численно (комиссия по-прежнему вычитается из realized).

**Verification:** `uv run pytest tests/test_spot_pnl.py` (обновить ожидаемый avg_entry).
**Dependencies:** None
**Files:** `app/services/execution/pnl.py`, `tests/test_spot_pnl.py`
**Scope:** S

#### T14: Daily-loss gate — полный учёт (gross − commission + funding), pure-DB ⚠️ торговый гейт
**Описание:** _(решение пользователя: «добавляем весь учёт»)._ Переписать
`_today_realized_pnl_usdt` (`service.py:1278-1318`) с ценового
`(close_price−entry_price)*qty` на **чистый агрегат из локального леджера** для
сделок, закрытых/реализованных сегодня (UTC), scoped к `config_id`:
`net = Σ realized_pnl(fills сегодня) − Σ commission(fills сегодня) + Σ FUNDING_FEE
(account+symbol, income_at≥day_start)`. Источники: `exchange_trade_ledger.realized_pnl`
(T1/T2), `fee_cost`→quote (T9-конвертер), `exchange_income_ledger` (T4/T6).
**Категорически без вызовов биржи** — всё уже синкается в фоне; hot path сигнала
остаётся DB-only (см. docstring функции про K round-trips).

**Acceptance criteria:**
- [ ] Возвращает net (gross realized − commission + funding) для сегодняшних реализаций, scoped к config.
- [ ] Ни одного нового exchange-вызова (только SQL по трём локальным таблицам) — проверить, что в тесте адаптер не дёргается.
- [ ] Fallback: нет `realized_pnl`/income → деградирует к ценовому gross (не падает, не блокирует торговлю некорректно).
- [ ] Знак: фандинг-выплата (income<0) увеличивает дневной убыток; полученный фандинг (income>0) уменьшает.

**Verification:**
- [ ] `uv run pytest tests/test_auto_trade_risk_engine.py` — существующие daily-loss тесты зелёные/обновлены.
- [ ] Новые кейсы: позиция через funding-таймштамп считается в дневной убыток; BNB-комиссия учтена.
- [ ] Diff не трогает сам risk-engine (`engine.py`) — меняется только числитель в `service.py`.

**Dependencies:** T2, T6 (и T9 для конвертера комиссий)
**Files:** `app/services/auto_trade/service.py`, `tests/test_auto_trade_risk_engine.py`
**Scope:** S

### ▣ Checkpoint D (после T14) — торговый гейт
- [ ] `uv run pytest tests/test_auto_trade_risk_engine.py tests/ -q` зелено; lint+typecheck
- [ ] Подтвердить pure-DB: в тесте daily-loss мок-адаптер НЕ вызывается на пути сигнала
- [ ] **Ревью с человеком: согласовать, что более раннее срабатывание daily-loss при учёте фандинга/комиссий — желаемое поведение**

### Фаза 5 — Тест-матрица и регресс

#### T13: Тест-матрица точности
**Описание:** Сводный набор unit/интеграционных тестов на инварианты:
realized = Σ realizedPnl; включение фандинга (через funding-таймштамп);
BNB-комиссия ≠ 0; OPEN с частичными закрытиями; multi-fill закрытие; согласие
двух движков; net = gross − commission + funding.

**Acceptance criteria:**
- [ ] Все сценарии из аудита покрыты тестами и зелёные.
- [ ] Нет регрессий в `tests/` (полный прогон).

**Verification:** `uv run pytest tests/ -q && uv run lint && uv run typecheck`.
**Dependencies:** T3, T7, T8, T9, T10, T11, T12, T14
**Files:** `tests/test_pnl_*` (новые/расширенные)
**Scope:** M

### ▣ Checkpoint Final
- [ ] Полный `uv run pytest tests/ -q` зелёный; `uv run lint && uv run typecheck` чисто
- [ ] Миграции up→down→up ок; `openapi.json` актуален
- [ ] Демо-сверка `/positions`, `/portfolio`, `/accounts/{id}/trades` против Binance UI
- [ ] Подтверждено: order/open/close/`engine.py` не изменены (единственное касание гейта — числитель `_today_realized_pnl_usdt`, T14, pure-DB)
- [ ] **Финальное ревью с человеком**

## 8. Риски и митигации

| Риск | Влияние | Митигация |
|---|---|---|
| Income API лимит 3 мес. / weight 20 | Сред | Тянуть инкрементально по курсору раз в минуту; backfill ≤3 мес.; учесть rate-limit (ccxt `enableRateLimit`). |
| Атрибуция фандинга к позиции (перекрытие позиций одного символа) | Сред | Окно `[opened_at, closed_at]`+symbol; задокументировать caveat; аккаунт-уровень как сверка. |
| Двойной учёт: realizedPnl уже «чистый»? | Выс | Зафиксировано: Binance `realizedPnl` — ценовой, БЕЗ fee/funding; комиссию/фандинг добавляем отдельно. Покрыть тестом-инвариантом. |
| Изменение realized сломает потребителей фронта | Сред | Новые поля аддитивны; старые сохранены; регенерация `openapi.json`; согласовать с фронтом на Checkpoint A. |
| Случайно задеть execution/risk | Выс | Жёсткие границы (раздел 5); diff-review на каждом checkpoint; единственное касание гейта — числитель T14. |
| T14 меняет поведение daily-loss gate | Выс | Учёт фандинга/комиссий → лимит срабатывает чуть раньше (желаемо, решение пользователя); pure-DB (без exchange-вызовов на hot path); fallback на ценовой gross; Checkpoint D + ревью. |
| ccxt income shape отличается demo↔real | Низ | Нормализовать из `info` (raw); тест на обе формы; demo-смоук (`exchange_demo` маркер). |

## 9. Решения (закрыто пользователем)

1. ✅ **`_today_realized_pnl_usdt` (daily-loss gate)** — добавляем полный учёт (T14), pure-DB, с risk-ревью на Checkpoint D.
2. ✅ **Только Binance.** Bybit income — backlog, не в этом плане.
3. ✅ **Из `/income` тянем только `FUNDING_FEE`.** Комиссия уже в `fee_cost` (Context7: `commission`/`commissionAsset` per-trade), realized — в `realized_pnl` (T1). `COMMISSION`/`REALIZED_PNL` НЕ тянем.
4. ✅ **Атрибуция фандинга — окно `[opened_at, closed_at|now]` по (account_id, symbol)** («лучше и проще»). Аккаунт-уровень точен; per-position split — приближение только при `max_open_positions>1` на одном символе (задокументировать).

### Остаётся уточнить по ходу (не блокирует старт)
- **mark-price для BNB-комиссии (T9):** предпочтительно — взять сумму комиссии в USDT, агрегируя по `commissionAsset` через live mark/ticker (read-only). Если дорого по запросам — оценивать BNB по последнему известному mark из live-позиции. Финализировать в T9.
```

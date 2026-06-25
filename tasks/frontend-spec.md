# ТЗ для фронтенда — точное разложение PnL (W9)

> Бэкенд-изменения W9 (точность PnL/комиссий/фандинга) меняют **контракт нескольких
> эндпоинтов**: добавлены поля разложения PnL и поменялась семантика части старых
> полей. Логика торговли (открытие/закрытие) **не менялась** — это только отдача
> и точность чисел. Все новые поля **аддитивны и nullable**, старые поля остались,
> так что текущий фронт не сломается, но числа в нём станут точнее (а местами
> заметно изменятся — см. §6 «Изменения поведения»).

## 1. Зачем (контекст)

Раньше PnL пересчитывался из цен (FIFO), фандинг не учитывался нигде, комиссии в
BNB обнулялись, у открытых позиций realized = `−комиссии` (терялась прибыль от
частичных TP), а «маржа» в портфеле на самом деле была ноционалом. Теперь источник
истины — биржа: **realizedPnl из трейдов + фандинг из `/fapi/v1/income`**, а PnL
раскладывается на компоненты:

```
net_pnl = gross_realized − commission + funding   (+ unrealized для открытых)
```

- `gross_realized` — ценовой PnL закрытых частей (биржевой `realizedPnl`), БЕЗ комиссий и фандинга.
- `commission` — торговые комиссии в USDT (включая BNB по mark-цене), всегда ≥ 0.
- `funding` — знаковый фандинг (отрицательный = заплатили, положительный = получили).
- `net_pnl` — то, что реально осело на счёте по закрытым частям.
- `unrealized` — нереализованный PnL ещё открытой части (с биржи).

> Источники подтверждены доками Binance (Context7): в `userTrades` `realizedPnl` и
> `commission`/`commissionAsset` — **отдельные** поля; фандинг — отдельный поток
> `FUNDING_FEE` в `/fapi/v1/income`; `entryPrice` (цена входа) отделён от
> `breakEvenPrice` (с учётом комиссий); `initialMargin = notional / leverage`.

## 2. Затронутые эндпоинты (сводка)

| Эндпоинт | Что нового | Что изменилось по значению |
|---|---|---|
| `GET /api/v1/live/auto-trade/positions` | `pnl.{gross_realized_usdt, commission_usdt, funding_usdt, net_pnl_usdt}` | `pnl.realized_pnl_usdt` у **открытой** позиции теперь = net (gross частичных закрытий − comm + funding), а не `−комиссии` |
| `GET /api/v1/accounts/{account_id}/trades` | `pnl.{gross_realized_usdt, commission_usdt, funding_usdt, net_pnl_usdt}` | `pnl.realized` остаётся **gross** (Σ realizedPnl) |
| `GET /api/v1/live/auto-trade/trades` | — | `summary.total_fee_usdt` теперь включает **все валюты комиссий** (BNB и пр.), а не только USDT |
| `GET /api/v1/live/auto-trade/portfolio` | — | `strategies[].margin_used_usdt` теперь = **маржа** (notional/leverage), была ноционалом → значение падает в `leverage` раз |

`GET /api/v1/live/auto-trade/positions` использует `summary` (агрегаты) — там значения
тоже стали net (поля те же).

## 3. Поля схем (детально)

### 3.1 `AutoTradePositionPnlRead` (positions[].pnl)

Существующие поля без изменений по форме (`number | null` где `| null`):
`position_id, symbol, chart_symbol, side, status, entry_price, mark_price?,
close_price?, quantity, entry_notional_usdt, initial_margin_usdt,
realized_pnl_usdt?, unrealized_pnl_usdt?, total_pnl_usdt?, pnl_pct?, roe_pct?,
source, error?, calculated_at`.

**Новые поля:**

| Поле | Тип | Семантика |
|---|---|---|
| `gross_realized_usdt` | `number \| null` | Ценовой PnL закрытых частей (биржевой realizedPnl), без комиссий/фандинга |
| `commission_usdt` | `number \| null` | Σ комиссий по филлам позиции (USDT; BNB по mark), ≥ 0 |
| `funding_usdt` | `number \| null` | Знаковый фандинг за окно позиции `[opened_at, closed_at\|now]` |
| `net_pnl_usdt` | `number \| null` | `gross_realized − commission + funding`; равно `realized_pnl_usdt` |

- Поля `null`, когда у позиции **нет синканных филлов в леджере** (старый fallback-путь) —
  тогда показывайте только `realized_pnl_usdt`/`total_pnl_usdt` без разбивки.
- `total_pnl_usdt = net_pnl_usdt + (unrealized_pnl_usdt ?? 0)` (сервер считает сам — берите готовое).
- `source ∈ {"exchange","derived","closed","unavailable"}` — относится к источнику **unrealized**, не к realized.

### 3.2 `AccountTradesPnlRead` (accounts/{id}/trades → pnl)

| Поле | Тип | Семантика |
|---|---|---|
| `realized` | `number` | **Gross** (Σ биржевого realizedPnl). Не путать с net. |
| `unrealized` | `number` | Нереализованный (с биржи/lots) |
| `base_currency` / `quote_currency` | `string` | как раньше |
| `gross_realized_usdt` | `number \| null` | = `realized` (дублирует для единообразия с positions) |
| `commission_usdt` | `number \| null` | Σ комиссий по символу |
| `funding_usdt` | `number \| null` | Фандинг по символу (вся история) |
| `net_pnl_usdt` | `number \| null` | `gross − commission + funding` |

### 3.3 `AutoTradeLedgerTradesSummaryRead` (auto-trade/trades → summary)

| Поле | Тип | Изменение |
|---|---|---|
| `total_fee_usdt` | `number` | Теперь сумма комиссий **всех валют** в USDT (USDT напрямую, base по цене филла, BNB по best-effort mark). При недоступном mark отдельный актив даёт 0 (консервативно, без падения). |

### 3.4 `StrategyPortfolioEntryRead` (portfolio → strategies[])

| Поле | Тип | Изменение |
|---|---|---|
| `margin_used_usdt` | `number` | Теперь **маржа** = Σ(notional/leverage) по открытым позициям конфига. Раньше = Σ notional. **Значение уменьшится в `leverage` раз.** |

## 4. Рекомендации по UI

1. **Карточка/строка позиции** — показать раскладку, когда поля не `null`:
   ```
   Net PnL:        {net_pnl_usdt}            (главное число, цвет по знаку)
     ├ Realized:   {gross_realized_usdt}     (ценовой PnL закрытых частей)
     ├ Commission: −{commission_usdt}
     ├ Funding:    {funding_usdt}            (знаковое)
     └ Unrealized: {unrealized_pnl_usdt}     (открытая часть)
   Total:          {total_pnl_usdt}
   ```
   Если разбивка `null` (нет леджера) — fallback на старое отображение `realized_pnl_usdt` + `unrealized_pnl_usdt`.

2. **Фандинг** — отдельная строка/иконка; знак важен (выплата vs получение). Добавьте тултип
   «funding за время удержания позиции». **Если блок фандинга в UI выводится в принципе — показывать
   всегда, даже при `0` (`0.00`)**, чтобы не было «прыгающих» строк (_решение продукта_).

3. **Trades-таблица** — `total_fee_usdt` теперь честный; если показываете комиссию по строке трейда, она в `fee` + `fee_currency` (как раньше; пер-строчная конвертация в USDT на фронте не делается — берите агрегат `summary.total_fee_usdt`).

4. **Портфель** — `margin_used_usdt` теперь маржа; обновите подпись/тултип (раньше, возможно,
   подписывали как ноционал). **Отдельно ноционал в портфеле пока НЕ показываем** — маржи
   достаточно (_решение продукта_). Если в будущем понадобится ноционал — это `position_size_usdt`
   позиции (или `margin_used_usdt × leverage`).

5. **Accounts/{id}/trades — основное число = `net_pnl_usdt` (net).** Это индустриальный стандарт
   для сводного P&L: авторитетный «реальный» PnL аккаунта у Binance = сумма income-потоков
   `REALIZED_PNL + COMMISSION + FUNDING_FEE` (т.е. net). `pnl.realized` (= gross, Σ биржевого
   realizedPnl) и `commission_usdt`/`funding_usdt` показывайте в разворачиваемой детализации.
   **В построчной истории трейдов** — биржевая конвенция: gross `realizedPnl` (поле `raw`/строка)
   + отдельная колонка Fee (`fee`/`fee_currency`), как в Binance trade history.

## 5. Обработка null / обратная совместимость

- Все 4 новых поля (`gross_realized_usdt`/`commission_usdt`/`funding_usdt`/`net_pnl_usdt`)
  — **опциональные**, `null` на fallback-пути. Фронт обязан гейтить разбивку по `!= null`.
- Старые поля сохранены — старый фронт продолжит работать, просто покажет менее точные/менее
  детальные числа.
- Формат чисел — `float` (USDT). Рекомендуется округление до 2 знаков в UI, но хранить полное.

## 6. ⚠️ Изменения поведения (важно для QA/дизайна)

1. **Открытая позиция, `realized_pnl_usdt`**: раньше ≈ `−комиссии` (часто около 0/слегка минус);
   теперь включает прибыль уже закрытых частей (multi-TP) + фандинг. Числа у открытых позиций
   с частичными закрытиями вырастут — это правильно (раньше недосчитывали).
2. **Портфель `margin_used_usdt`** уменьшится в `leverage` раз (был ноционал). Прогресс-бары/
   проценты загрузки депозита, если считались от этого поля, надо пересмотреть.
3. **`total_fee_usdt`** вырастет на аккаунтах, где комиссии платятся в BNB (раньше = 0 для BNB).
4. **Daily-loss** (бэкенд-гейт, не UI) теперь считает net (с фандингом/комиссиями) → может
   срабатывать чуть раньше. На фронт напрямую не влияет, но если показываете «дневной PnL/лимит» —
   значения станут чуть отрицательнее.
5. **`/accounts` `realized` = gross, `/positions` `realized_pnl_usdt` = net** — это разные числа
   по дизайну; сверять их между собой можно только через разбивку (`net = gross − commission + funding`).

## 7. Не входит в этот контракт

- **Spot PnL** (`average_entry_price` теперь без комиссий = цена филла, отдельно break-even):
  сервисный метод есть, но **HTTP-эндпоинт сейчас не подключён**. Если/когда заведём spot-PnL
  эндпоинт — `SpotPnlAsset.average_entry_price` будет фактической ценой входа (ниже break-even на
  величину комиссии). Пока на фронт не влияет.

## 8. Чеклист интеграции (для фронта)

- [ ] Обновить типы/DTO: добавить 4 опциональных поля в `AutoTradePositionPnlRead` и `AccountTradesPnlRead`.
- [ ] Позиции: отображать разбивку net/realized/commission/funding/unrealized с гейтом по `!= null`.
- [ ] Портфель: пересмотреть подпись и любые расчёты от `margin_used_usdt` (теперь маржа).
- [ ] Trades: довериться `summary.total_fee_usdt` (все валюты).
- [ ] Accounts/{id}/trades: показывать `net_pnl_usdt` как основной, gross/commission/funding в детали.
- [ ] QA: проверить кейсы — открытая позиция с частичными TP, позиция через funding-таймштамп, комиссии в BNB.
- [ ] Источник схем: актуальный `openapi.json` (перегенерён бэкендом; новые поля присутствуют).

## 9. Решения продукта (закрыто)

1. ✅ **Фандинг показываем всегда, даже при `0`** — если блок фандинга в UI присутствует в принципе
   (см. §4.2). Не скрывать нулевые значения.
2. ✅ **В портфеле — только маржа (`margin_used_usdt`), ноционал отдельно НЕ выводим** пока хватает
   остальной информации (см. §4.4). Вернёмся, если появится потребность.
3. ✅ **Основное число на `/accounts` — net (`net_pnl_usdt`)**, по индустриальному стандарту
   (net = `REALIZED_PNL + COMMISSION + FUNDING_FEE`, как у Binance). Gross/commission/funding —
   в детализации; построчная история трейдов — gross realizedPnl + отдельная Fee (см. §4.5).

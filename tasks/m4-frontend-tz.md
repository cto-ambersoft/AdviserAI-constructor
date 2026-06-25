# ТЗ для фронтенда — Milestone 4 (что обновилось в бэкенде и что реализовать)

> Аудитория: команда `constructor-front`. Бэкенд `constructor` за Phase 1–2 ушёл вперёд; контракт (`openapi.json` + `lib/api/openapi-types.ts`) уже синхронизирован — типы актуальны. Этот документ описывает, что фронту нужно сделать поверх нового контракта.
> Перегенерация типов (если подтянули бэк): `npm run gen:api-types` — **нужен Node ≥18** (дефолтный node v14 падает).

---

## 0. TL;DR — что появилось на бэке и что делать фронту

| Бэкенд-фича | Эндпоинты | Задача фронта |
|---|---|---|
| **2FA (TOTP)** | `POST /auth/2fa/{enroll,verify,step-up}`, `GET /auth/2fa/status`, `DELETE /auth/2fa` | **F4** — UI 2FA (QR, verify, recovery, disable) |
| **Step-up gating** | заголовок `X-Step-Up-Token` на критичных действиях | **сквозной flow** — перехват 403, запрос кода, повтор с токеном |
| **Live-KPI портфеля** | `GET /live/auto-trade/portfolio`, `…/strategies/{id}/health` (+ `kpi_as_of`) | **F2** — Live Monitoring Dashboard |
| **SSE risk-события** | `GET /events/stream` | **F5** — live-консьюмер (через BFF-прокси) |
| **AI Forecast Catalogue** | `/ai-backtests/ai-forecast-catalogue*` | **F3** — trader-UI каталога |

Готово и **трогать не надо:** Risk Config UI (F1, commit `63b5b9a`) — но учтите: сохранение risk-конфига теперь **под step-up** (см. §2).

---

## 1. Сквозной flow: Step-up 2FA (САМОЕ ВАЖНОЕ — затрагивает и существующие формы)

Когда у пользователя **включена 2FA**, критичные действия требуют свежего step-up-токена в заголовке. Без него бэк вернёт **403**. У пользователей **без 2FA — всё работает как раньше** (pass-through), ничего не меняется.

**Критичные (gated) эндпоинты:**
- `POST /api/v1/live/auto-trade/play` — старт авто-трейда
- `PUT /api/v1/live/auto-trade/config` — сохранение конфига/risk-config (это и есть форма F1!)
- `POST /api/v1/exchange/accounts` — добавление ключа биржи
- `DELETE /api/v1/auth/2fa` — отключение 2FA

**Алгоритм (реализовать один раз, переиспользовать везде):**
1. Вызвать критичное действие как обычно.
2. Если ответ **403** (и у юзера 2FA включена) → показать модалку «Введите код из приложения».
3. `POST /auth/2fa/step-up` `{ "code": "<TOTP или recovery>" }` → `{ step_up_token, expires_in }`.
4. **Повторить** исходный запрос с заголовком `X-Step-Up-Token: <step_up_token>`.

**Учесть:**
- **Токен одноразовый** (один re-auth = одно действие). НЕ кэшировать и НЕ переиспользовать между действиями — на каждое gated-действие минтить свежий. Повтор использованного → **403** «already been used».
- TTL токена — `expires_in` (5 мин). Истёк → минтить заново.
- На `/step-up` тоже действует **lockout** (см. §5): после N неверных кодов — **429 + Retry-After**.
- Рекомендуется: тонкий interceptor в `lib/api/client.ts`, который ловит 403 на gated-путях, запускает step-up-модалку (promise) и автоматически ретраит с заголовком.

---

## 1b. Login-2FA — код при ВХОДЕ (НОВОЕ, обновить логин-флоу)

`POST /auth/signin` теперь возвращает **одно из двух** (union):
- юзер **без 2FA** → `{ access_token, refresh_token, ... }` — **как раньше, ничего менять не надо**;
- юзер **с 2FA** → `{ two_factor_required: true, challenge_token, expires_in }` — **токенов нет**.

Логика фронта на сабмите логина:
1. POST `/auth/signin`. Если в ответе есть `access_token` → обычный вход (как сейчас).
2. Если `two_factor_required === true` → показать экран ввода кода (TOTP или recovery), сохранить `challenge_token`.
3. POST `/auth/2fa/login` `{ challenge_token, code }` → `{ access_token, refresh_token, ... }` → завершить вход.

Учесть: `400` — неверный код; `429 + Retry-After` — lockout; `401` — `challenge_token` истёк/невалиден (→ начать логин заново). Recovery-код тут принимается как fallback.

## 2. F4 — UI двухфакторной аутентификации (2FA / TOTP)

Где: новый раздел в `app/(app)/settings/` (рядом с connect-exchange) + сервис `lib/api/services/totp.ts`. Бэкенд — чистый TOTP (приложение-аутентификатор), **email/SMS не используются**.

### 2.1 Enroll (включение)
- `POST /api/v1/auth/2fa/enroll` → `{ provisioning_uri, secret, recovery_codes: string[10] }`.
- Показать **QR** из `provisioning_uri` (`otpauth://…`) + сам `secret` для ручного ввода.
- Показать **10 recovery-кодов** — **один раз**: дать «скопировать»/«скачать», явно предупредить «сохраните, больше не покажем».
- `409 Conflict` если 2FA уже включена → сценарий «сначала отключите».

### 2.2 Verify (подтверждение)
- `POST /api/v1/auth/2fa/verify` `{ "code": "<6 цифр>" }` → `{ enabled: true }`. До этого шага 2FA **не активна**.
- **Только TOTP** (recovery-код тут не принимается). `400` — неверный код; `429` — lockout.

### 2.3 Status / Disable
- `GET /api/v1/auth/2fa/status` → `{ enabled }` — для отрисовки состояния.
- `DELETE /api/v1/auth/2fa` → отключить. **Требует step-up** (§1): запросить код, передать `X-Step-Up-Token`.

### 2.4 Технически (context7)
- QR: `qrcode.react` → `import { QRCodeSVG } from "qrcode.react"; <QRCodeSVG value={provisioning_uri} size={192} level="M" />`. Не генерить QR на бэке — он отдаёт строку `otpauth://`.
- Поле кода принимает 6–64 символа (6 цифр TOTP или 16-символьный recovery-код в step-up).

---

## 3. F2 — Live Monitoring Dashboard (AC#7)

Новый экран `app/(app)/monitor` (или вкладка в auto-trade). Источник — `GET /api/v1/live/auto-trade/portfolio` (+ `…/strategies/{config_id}/health` для деталей).

**Поля по каждой стратегии** (`StrategyPortfolioEntryRead`): `strategy_name`, `is_running`, `realized_pnl_usdt`, `unrealized_pnl_usdt`, `margin_used_usdt`, `balance_*`, и **live-KPI**: `win_rate_pct`, `max_dd_pct`, `sharpe_proxy`, `roi_pct`, `health_class`, `sample_size`, **`kpi_as_of`**.

**Учесть:**
- **`kpi_as_of`** — время расчёта KPI. Показывать свежесть («обновлено HH:MM» / «N мин назад»). `null` → стратегия остановлена и снапшота нет → рисовать «—», не «0».
- **Денежная база KPI:** `max_dd_pct` и `roi_pct` считаются от **per-trade notional base, НЕ от equity счёта** (W9-прокси, в описании поля прямо сказано). В UI **подписать знаменатель** (напр. «DD, % от размера сделки»), иначе цифры вводят в заблуждение. Не подавать как «ROI счёта».
- `health_class`: `healthy` / `warning` / `critical` / `insufficient_data` — цветовая индикация; `insufficient_data` (мало сделок) показывать нейтрально.
- Сводка портфеля: `total_realized_pnl_usdt`, `total_unrealized_pnl_usdt`, `total_open_positions`, `total_running_strategies`, `portfolio_max_dd_pct`.

**Контролы (учесть gating §1):** `play`/`play-all`/`stop`/`stop-all`/`close-positions`. **`play` — gated step-up'ом** при включённой 2FA.

---

## 4. F5 — SSE live-консьюмер risk-событий

`GET /api/v1/events/stream` — Server-Sent Events. Питает Live Monitor (обновление без поллинга) и тосты.

**Типы событий** (SSE `event:` = тип, `data:` = JSON `{ event_type, payload, message }`): `kill_switch_triggered`, `kpi_guard_triggered`, `strategy_auto_paused`, `portfolio_dd_halt`, `data_stale`, `risk_blocked`, `risk_check_degraded`, `position_emergency_closed_unprotected`.

### 4.1 Авторизация SSE — через BFF (важно)
Браузерный `EventSource` **не умеет слать заголовок `Authorization`**. У вас уже BFF-архитектура (куки + Next.js API-роуты), поэтому правильный путь — **прокси-роут**, а не токен в query-string (он утечёт в логи/Referer).

Создать `app/api/events/stream/route.ts` (Next.js Route Handler, runtime `nodejs`), который читает токен из cookie, дёргает бэковый `/events/stream` с `Authorization: Bearer …` и **пайпит поток** обратно (context7 / Next.js streaming):
```ts
export const runtime = "nodejs";
export async function GET() {
  const token = /* достать access-token из cookie/сессии */;
  const upstream = await fetch(`${BACKEND}/api/v1/events/stream`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
    // @ts-expect-error — стримим тело ответа
    cache: "no-store",
  });
  return new Response(upstream.body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-store, no-transform",
      Connection: "keep-alive",
    },
  });
}
```
Браузер: `new EventSource("/api/events/stream")` (same-origin, заголовки не нужны).

### 4.2 Учесть
- **Лимит стримов:** на пользователя ограничение одновременных соединений (по умолчанию 5/воркер). Превышение → **429**. Держать **один** общий EventSource на вкладку (через стор), не плодить по компонентам.
- **Reconnect с backoff** при разрыве; на 429 — не долбить реконнектом.
- События — «живые подсказки», не источник истины: при `kpi_guard_triggered`/`portfolio_dd_halt`/`kill_switch_triggered` — тост + рефетч `/portfolio` (статусы стратегий могли смениться на paused).

---

## 5. Обработка ошибок (единая матрица)

| Код | Когда | Что показать |
|---|---|---|
| **400** | неверный TOTP-код в `/verify` или `/step-up` | «Неверный код» |
| **403** | gated-действие без валидного `X-Step-Up-Token` (2FA включена) | запустить step-up flow (§1) |
| **403** | step-up-токен уже использован / истёк / чужой | перезапросить код (минтить заново) |
| **409** | `enroll` при уже включённой 2FA | «2FA уже включена, сначала отключите» |
| **429** | lockout 2FA (после N неверных кодов) **или** превышен лимит SSE-стримов | показать `Retry-After`, заблокировать ввод/реконнект на это время |

Заголовок **`Retry-After`** (секунды) есть в 429 — использовать для таймера/блокировки кнопки.

---

## 6. F3 — AI Forecast Catalogue (trader-UI, AC#1)

Вынести каталог из админки в трейдерский раздел. Эндпоинты в контракте: `GET /ai-backtests/ai-forecast-catalogue`, `…/metrics-schema`. Нужны: фильтры по symbol/timeframe, метрики (Win/Sharpe/MaxDD + **Delta-vs-Baseline**), действие **«привязать прогноз к стратегии»** (не только в backtest-билдер, как сейчас).

---

## 7. Технические заметки и приёмка

- **Контракт уже синхронизирован** в репо фронта; при подтягивании бэка — `npm run gen:api-types` (Node ≥18). Все новые типы (`TotpEnrollResponse`, `StepUpResponse`, `StrategyPortfolioEntryRead.kpi_as_of`, и т.д.) уже в `lib/api/openapi-types.ts`.
- **Новые BFF-роуты:** если ваш `apiRequest`/клиент ходит на бэк через Next.js `/api/*`-прокси (как `auth.ts`), под новые эндпоинты (2FA, step-up, portfolio, events) могут понадобиться соответствующие route-handlers/прокси — заложите.
- **Безопасность (S2/S3 из ревью):** SSE-токен — только через cookie/BFF, **никогда не в query-string**; куки — `httpOnly`+`Secure`+`SameSite`. CORS в проде — явные origins (не `*`).
- **Не логировать** secret/recovery-коды/step-up-токены в консоль/телеметрию.

### Acceptance-чеклист
- [ ] F4: enroll(QR+recovery) → verify → status → disable(step-up); 409/400/429 обработаны.
- [ ] Step-up interceptor: gated-действие без 2FA — без изменений; с 2FA — модалка кода → повтор с `X-Step-Up-Token`; одноразовость токена учтена.
- [ ] F2: Live Monitor с live-KPI, `kpi_as_of`-свежестью и подписанным знаменателем DD/ROI; контролы play/stop/close (play под step-up).
- [ ] F5: один EventSource через BFF-прокси; reconnect/backoff; лимит 5 (429) обработан; risk-события → тосты + рефетч.
- [ ] F3: трейдерский каталог с фильтрами, Delta-vs-Baseline и привязкой к стратегии.
- [ ] Матрица ошибок (§5) реализована, `Retry-After` учтён.

---

## Приложение — точные контракты

```
POST /api/v1/auth/2fa/enroll   → { provisioning_uri: string, secret: string, recovery_codes: string[] }
POST /api/v1/auth/2fa/verify   { code: string(6..64) } → { enabled: true } | 400 | 429
GET  /api/v1/auth/2fa/status   → { enabled: boolean }
POST /api/v1/auth/2fa/step-up  { code } → { step_up_token: string, expires_in: number } | 400 | 429
DELETE /api/v1/auth/2fa        (X-Step-Up-Token) → { enabled: false }
GET  /api/v1/live/auto-trade/portfolio              → { strategies: StrategyPortfolioEntryRead[], total_*… }
GET  /api/v1/live/auto-trade/strategies/{id}/health → live KPI одной стратегии
GET  /api/v1/events/stream                          → text/event-stream (через BFF-прокси)
gated (X-Step-Up-Token при включённой 2FA):
  POST /api/v1/live/auto-trade/play
  PUT  /api/v1/live/auto-trade/config
  POST /api/v1/exchange/accounts
  DELETE /api/v1/auth/2fa
```

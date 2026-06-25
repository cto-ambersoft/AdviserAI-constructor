# Telegram-уведомления о сделках — Design Doc

Статус: **Phase 1 реализована** (TDD, см. §13 чеклист — все пункты закрыты)
Дата: 2026-06-15
Версия схемы: v1 (настройки на пользователя, привязка через webhook, общий бот сервиса)

> **Что уже в коде (Phase 1):**
> - Модели + миграция `20260615_0027` — `app/models/telegram_notification_settings.py`, `app/models/telegram_notification_delivery.py`
> - Настройки `telegram_*` — `app/core/config.py`, `.env.example`
> - Клиент + форматтер — `app/services/notifications/telegram.py`, `formatting.py`
> - Диспетчер + привязка — `app/services/notifications/service.py`
> - Cron-задача `dispatch_trade_notifications` — `app/worker/tasks.py`
> - API + webhook — `app/api/v1/endpoints/live.py`, `telegram_webhook.py`, `app/schemas/notifications.py`, lifespan в `app/main.py`
> - Тесты: 33 (unit + integration + endpoint), `openapi.json` перегенерирован
>
> Чтобы включить в проде: задать `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`,
> `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_PUBLIC_BASE_URL` в `.env`. Без токена фича —
> полный no-op.

---

## 1. Цель

Пользователь автоторговли получает сообщение в Telegram, когда движок
открывает/закрывает сделку (и опционально — на риск-события). Пользователь
сам подключает свой Telegram и управляет тем, что ему присылать.

Требования заказчика: **надёжно**, **дёшево и быстро в реализации**.
Поэтому решение максимально переиспользует то, что уже есть в сервисе, и не
трогает торговый hot-path.

Нефункциональные:
- Доставка не должна влиять на исполнение сделок (никаких внешних HTTP-вызовов
  внутри торговых транзакций).
- Переживает рестарты процессов без потери/дублей уведомлений.
- Без новых тяжёлых зависимостей (Telegram дёргается через уже имеющийся `httpx`).

---

## 2. Как это ложится на текущую архитектуру

Сервис уже даёт почти всё необходимое:

| Что нужно | Что уже есть |
|---|---|
| Источник событий «новая сделка» | Каждое событие движка пишется через `AutoTradeService._emit_event(...)` → строка в таблице `auto_trade_events` (`app/services/auto_trade/service.py:5133`). `position_opened` уже содержит symbol/trend/entry/qty/sl/tp/confidence (`service.py:3873`). |
| Надёжный, транзакционный «outbox» | Сама таблица `auto_trade_events` (модель `app/models/auto_trade_event.py`): `user_id`, `config_id`, `position_id`, `event_type`, `level`, `message`, `payload (JSON)`, `created_at`. |
| Фоновый исполнитель «раз в минуту» | Taskiq + `RedisStreamBroker` (`app/worker/broker.py`), уже 6 cron-свипов вида `@broker.task(schedule=[{"cron": "* * * * *"}])` (`app/worker/tasks.py`). |
| Конфиг/секреты | pydantic `BaseSettings` из `.env` (`app/core/config.py`); Fernet-шифр `SecretCipher` (`app/services/secrets.py`). |
| HTTP-клиент | `httpx>=0.28` уже в зависимостях. Telegram-SDK **не требуется**. |
| Место для API | CRUD автоторговли в `app/api/v1/endpoints/live.py`, защищённый роутер с `get_current_user`. |

**Главная идея:** `auto_trade_events` — это уже готовый durable outbox. Достаточно
асинхронно вычитывать новые «уведомляемые» события и слать в Telegram. Ничего
встраивать в торговый код не нужно.

---

## 3. Зафиксированные решения

1. **Общий бот сервиса.** Оператор создаёт ОДИН бот через @BotFather, токен в
   `TELEGRAM_BOT_TOKEN`. Пользователи не заводят своих ботов.
2. **Настройки на пользователя (v1).** Одна привязка Telegram на юзера, общий
   вкл/выкл + тогглы типов событий. Применяется ко всем его стратегиям.
   Гранулярность на конфиг — фаза 2 (см. §13).
3. **Привязка через webhook.** Публичный route + `setWebhook` на старте.
   (Long-polling — опциональный fallback для локалки, см. §9.4.)
4. **Доставка через outbox-поллер.** Cron-задача раз в минуту читает новые
   события и шлёт. Не enqueue из hot-path. Идемпотентность — отдельной таблицей
   доставок. Near-real-time — фаза 2 (см. §13).

---

## 4. Модель данных

Две новые таблицы. Миграция: следующий номер по конвенции — `20260615_0027_add_telegram_notifications.py`
(последняя — `20260605_0026`).

### 4.1 `telegram_notification_settings` (одна строка на юзера)

```
telegram_notification_settings
├─ id                     PK
├─ user_id                FK users.id, UNIQUE, NOT NULL, index
├─ chat_id                BIGINT, NULL  (заполняется после привязки)
├─ enabled                BOOL, NOT NULL, default false  (мастер-выключатель)
├─ notify_on_open         BOOL, NOT NULL, default true
├─ notify_on_close        BOOL, NOT NULL, default true
├─ notify_on_risk         BOOL, NOT NULL, default false (kill-switch/auto-pause)
├─ link_code              VARCHAR(32), NULL, index  (одноразовый код привязки)
├─ link_code_expires_at   TIMESTAMPTZ, NULL
├─ linked_at              TIMESTAMPTZ, NULL
├─ created_at / updated_at (TimestampMixin)
```

Заметки:
- `chat_id` — `BIGINT` (Telegram chat id может быть > 2^31; для групп — отрицательный).
- Привязан считается юзер, у которого `chat_id IS NOT NULL`.
- Хранится только chat_id (не секрет). Токен бота — общий, в env. Шифровать
  нечего (если в фазе 2 появятся пользовательские токены — шифруем через
  существующий `SecretCipher`).

### 4.2 `telegram_notification_deliveries` (идемпотентность + ретраи)

```
telegram_notification_deliveries
├─ event_id     PK, FK auto_trade_events.id   (1 событие → максимум 1 уведомление)
├─ user_id      FK users.id, NOT NULL, index
├─ status       VARCHAR(16) NOT NULL  (pending | sent | failed | skipped)
├─ attempts     INT NOT NULL default 0
├─ last_error   TEXT NULL
├─ created_at / updated_at
└─ sent_at      TIMESTAMPTZ NULL
```

`event_id` — первичный ключ, потому что каждое событие принадлежит ровно одному
`user_id` и рождает максимум одно уведомление. Это даёт **at-least-once с
идемпотентностью**: вставка строки доставки = факт того, что событие обработано;
повторный проход поллера её не дублирует.

> Альтернатива, которую отвергли: один глобальный курсор `last_event_id`. Проще,
> но падение отправки одному юзеру блокирует продвижение курсора для всех, а ретрай
> отдельного события невозможен. Таблица доставок дороже на одну вставку, но решает
> и идемпотентность, и поштучный ретрай — это стоит того при требовании «надёжно».

---

## 5. Изменения в настройках (`app/core/config.py` + `.env`)

```python
# app/core/config.py (Settings)
telegram_bot_token: str = ""                # токен от @BotFather; "" = фича выключена
telegram_webhook_secret: str = ""           # секрет для X-Telegram-Bot-Api-Secret-Token
telegram_public_base_url: str = ""          # напр. https://api.ambercore... для setWebhook
telegram_bot_username: str = ""             # для deep-link; либо тянем через getMe на старте
telegram_link_code_ttl_seconds: int = 900   # 15 минут
telegram_notify_batch_size: int = 200       # лимит событий за один прогон поллера
telegram_notify_max_attempts: int = 5
```

`.env` / `.env.example` — добавить те же ключи. Если `telegram_bot_token` пуст —
вся фича no-op (поллер выходит сразу, эндпоинты возвращают 503/"not configured").

---

## 6. Telegram-клиент — `app/services/notifications/telegram.py`

Тонкая обёртка над Bot API через `httpx.AsyncClient`. Без SDK.

Методы (все — POST на `https://api.telegram.org/bot<token>/<method>`):
- `send_message(chat_id, text, *, parse_mode="HTML", disable_web_page_preview=True)`
- `set_webhook(url, secret_token, allowed_updates=["message"])`
- `delete_webhook()`
- `get_me()` → для `telegram_bot_username`

Обработка ответов (Bot API всегда отдаёт `{"ok": bool, ...}`):
- `ok=true` → успех.
- HTTP 429 / `error_code=429` → взять `parameters.retry_after` (сек), вернуть как
  «retry later» — поллер отложит это событие на следующий прогон (не спим в задаче).
- HTTP 403 (`Forbidden: bot was blocked by the user`) или
  `chat not found` → пометить доставку `skipped` и **снять привязку** юзера
  (`chat_id=NULL`, `enabled=false`) — он переподключится заново.
- Прочие 4xx → `failed`, increment attempts; после `max_attempts` → `failed` навсегда.
- Таймаут/сеть/5xx → `failed`, ретрай на следующем прогоне.

Лимиты Telegram (справочно): ~30 msg/s суммарно, ~1 msg/s в один чат. При батче
раз в минуту это недостижимый потолок; отдельный rate-limit не нужен в v1.

### Формат сообщения (parse_mode=HTML)

HTML проще MarkdownV2 (экранируем только `& < >`). Шаблоны по типу события:

**Открытие** (`position_opened`):
```
🟢 LONG BTC/USDT
Стратегия: <strategy_name>
Вход: 64 250.0
Объём: 0.012
SL: 63 600.0  ·  TP: 65 500.0
Уверенность: 78%
```
(для SHORT — 🔴; цифры/символы берём из `event.payload`).

**Закрытие** (`position_closed_*`, `position_manual_closed`): символ, сторона,
причина закрытия, цена выхода, реализованный PnL (если есть в payload/ledger).

**Частичный TP** (`multi_tp_reconciled_via_rest`): уровень TP, закрытая доля, новый SL.

**Риск** (`kill_switch_triggered`, `strategy_auto_paused`): что сработало, по какой
стратегии, что предпринято.

Маппинг `event_type → (toggle, шаблон)` держим одной таблицей-словарём в модуле
форматтера — добавление новых типов = одна строка.

---

## 7. Диспетчер (cron-задача) — `app/worker/tasks.py`

Новая задача по образцу существующих свипов:

```python
@broker.task(
    task_name="app.worker.tasks.dispatch_trade_notifications",
    schedule=[{"cron": "* * * * *", "schedule_id": "telegram_notify_every_minute"}],
)
async def dispatch_trade_notifications() -> dict[str, int]:
    async with AsyncSessionFactory() as session:
        return await telegram_notify_service.dispatch_pending(session=session)
```

Алгоритм `dispatch_pending`:

1. Если `telegram_bot_token` пуст → вернуть `{"skipped": 1}` и выйти.
2. Выбрать кандидатов:
   ```sql
   SELECT e.* FROM auto_trade_events e
   LEFT JOIN telegram_notification_deliveries d ON d.event_id = e.id
   WHERE e.event_type IN (:notifiable_types)
     AND e.created_at > now() - interval '30 minutes'   -- окно догоняния
     AND (d.event_id IS NULL OR (d.status = 'failed' AND d.attempts < :max_attempts))
   ORDER BY e.id
   LIMIT :batch_size
   ```
   Окно (30 мин) ограничивает скан; всё старше считаем «протухшим» и не шлём
   (старт после долгого простоя не завалит юзера историей). Параметр настраиваемый.
3. Для каждого события:
   - Загрузить настройки юзера (`telegram_notification_settings` по `user_id`).
   - Пропустить (status=`skipped`), если: не привязан, `enabled=false`, или
     соответствующий тоггл (`notify_on_open/close/risk`) выключен.
   - Отформатировать сообщение, отправить через клиент.
   - Записать/обновить строку доставки: `sent` (+`sent_at`) / `failed` (+`attempts`,
     `last_error`) / `skipped`.
   - Коммит после каждого события (или небольшими батчами) — чтобы падение в
     середине не теряло уже отправленное.
4. Вернуть сводку `{"polled","sent","skipped","failed","errors"}` (как другие
   свипы логируют ненулевую сводку).

Свойства:
- **Идемпотентно**: строка доставки = «обработано»; повтор не дублирует.
- **Durable на рестарт**: курсора нет, состояние — в таблице доставок; после
  рестарта поллер сам подберёт необработанные события из окна.
- **Изоляция от торговли**: всё в отдельной задаче воркера, ноль изменений в
  `_emit_event` и торговом пути.
- **Задержка**: до ~60с (гранулярность cron). Для уведомления о сделке приемлемо.

> Многопроцессность: если воркеров несколько, два инстанса могут выбрать одно
> событие. Защита — `INSERT ... ON CONFLICT (event_id) DO NOTHING` перед отправкой
> (захват строки доставки в статусе `pending`), либо `SELECT ... FOR UPDATE SKIP LOCKED`
> по образцу `_with_for_update_skip_locked` в сервисе. В v1 при одном воркере не критично,
> но `ON CONFLICT` стоит заложить сразу — он дешёвый.

---

## 8. Уведомляемые типы событий

По умолчанию (тоггл `notify_on_open`):
- `position_opened`
- `position_synced_open_from_exchange`

По умолчанию (тоггл `notify_on_close`):
- `position_closed_on_opposite_trend`
- `position_manual_closed`
- `position_reconciled_closed_via_rest`
- `position_marked_closed_from_exchange_state`
- `multi_tp_reconciled_via_rest` (частичный TP)

Опционально (тоггл `notify_on_risk`, по умолчанию off):
- `kill_switch_triggered`
- `strategy_auto_paused`
- `kpi_guard_triggered`
- `position_emergency_closed_unprotected`

Список — константа в сервисе нотификаций; расширяется одной строкой.

---

## 9. Привязка Telegram (webhook)

### 9.1 Поток (sequence)

```
Frontend            API (app_trade)                 Telegram            User
   │  POST /live/notifications/telegram/link            │                 │
   │ ─────────────────────────▶                         │                 │
   │   { deep_link, code, expires_at }                   │                 │
   │ ◀─────────────────────────                          │                 │
   │  показывает кнопку "Подключить Telegram" (deep_link)│                 │
   │                                                     │   тап на ссылку │
   │                                                     │ ◀───────────────│
   │                                          /start <code>  (от Telegram) │
   │                       webhook POST /telegram/webhook/<secret>         │
   │                       ◀──────────────────────────────                │
   │              матч code→user, сохранить chat_id, ответ "Подключено ✅" │
   │                       ──────────────────────────────▶ sendMessage    │
   │  GET /live/notifications/telegram → linked=true     │                 │
```

### 9.2 Генерация кода

`POST /live/notifications/telegram/link` (auth):
- Сгенерировать криптослучайный `code` (напр. `secrets.token_urlsafe(12)`).
- Записать в настройки юзера: `link_code`, `link_code_expires_at = now()+ttl`.
- Вернуть:
  ```json
  {
    "deep_link": "https://t.me/<bot_username>?start=<code>",
    "code": "<code>",
    "expires_at": "2026-06-15T12:15:00Z"
  }
  ```
Deep-link с `?start=<code>` — Telegram при открытии подставит `/start <code>`.

### 9.3 Webhook-эндпоинт

`POST /api/v1/telegram/webhook/<secret>` — **публичный** (вне `get_current_user`).
Регистрируется отдельным роутером (как `auth`/`internal`, не под protected).

Безопасность:
- Путь содержит несекретный, но неугадываемый сегмент (`telegram_webhook_secret`).
- Проверяем заголовок `X-Telegram-Bot-Api-Secret-Token` == `telegram_webhook_secret`
  (Telegram шлёт его, если задать `secret_token` в `setWebhook`).
- Тело — Telegram `Update`. Берём `update.message.text`, если это `/start <code>`.

Логика:
- Распарсить `code` из `/start <code>`.
- Найти настройки с `link_code == code` и не истёкшим `link_code_expires_at`.
- Если найдено: сохранить `chat_id = message.chat.id`, `linked_at = now()`,
  обнулить `link_code`, выставить `enabled = true`. Ответить юзеру в чат
  «✅ Telegram подключён, уведомления включены».
- Если код не найден/истёк: ответить «Ссылка устарела, сгенерируйте заново».
- Всегда возвращать `200 OK` (иначе Telegram будет ретраить).

### 9.4 Установка webhook

На старте приложения (в lifespan, `app/main.py`), если токен задан:
- (опц.) `getMe` → заполнить `telegram_bot_username` для deep-link.
- `setWebhook(url=<telegram_public_base_url>/api/v1/telegram/webhook/<secret>,
  secret_token=<secret>, allowed_updates=["message"])`.

Делать это идемпотентно и не валить старт при ошибке (по образцу
`install_auto_trade_runtime`, который оборачивается в try/except в lifespan).

**Fallback для локалки без публичного URL:** маленькая фоновая задача
long-polling `getUpdates` (как `rawapibot.py` из доков python-telegram-bot).
Включается флагом; в проде — webhook. В v1 можно не реализовывать polling вовсе,
а для локального теста привязки временно вставлять chat_id вручную (см. §11 test).

---

## 10. API-эндпоинты (контракты)

Все, кроме webhook, — под `get_current_user`, добавляются в `live.py`
(префикс `/api/v1/live`). Webhook — отдельный публичный роутер.

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/live/notifications/telegram/link` | сгенерировать код + deep-link |
| `GET`  | `/live/notifications/telegram` | текущие настройки: `linked`, `enabled`, тогглы |
| `PUT`  | `/live/notifications/telegram` | обновить `enabled` и тогглы |
| `POST` | `/live/notifications/telegram/test` | отправить тестовое сообщение (проверка привязки) |
| `DELETE` | `/live/notifications/telegram` | отвязать (`chat_id=NULL`, `enabled=false`) |
| `POST` | `/telegram/webhook/<secret>` | приём апдейтов Telegram (публичный) |

Pydantic-схемы — в `app/schemas/` (напр. `notifications.py`):
`TelegramSettingsOut`, `TelegramSettingsUpdate`, `TelegramLinkOut`.

Пример `GET` ответа:
```json
{
  "linked": true,
  "enabled": true,
  "notify_on_open": true,
  "notify_on_close": true,
  "notify_on_risk": false,
  "linked_at": "2026-06-15T12:01:00Z"
}
```

---

## 11. Надёжность и крайние случаи

| Случай | Поведение |
|---|---|
| Юзер заблокировал бота (403) | доставка `skipped`, привязка снимается (`chat_id=NULL`, `enabled=false`) |
| Rate limit (429) | читаем `retry_after`, событие переносится на следующий прогон |
| Рестарт воркера в середине батча | отправленные уже имеют строку `sent`; неотправленные подберутся снова |
| Дубли при нескольких воркерах | `INSERT ... ON CONFLICT (event_id) DO NOTHING` захватывает событие до отправки |
| Долгий простой сервиса | окно 30 мин отсекает старьё — юзера не завалит историей |
| Токен не задан | вся фича no-op; поллер и эндпоинты выходят сразу |
| Истёкший код привязки | webhook отвечает «сгенерируйте заново» |
| Порядок сообщений | `ORDER BY e.id` сохраняет хронологию open→close в пределах прогона |

---

## 12. Безопасность

- **Токен бота** — только в env, не в БД, не в логах.
- **Webhook**: неугадываемый путь + проверка `X-Telegram-Bot-Api-Secret-Token`.
  Парсим только `message.text`; всё прочее игнорируем. Всегда `200`.
- **Код привязки**: криптослучайный, одноразовый, TTL 15 мин, обнуляется после
  использования.
- **chat_id** — не секрет, но привязан к `user_id`; смена chat_id только через
  свежий код, инициированный аутентифицированным юзером.
- Никаких пользовательских данных в URL/логах сверх необходимого.

---

## 13. Объём, фазы, оценка

### Фаза 1 (этот док) — ~1–1.5 дня

- [ ] Модели `telegram_notification_settings`, `telegram_notification_deliveries`
- [ ] Миграция `20260615_0027_add_telegram_notifications.py`
- [ ] Настройки в `config.py` + `.env(.example)`
- [ ] `app/services/notifications/telegram.py` — клиент + форматтер
- [ ] `app/services/notifications/service.py` — `dispatch_pending`, линк/тогглы
- [ ] cron-задача `dispatch_trade_notifications` в `tasks.py`
- [ ] Эндпоинты в `live.py` + публичный webhook-роутер + схемы
- [ ] `setWebhook`/`getMe` в lifespan (`main.py`)
- [ ] Тесты (см. §14)

### Фаза 2 (по желанию, позже)

- **Гранулярность на стратегию**: поле `notifications_enabled` в `auto_trade_config`
  (или таблица оверрайдов), фильтр в диспетчере по `event.config_id`.
- **Near-real-time**: вместо/в дополнение к cron — enqueue Taskiq-задачи
  `notify_event(event_id)` сразу после коммита события (точечный хук после
  `_emit_event`-commit), cron остаётся как «подбиратель» пропусков.
- **Другие каналы**: Email/Slack по тому же outbox-механизму (диспетчер
  абстрагируется от канала).
- **Богатые сообщения**: PnL/график позиции, инлайн-кнопка «закрыть позицию».

---

## 14. Тестирование

- **Unit**: форматтер (open/close/partial/risk → текст, экранирование HTML);
  выбор кандидатов диспетчером (тогглы, окно, дедуп); парсинг `/start <code>`.
- **Клиент**: мок `httpx` — ok / 403 / 429(retry_after) / 5xx → корректные статусы
  доставки и снятие привязки на 403.
- **Интеграция (sqlite/pg)**: эмитим `auto_trade_events`, прогоняем `dispatch_pending`,
  проверяем строки доставок и идемпотентность повторного прогона.
- **Привязка**: POST `link` → подделанный webhook-апдейт `/start <code>` →
  `chat_id` сохранён, `enabled=true`, код обнулён.
- **Smoke (ручной)**: реальный тест-бот, `test`-эндпоинт шлёт сообщение в личный чат.

---

## 15. Сводка диаграммы потока

```
движок ──_emit_event──▶ auto_trade_events (durable outbox)
                              │
        cron "* * * * *" (telegram_notify_every_minute)
                              ▼
        dispatch_pending:
          выбрать новые notifiable-события (окно, дедуп через deliveries)
          → настройки юзера (привязан? enabled? нужный тоггл?)
          → формат сообщения → Telegram sendMessage (httpx)
          → запись deliveries (sent/failed/skipped)
                              │
                              ▼
                        Telegram Bot API

привязка:  /link (код+deep-link) → юзер жмёт → /start <code>
           → webhook /telegram/webhook/<secret> → сохранить chat_id
```

# M4 Remediation Plan — блокеры + невыполненные обещания

> **Источник:** [reports/M4_AUDIT.md](../reports/M4_AUDIT.md) (аудит от 2026-06-19).
> **Цель:** закрыть каждый **блокер** и каждое **невыполненное обещание с нашей стороны** из аудита.
> **Сервисы:** `constructor` (FastAPI) · `core` (NestJS/Mastra) · `constructor-front` (Next.js).
> **Режим составления:** read-only (plan mode); код не менялся. Технические решения сверены с офиц. документацией через context7 (Fernet/Argon2id, pyotp, sse-starlette).
> **⚠️ Реальные деньги:** mainnet-счёт 17. Phase 0 (security) и Phase 1 (execution integrity) — fail-fast, идут первыми.

---

## Архитектурные решения (зафиксировать перед стартом)

1. **Переиспользовать существующие зависимости и паттерны, не тянуть новое:**
   - KDF для ключа шифрования — `pwdlib[argon2]` **уже в зависимостях** (`pyproject.toml`); Argon2id вместо `hashlib.sha256` (context7: cryptography предписывает KDF, не сырой хэш).
   - Login-throttle — небольшой **Redis fixed-window** лимитер (redis уже dep), а не `slowapi`; согласован с TOTP-lockout паттерном.
   - Краны — taskiq `@broker.task(schedule=[...])`, как в `app/worker/tasks.py`.
   - FSM — ручной `Enum` + `VALID_TRANSITIONS`, как `position/state_machine.py` и `promotion/state_machine.py`.
   - SSE — `sse-starlette` уже подключён (`events.py`); расширяем `STREAMABLE_EVENTS`, не строим новый канал.
2. **Step-up — единый dependency** `RequireStepUp` (`app/api/deps.py:81`). Любая критичная мутация вешается на него.
3. **«Чистое решение + side-effect отдельно»** — как `kpi_guard.py` (decision) ↔ `service._auto_pause_strategy` (effect). Все новые детекторы/гейты — чистые функции + тонкий сервис-слой.
4. **Контракт фронта** регенерируется после правок роутов (Node 20): `npm run gen:api-types` (Appendix B closeout-плана).
5. **Развилки решены заказчиком (2026-06-20):** Q1 email-confirm — **реализовать** (Resend); Q2 sandbox — **стадия верификации работоспособности** перед live; Q3 worker isolation — **НЕ делать**; Q4 ai_trend — **реализовать агрегацию**; Q5 conflicting `net`/`replace` — **убрать**. См. раздел «Решения» внизу.
6. **Email-провайдер — Resend** (выбран как самый простой + бесплатный): free 3,000/мес · 100/день (постоянно), async-native `resend.Emails.send_async`, встроенный `idempotency_key` (совпадает с паттерном notifications-outbox), конфиг через `RESEND_API_KEY`. Предпосылка: верифицированный домен отправки (`ambersoft.llc` — есть). Сравнение: Brevo (300/день, сложнее), Amazon SES (дёшево, но тяжёлая настройка) — отклонены в пользу простоты.

---

## Граф зависимостей (укрупнённо)

```
Phase 0 SECURITY (независимы, можно параллелить)
   T1 startup-guard ─┐
   T2 KDF ───────────┤ (T1,T2 оба трогают core/security.py, config.py → сериализовать)
   T3 step-up keys   │
   T4 /encrypt auth  │
   T5 login-throttle │
   T6 CORS prod      ┘
        │
Phase 1 EXECUTION INTEGRITY
   T7 watcher consumer (independent)
   T8 lifecycle default=sandbox (independent)
        │
Phase 2 AI LOOP            Phase 3 RISK/PORTFOLIO        Phase 4 TRANSPARENCY/UX
   T9  weight rebind          T12 merged-equity DD          T15 live SSE KPI
   T10 real-outcome accuracy  T13 conflicting net/replace   T16 forecast→live (T16a→T16b)
       (T10a contract → T10b) T14 freshness acts            T17 trader AI dashboard (← T9,T10)
   T11 ai_trend aggregate
        │
Phase 5 GOVERNANCE / AUDIT / DECISIONS
   T18 safe enablement+calibration   T19 lifecycle audit table
   T20 email-confirm (Resend)        T21 worker isolation ❌НЕ ДЕЛАЕМ   T22 docs reconcile
```

---

# PHASE 0 — Security блокеры (fail-fast, реальные деньги)

## T1 — Startup-guard на дефолтные секреты · 🔴 блокер (S4)
**Description:** Приложение молча стартует с плейсхолдерными `secret_key` / `jwt_secret_key` / `encryption_key` (`config.py:32-34`). Добавить fail-fast валидацию при старте.

**Acceptance criteria:**
- [ ] При значении-плейсхолдере любого из трёх ключей (или длине/энтропии ниже порога) приложение **не стартует** в non-debug режиме (raise при создании `Settings` или в `lifespan`).
- [ ] В `debug=True` (локально) допускается предупреждение вместо краха.
- [ ] `.env.example` документирует генерацию (`Fernet.generate_key()` для `encryption_key`).

**Verification:**
- [ ] `uv run pytest tests/unit/test_config_guard.py` (новый): плейсхолдер → `ValueError`; валидные → OK.
- [ ] Ручной: запуск без env-переопределений падает с понятной ошибкой.

**Dependencies:** None · **Files:** `app/core/config.py`, `app/main.py`, `tests/unit/test_config_guard.py`, `.env.example` · **Scope:** S

## T2 — Стойкая деривация ключа шифрования · 🔴 блокер (S5)
**Description:** `SecretCipher._normalize_key` (`security.py:21-32`) SHA256-деривит **любую** строку в валидный Fernet-ключ — низкоэнтропийный ключ не падает и легко перебирается. context7 (cryptography): для ключа из строки использовать **Argon2id** (или PBKDF2HMAC, соль 16+, 1.2M итераций), не сырой SHA256.

**Acceptance criteria:**
- [ ] Принимается **только** валидный 44-симв. url-safe base64 Fernet-ключ; иначе — ошибка (см. T1). **Либо** (если допускаем парольный режим) деривация через Argon2id с фиксированной солью из отдельного env и явным флагом — задокументировать выбор в ADR.
- [ ] Существующие зашифрованные значения остаются дешифруемыми (миграция/совместимость продумана; при смене схемы — план ре-шифрования через `MultiFernet`).
- [ ] `hashlib.sha256`-fallback удалён.

**Verification:**
- [ ] `uv run pytest tests/unit/test_secret_cipher.py`: невалидный ключ → reject; round-trip encrypt/decrypt; (если KDF) детерминизм.
- [ ] Ручной: подтвердить чтение реального `exchange_credentials` после изменения (на demo-копии БД).

**Dependencies:** T1 (общий config) · **Files:** `app/core/security.py`, `app/core/config.py`, `docs/adr/0001-key-derivation.md`, `tests/unit/test_secret_cipher.py` · **Scope:** S
**⚠️ Риск:** смена деривации меняет ключ → потеря доступа к ciphertext. Перед мержем — план совместимости (`MultiFernet([new, old])`).

## T3 — Step-up на смену/удаление ключа биржи · 🔴 блокер (S1/S2)
**Description:** `PATCH`/`DELETE /accounts/{id}` используют `CurrentUser`, не `RequireStepUp` (`exchange.py:71-96`), хотя договор называет «смену exchange-key» критической, а `config.py:38-41` это даже декларирует.

**Acceptance criteria:**
- [ ] `update_exchange_account` и `delete_exchange_account` зависят от `RequireStepUp`.
- [ ] Фронт прогоняет PATCH/DELETE ключа через step-up-интерсептор (`lib/api/step-up.ts`) с модалкой.
- [ ] Контракт регенерирован.

**Verification:**
- [ ] `uv run pytest tests/integration/test_exchange_accounts.py`: PATCH/DELETE без step-up → 401/403; со step-up → OK.
- [ ] Ручной (фронт): смена ключа открывает step-up-модалку.

**Dependencies:** None · **Files:** `app/api/v1/endpoints/exchange.py`, `constructor-front/.../exchange` API-клиент, `tests/integration/test_exchange_accounts.py` · **Scope:** S

## T4 — Закрыть `/encrypt`-оракул · 🔴 блокер (S6)
**Description:** `POST /encrypt` (`exchange.py:23-30`) без auth — публичный crypto-оракул на серверном ключе.

**Acceptance criteria:**
- [ ] Эндпоинт либо удалён (если не нужен фронту — проверить использование), либо закрыт `RequireStepUp` + ownership.
- [ ] Если удалён — фронт/клиенты не ломаются (grep использования).

**Verification:**
- [ ] `uv run pytest tests/integration/test_exchange_accounts.py::test_encrypt_requires_auth` (или 404 если удалён).
- [ ] `grep -rn "/encrypt" constructor-front` пуст или ведёт через auth.

**Dependencies:** None · **Files:** `app/api/v1/endpoints/exchange.py`, тесты · **Scope:** XS

## T5 — Rate-limit логина + per-IP + step-up jti fail-closed · 🔴 блокер (S7/S8)
**Description:** Нет rate-limit на `/signin` (`auth.py:99-123`) и нет per-IP throttle → password-spray. `_consume_step_up_jti` fail-OPEN на ошибке Redis (`deps.py:45-47`) → реюз step-up токена.

**Acceptance criteria:**
- [ ] Redis fixed-window лимитер: ограничение попыток `/signin` и `/2fa/login` per-IP **и** per-email (напр. 10/мин/IP, экспон. backoff); превышение → 429.
- [ ] Step-up jti single-use **fail-CLOSED**: при недоступности Redis критичное действие **отклоняется** (а не пропускается).
- [ ] Лимиты конфигурируемы через `config.py`.

**Verification:**
- [ ] `uv run pytest tests/integration/test_auth_throttle.py`: N+1 попытка → 429; Redis down → step-up reject.
- [ ] Ручной: брутфорс-симуляция упирается в 429.

**Dependencies:** None · **Files:** `app/api/v1/endpoints/auth.py`, `app/api/deps.py`, новый `app/core/ratelimit.py`, `app/core/config.py`, тесты · **Scope:** M

## T6 — Прод-CORS и хардненинг · 🟡 (S9)
**Description:** CORS дефолт `["*"]` (`config.py:90-92`). Убедиться, что прод выставляет явные origins.

**Acceptance criteria:**
- [ ] Дефолт origins в коде сужен (или явный startup-warning, если `*` + не-debug), `.env.example` документирует прод-origins.
- [ ] Проверены прод-env (ambercore-server) на явные origins.

**Verification:**
- [ ] `uv run pytest tests/unit/test_cors_config.py`.
- [ ] Ручной: проверить заголовки CORS на staging.

**Dependencies:** T1 · **Files:** `app/core/config.py`, `app/main.py`, `.env.example` · **Scope:** XS

### ✅ Checkpoint A — Security (после T1–T6)
- [ ] `uv run pytest` зелёный; `uv run mypy app` чисто.
- [ ] Все критичные мутации (play, promote, edit-risk, create/update/delete ключа) требуют step-up.
- [ ] Дефолтные секреты не дают старт; ключ шифрования стойкий.
- [ ] **Review с человеком + прогон на demo-аккаунте перед прод-деплоем.**

---

# PHASE 1 — Execution integrity блокеры

## T7 — Подключить потребителя watcher event-bus · 🔴 блокер (W5b)
**Description:** In-Position Indicator Monitoring считает RSI/MACD/EMA-cross и публикует в Redis `position.indicator_trigger`, но `subscribe_watcher_events`/`handle_watcher_event` (`event_bus.py:55-206`) вызываются **только в тестах** — в проде на триггеры никто не реагирует. Договорный пункт не функционирует.

**Acceptance criteria:**
- [ ] `subscribe_watcher_events(handle_watcher_event)` стартует как долгоживущая фоновая задача в `install_auto_trade_runtime` (`service.py:6181`), с graceful-shutdown (`asyncio.CancelledError`).
- [ ] При срабатывании watcher-правила реально исполняется `tighten_sl` / `close_partial` / `alert` (через order-queue / notifications).
- [ ] Обновить вводящие в заблуждение комментарии `pipeline.py:51-56` / live_tracker, чтобы отражали живой путь.
- [ ] Идемпотентность/дедуп триггеров (не дёргать SL каждый тик) — cooldown.

**Verification:**
- [ ] `uv run pytest tests/unit/test_watcher_event_bus.py tests/integration/test_position_watcher.py`.
- [ ] Интеграционный: синтетический RSI-триггер на открытой позиции → enqueue `replace_sl`/partial-close.
- [ ] Ручной (demo): открыть позицию с watcher-правилом, спровоцировать условие → SL/частичное закрытие наблюдается.

**Dependencies:** None · **Files:** `app/services/auto_trade/service.py`, `app/services/watchers/event_bus.py`, `app/services/sl_tp/pipeline.py`, тесты · **Scope:** M

## T8 — Sandbox = стадия верификации работоспособности перед live · 🔴 блокер (W10e) · **[Q2 решён]**
**Description:** Договор: `Deep Research → Sandbox → KPI Validation → Live`. Сейчас (а) `lifecycle_stage` дефолтит в `"live"` (fail-open, `models/auto_trade_config.py:87-88`); (б) docstring `state_machine.py:14-18` обещает «paper/dry-run», а фактически идут реальные ордера на demo/testnet. **Решение Q2:** sandbox = реальная стадия верификации, что стратегия работоспособна (исполнение на demo/testnet — деньги не настоящие), и **только после прохождения проверки** стратегия выходит в live. То есть demo-исполнение оставляем (это и есть «paper» без реальных денег), но делаем lifecycle честным: верификация → гейт → live.

**Acceptance criteria:**
- [ ] Дефолт колонки → `"sandbox"` (миграция `0036` + ORM `default`/`server_default`); новые стратегии **обязаны** пройти sandbox.
- [ ] Backfill **не трогает** существующие рабочие конфиги (осознанно `live`).
- [ ] Order-path stage-guard (`service.py:4063-4079`) покрывает **все** пути размещения ордера: вне `live` ордера идут только на demo/testnet-аккаунт; добавить тест на каждый путь (`_place_entry_order`, `place_futures_market_order`).
- [ ] Переход sandbox→live разрешён **только** после `gate_passed` (KPI Gate подтверждает работоспособность: min-win-rate / max-DD / min-trades / min-sandbox-days) — связать с существующим `promote_strategy`.
- [ ] Docstring `state_machine.py:14-18` исправлен: «sandbox = верификация на demo/testnet (без реальных денег), не in-process симуляция».

**Verification:**
- [ ] `uv run pytest tests/unit/test_promotion_lifecycle.py`: новый конфиг → `sandbox`; sandbox на real-аккаунте → отказ ордера; promote без `gate_passed` → отказ.
- [ ] Alembic up/down на throwaway PG (:55432, см. memory).
- [ ] Ручной (demo): создать стратегию → sandbox исполняет на demo → пройти гейт → promote (step-up) → live.

**Dependencies:** None · **Files:** `app/models/auto_trade_config.py`, `migrations/versions/20260620_0036_*.py`, `app/services/auto_trade/promotion/state_machine.py`, `app/services/auto_trade/service.py`, тесты · **Scope:** S→M

### ✅ Checkpoint B — Execution integrity (после T7–T8)
- [ ] Watcher-триггеры реально влияют на открытую позицию (end-to-end на demo).
- [ ] Новые стратегии стартуют в sandbox; real-money ордера невозможны вне `live`.
- [ ] Review + demo-прогон.

---

# PHASE 2 — AI feedback loop (адаптивный движок: обещания W2/W3)

## T9 — «Применение весов» реально перепривязывает конфиг · (W3b)
**Description:** `applySuggestion` создаёт orphan-профиль `AW-SUGG-...`, но не делает `updateOne/save` → `AiConfig.agentWeightsId` не меняется, рантайм-веса прежние.

**Acceptance criteria:**
- [ ] При apply (`dry_run=false`) активный `AiConfig` перепривязывается на новый weights-профиль (атомарно), с аудит-записью «кто/когда/откуда».
- [ ] `dry_run=true` — без побочных эффектов.
- [ ] Прогон анализа после apply использует новые веса (интеграционная проверка).

**Verification:**
- [ ] `npm test` (core) — `agent-accuracy.service.spec.ts`: apply → `agentWeightsId` обновлён; dry-run → нет.
- [ ] Ручной: apply → следующий прогон отражает новые веса.

**Dependencies:** None · **Files:** `core/src/analysis/ai-config.service.ts`, `core/src/analysis/agent-accuracy.service.ts`, спеки · **Scope:** S

## T10 — Agent Accuracy на реальном исходе сделки · (W3a) · **split**
**Description:** Сейчас точность считается по синтетическому дневному движению цены (Snowflake `price_history_daily`), а не по реальным сделкам/PnL. Поля `outcomeJoinKey`/`resultSnapshot` мёртвые.

### T10a — Контракт: constructor отдаёт исходы сделок (backend, contract-first)
**Acceptance criteria:**
- [ ] Эндпоинт/выгрузка в `constructor`, отдающая закрытые сделки с привязкой к сигналу/решению (symbol, side, время сигнала, время закрытия, net-PnL, исход win/loss) — переиспользовать W9-ledger / `build_position_trace`.
- [ ] Авторизация (internal_api_key, как analysis_proxy).
- [ ] Контракт задокументирован (OpenAPI).

**Verification:** `uv run pytest tests/integration/test_outcomes_api.py`; ручной curl возвращает реальные сделки.
**Dependencies:** None · **Files:** `app/api/v1/endpoints/*`, `app/services/auto_trade/*`, тесты · **Scope:** M

### T10b — core потребляет реальные исходы
**Acceptance criteria:**
- [ ] `agent-accuracy.service.ts` сопоставляет AI-decision с **реальной** сделкой через `outcomeJoinKey`; `fetchMovePct`-путь заменён/помечен fallback.
- [ ] `resultSnapshot` заполняется реальными данными и читается.
- [ ] Спек тестирует реальный путь (не мок исхода).

**Verification:** `npm test` (core) accuracy-спек с реальным join; ручной recompute даёт точность по факт. сделкам.
**Dependencies:** T10a · **Files:** `core/src/analysis/agent-accuracy.service.ts`, схемы, спеки · **Scope:** M

## T11 — `ai_trend` агрегирует взвешенный вклад агентов · (W2a) · **[Q4 решён: реализовать]**
**Description:** `ai_trend` сейчас выводится **только** из финальной `analysisStructured.confidence`+`bias` через `directionProbabilitiesFromAnalysis` (`decision-analytics.ts:151-170`), а пер-агентные `signal`/`confidence`/`weight` (которые уже считаются в `buildReasoningPath:253-285` через `getAgentWeight`) **в `ai_trend` не входят** → петля «accuracy → веса → сигнал» мертва. **Решение Q4: реализовать агрегацию** — `ai_trend` должен стать взвешенной агрегацией пер-агентных сигналов, чтобы веса (после T9) реально влияли на сигнал.

**Дизайн (зафиксировать в ADR):**
- Вход агрегации: пер-агентные `{signal, confidence, weight}` из `buildReasoningPath` (веса — из `AiConfig.agentWeights`, ставшие живыми после T9) + analyst как участник с высоким весом (он синтезирует).
- Выход: та же структура `AiTrend {direction, strength∈[0,1], probabilitiesPct{up,down,flat}}` (сумма = 100) — **контракт-форма не меняется**, потребитель `ai_overlay` в constructor не ломается.
- Детерминизм сохранить (как у текущей `directionProbabilitiesFromAnalysis`); опц. конфиг-блендер «доля bottom-up агрегации vs top-down analyst» с безопасным дефолтом.

**Acceptance criteria:**
- [ ] `ai_trend` вычисляется как взвешенная агрегация пер-агентных сигналов; смена весов агента даёт измеримое смещение `ai_trend`.
- [ ] Выходная форма/диапазоны идентичны прежним (back-compat для `ai_overlay/resolver`); сумма probabilitiesPct = 100.
- [ ] ADR с формулой; покрытие тестами (сейчас тестируется только confidence→distribution).
- [ ] Поведение при отсутствии кастомных весов = эквивалент текущему (no-regression для дефолтной установки).

**Verification:** `npm test` decision-analytics (агрегация + детерминизм + back-compat); интеграция: изменить вес агента → `ai_trend` сдвигается предсказуемо; смок constructor `ai_overlay` на новых значениях.
**Dependencies:** T9 (живые веса) · **Files:** `core/src/analysis/decision-analytics.ts`, `docs/adr/0002-ai-trend-aggregation.md`, спеки · **Scope:** M

### ✅ Checkpoint C — AI loop (после T9–T11)
- [ ] Apply весов меняет рантайм; точность считается по реальным сделкам; (если Q4=yes) ai_trend реагирует на веса.
- [ ] End-to-end: accuracy → weight suggestion → apply → следующий сигнал смещён.
- [ ] Review.

---

# PHASE 3 — Risk / Portfolio обещания

## T12 — True merged-equity portfolio DD · (W11a)
**Description:** Portfolio-DD = worst-strategy прокси (`max(max_dd_pct)`, `service.py:2405-2410`), а не настоящая просадка слитой эквити. Равномерно «кровоточащий» портфель халт не триггерит.

**Acceptance criteria:**
- [ ] `compute_portfolio` строит **слитую** equity-кривую (сумма реализ.+нереализ. PnL по всем стратегиям во времени) и считает её max-DD.
- [ ] `evaluate_portfolio_dd_guards` использует merged-equity DD вместо прокси.
- [ ] `PortfolioSummary.portfolio_max_dd_pct` отражает merged-equity (поле/комментарий обновлены).
- [ ] Прокси оставлен как опц. дешёвый предохранитель **или** удалён — задокументировать.

**Verification:** `uv run pytest tests/unit/test_portfolio_dd.py`: синтетический портфель с равномерной просадкой > порога → халт всех (прокси бы не сработал).
**Dependencies:** None · **Files:** `app/services/auto_trade/portfolio.py`, `app/services/auto_trade/service.py`, тесты · **Scope:** M

## T13 — Убрать interface-only `net`/`replace` · (W8c) · **[Q5 решён: убрать]**
**Description:** `net`/`replace` есть в API-схеме и DB-CHECK, но движок их пропускает (`engine.py:185-191`) — мёртвые опции. **Решение Q5: убрать.** (Фронт их уже не предлагает — `risk-section.tsx:21-24`.)

**Acceptance criteria:**
- [ ] Удалить `net`/`replace` из `Literal["off","block_opposite",...]` (`auto_trade.py:56`); остаются `off` / `block_opposite`.
- [ ] Обновить DB-CHECK `conflicting_signal_policy` (`auto_trade_risk_config.py:66`) миграцией; существующие строки со значением `net`/`replace` (если есть) → backfill в `block_opposite` (безопаснее) или `off` — решить и задокументировать.
- [ ] Удалить мёртвую ветку warn-and-allow в `engine.py:185-191`.
- [ ] Контракт регенерирован; схема/БД/движок/UI согласованы.

**Verification:** `uv run pytest tests/unit/test_pre_trade_engine.py`: схема не принимает `net`/`replace` (422); `block_opposite`/`off` работают как прежде. Alembic up/down.
**Dependencies:** None · **Files:** `app/schemas/auto_trade.py`, `app/models/auto_trade_risk_config.py`, `app/services/auto_trade/risk/engine.py`, миграция, тесты · **Scope:** S

## T14 — Data-freshness реально действует + per-agent · (W8b)
**Description:** 4ч-крон эмитит `data_stale`, но не действует (не паузит/не блокирует вход) и не truly per-agent (profile-level recency).

**Acceptance criteria:**
- [ ] При устаревших данных AI вход блокируется/стратегия паузится (за конфиг-флагом), не только алерт.
- [ ] Freshness считается **per-agent** (таймстемпы по каждому агенту), а не profile-level — устранить документированное ограничение `freshness.py:9-12`.
- [ ] Действие конфигурируемо (alert-only / block / pause), дефолт безопасный.

**Verification:** `uv run pytest tests/unit/test_freshness.py`: stale-агент → вход заблокирован/пауза по флагу; свежие → проход.
**Dependencies:** None · **Files:** `app/services/auto_trade/freshness.py`, `service.py` (enqueue-гейт `:3608`), модели, тесты · **Scope:** M

### ✅ Checkpoint D — Risk/Portfolio (после T12–T14)
- [ ] Merged-equity DD триггерит халт на равномерной просадке; нет interface-only риск-опций; stale-данные блокируют вход.
- [ ] Review + калибровка порогов с трейдерами (см. T18).

---

# PHASE 4 — Transparency & UX обещания

## T15 — «Live» KPI через SSE-push, не polling · (W12g, AC#7)
**Description:** KPI-числа на `/monitor` тянутся `setInterval` раз в 30с; SSE используется только для тостов. Договорное «live dashboard» — условно.

**Acceptance criteria:**
- [ ] Backend периодически (или на изменение) пушит KPI-снапшот портфеля/стратегий как SSE-событие (новый тип в `STREAMABLE_EVENTS`, `stream.py:32-49`); context7 sse-starlette паттерн (`EventSourceResponse`, ping, disconnect).
- [ ] Фронт `LiveMonitorDashboard` обновляет KPI из SSE; `setInterval`-poll убран или оставлен как резерв при разрыве SSE.
- [ ] Плашка «Live» отражает реальный поток данных.

**Verification:** `npm test` (front) monitor-стор; ручной: KPI меняется без 30с-задержки при событии; `uv run pytest` на эмит KPI-события.
**Dependencies:** None (SSE-инфра есть) · **Files:** `app/services/events/stream.py`, `app/worker/tasks.py` (или emit-хук), `constructor-front/.../monitor`, `stores/risk-events-store.ts`, тесты · **Scope:** M

## T16 — Привязка forecast к LIVE-стратегии · (W12e, AC#1) · **split**
**Description:** «Use in strategy» ведёт только в backtest/paper-билдер (`/strategy?forecast=`); форма live auto-trade конфига не имеет привязки forecast.

### T16a — Backend: forecast↔live-конфиг
**Acceptance criteria:**
- [ ] Поле привязки forecast на `auto_trade_configs` (id/ссылка на каталог) + миграция.
- [ ] `ai_overlay`/resolver учитывает привязанный forecast при live-сигнале (или явно документируется, как он влияет).
- [ ] Контракт обновлён.

**Verification:** `uv run pytest tests/integration/test_config_forecast.py`; alembic up/down.
**Dependencies:** None · **Files:** `app/models/auto_trade_config.py`, `app/services/auto_trade/ai_overlay/*`, миграция, эндпоинт, тесты · **Scope:** M

### T16b — Frontend: «Attach to live strategy»
**Acceptance criteria:**
- [ ] В каталоге `/forecasts` кнопка привязки к **live**-стратегии (не только preselect в билдере); в форме auto-trade видна/редактируется привязка.
- [ ] Round-trip через контракт.

**Verification:** `npm test` (front); ручной: привязать forecast к live-стратегии, сохранить, перезагрузить — сохранилось.
**Dependencies:** T16a · **Files:** `components/forecast-catalogue.tsx`, `components/auto-trade/*`, API-клиент · **Scope:** M

## T17 — Agent accuracy / weight-suggestions в трейдерском UI · (W12f)
**Description:** Точность агентов и weight-suggestions доступны только в admin-дашборде; трейдер их не видит. Нет отдельного AI Decision Dashboard.

**Acceptance criteria:**
- [ ] Трейдерская страница/секция (AI Decision Dashboard) показывает: reasoning path, ai_trend, **per-agent accuracy (7d/30d)** и **weight-suggestions** с кнопкой apply (step-up).
- [ ] Данные из реальных эндпоинтов (после T9/T10 — корректные).
- [ ] Не admin-only (в `BASE_NAV`).

**Verification:** `npm test` (front); ручной: трейдер видит accuracy + может применить suggestion (через step-up).
**Dependencies:** T9, T10 · **Files:** `constructor-front/app/(app)/...`, `components/...ai-decisions...`, nav · **Scope:** S→M

### ✅ Checkpoint E — Transparency/UX (после T15–T17)
- [ ] «Live» дашборд реально push-обновляется; forecast привязывается к live; трейдер видит AI-аналитику и accuracy.
- [ ] Контракт фронта регенерирован; vitest зелёный. Review.

---

# PHASE 5 — Governance enablement, audit, решения-развилки

## T18 — Безопасное включение governance + калибровка · (W12 §7)
**Description:** Весь governance off-by-default (KPI-Guard, Kill-Switch, portfolio-DD, anomaly, AI-overlay). Договорное «enforcement» в проде дремлет. Нужны калибровка с трейдерами и осознанное включение.

**Acceptance criteria:**
- [ ] Документ калибровки порогов (KPI-guard, kill-switch, portfolio-DD, anomaly-z, promote-gate) — значения согласованы с трейдерами, не «на глаз».
- [ ] Безопасный rollout-чеклист: включать на demo → малый размер → live; per-config.
- [ ] (опц.) UX-предупреждение в Risk Config UI, что защита выключена.
- [ ] Зафиксировать, что считается приёмочной конфигурацией (какие флаги on на сдаче).

**Verification:** Ревью документа трейдерами; smoke на demo с включёнными порогами → срабатывания наблюдаемы.
**Dependencies:** Phase 0–3 (механизмы должны быть корректны) · **Files:** `RISK_GOVERNANCE.md`, `tasks/*`, (опц.) UI-баннер · **Scope:** M (doc-heavy)

## T19 — First-class таблица lifecycle-аудита · (W10f)
**Description:** Переходы lifecycle только как `AutoTradeEvent`; выделенной истории нет; FSM `_transition_log` в памяти.

**Acceptance criteria:**
- [ ] Таблица `strategy_promotion_events` (`config_id, from_stage, to_stage, decision, kpi_snapshot_json, actor, created_at`) + миграция.
- [ ] Каждый promote/demote/gate-fail пишет строку; запрашиваемая история.
- [ ] (опц.) эндпоинт/UI истории на карточке стратегии.

**Verification:** `uv run pytest tests/unit/test_promotion_audit.py`; alembic up/down; ручной: промоут → строка в таблице.
**Dependencies:** None · **Files:** модель + миграция `0037`, `service.py` (promote/demote), тесты · **Scope:** S

## T20 — Email confirmation для критичных действий (Resend) · (W11c-e) · **[Q1 решён: реализовать]**
**Description:** Договор: «TOTP **+ email confirmation**». Сейчас отсутствует полностью. **Решение Q1: реализовать через Resend** (context7-verified: `resend.Emails.send_async`, `idempotency_key`, `RESEND_API_KEY`). **Жёсткое требование:** старые аккаунты (без email-confirm) **не должны ломаться**.

**Дизайн:**
- Зависимость `resend`; `RESEND_API_KEY`/`EMAIL_FROM` через `config.py` (+ startup-guard как T1: пустой ключ → email-confirm выключен, не краш).
- Сервис `app/services/auth/email_confirm.py`: генерация одноразового токена (хэш at-rest, TTL ~10 мин, single-use — паттерн recovery-codes/step-up jti), отправка через `send_async` с `idempotency_key`.
- Интеграция как **дополнительный** фактор для критичных действий (совместно со step-up), управляемый флагом/настройкой пользователя.
- **Back-compat:** аккаунты без подтверждённого email → текущий поток (TOTP/step-up) работает без изменений; email-confirm — постепенный rollout, не хард-блок. Отправка через рейт-лимит (как T5).

**Acceptance criteria:**
- [ ] Письмо с одноразовым кодом/ссылкой реально уходит (Resend), токен single-use + TTL + hash at-rest.
- [ ] Критичное действие может требовать email-confirm (за настройкой), но **аккаунты без него работают по-старому** — ничего не сломано.
- [ ] Пустой `RESEND_API_KEY` → фича корректно выключена (как Telegram при пустом токене), без краша.
- [ ] Rate-limit на отправку (анти-абуз).

**Verification:** `uv run pytest tests/integration/test_email_confirm.py` (отправка мокнута; токен single-use/TTL; back-compat: старый аккаунт проходит без email); ручной: реальное письмо на тест-ящик через verified-домен.
**Dependencies:** Phase 0 (auth, T1, T5) · **Files:** `app/services/auth/email_confirm.py`, `app/api/v1/endpoints/auth.py`, `app/core/config.py`, `pyproject.toml`, тесты · **Scope:** M

## T21 — Per-strategy worker isolation · (W7c) · **[Q3 решён: НЕ делать] — ❌ ИСКЛЮЧЕНО ИЗ SCOPE**
**Description:** Один общий `RedisStreamBroker`; per-strategy воркеров нет. В договоре пункт **опционален**. **Решение Q3: не делать.**

**Acceptance criteria:**
- [ ] Зафиксировать письменно (`docs/change-requests/`), что worker isolation — опциональный пункт договора и **в scope M4 не входит** (исключён по согласованию с заказчиком 2026-06-20).
- [ ] Изоляция стратегий обеспечивается саб-аккаунтами + `FOR UPDATE SKIP LOCKED` (уже есть, W7a) — достаточно для AC#3.

**Verification:** подписанная нота в репозитории.
**Dependencies:** None · **Files:** `docs/change-requests/worker-isolation-out-of-scope.md` · **Scope:** doc (XS)

## T22 — Сверить документацию с деревом · (Аудит §8)
**Description:** Close-out доки разъехались с кодом (reconciliation.py «empty stub» — файла нет; watcher-bus комментарии; sandbox docstring; config.py:38-41 vs PATCH).

**Acceptance criteria:**
- [ ] Исправлены/удалены устаревшие утверждения: `m4-closeout-plan.md:252`, `pipeline.py:51-56`, `state_machine.py:14-18`, `config.py:38-41`.
- [ ] Close-out статус приведён в соответствие с фактом (после Phase 0–4).

**Verification:** Ревью diff'а доков; ссылки `файл:строка` в доках валидны.
**Dependencies:** T2,T3,T7,T8 (после фиксов) · **Files:** `tasks/m4-closeout-plan.md`, code-комментарии · **Scope:** S

### ✅ Checkpoint F — Финальная приёмка (после T18–T22)
- [ ] `uv run pytest` + `uv run mypy app` + `uv run ruff check app` зелёные; фронт vitest зелёный; контракт регенерирован.
- [ ] Все блокеры закрыты; каждое невыполненное обещание либо реализовано, либо формально пере-скоуплено письмом.
- [ ] Re-run таблицы Acceptance Criteria из аудита §3 — все ✅ или с подписанным change-request.
- [ ] Staging deploy + acceptance review с заказчиком.

---

## Риски и митигации

| Риск | Влияние | Митигация |
|---|---|---|
| T2 смена деривации ключа → потеря доступа к ciphertext | High | `MultiFernet([new, old])` совместимость; тест на demo-копии БД до прод |
| T7 watcher-consumer дёргает SL слишком часто | Med | cooldown/дедуп; тест на частоту; demo-прогон |
| T8 миграция дефолта затронет существующие конфиги | High | backfill НЕ трогает рабочие (явно live); alembic up/down на throwaway PG |
| Включение governance с неоткалиброванными порогами на счёте 17 | High | T18 калибровка с трейдерами; rollout demo→малый размер→live |
| T10 cross-service контракт (constructor↔core) | Med | contract-first (T10a до T10b); internal_api_key auth |
| Реальные деньги во время работ | High | все изменения сперва на demo-аккаунте; Phase 0/1 первыми |

## Решения по развилкам (заказчик, 2026-06-20) — ЗАФИКСИРОВАНЫ

- **Q1 (T20) → РЕАЛИЗОВАТЬ** email-confirmation через **Resend** (самый простой+бесплатный: 3000/мес, 100/день, async, idempotency). Старые аккаунты без email-confirm **не ломать**.
- **Q2 (T8) → СТАДИЯ ВЕРИФИКАЦИИ.** Sandbox = реальная проверка работоспособности на demo/testnet (без реальных денег); выход в live **только** после прохождения KPI-гейта. Demo-исполнение оставляем, lifecycle делаем честным, docstring правим.
- **Q3 (T21) → НЕ ДЕЛАТЬ** worker isolation (опциональный пункт; исключён из scope, зафиксировать нотой).
- **Q4 (T11) → РЕАЛИЗОВАТЬ** взвешенную агрегацию пер-агентного вклада в `ai_trend` (форма контракта не меняется; ADR).
- **Q5 (T13) → УБРАТЬ** `net`/`replace` из схемы + DB-CHECK (оставить `off`/`block_opposite`).

## Параллелизация

- **Безопасно параллелить:** T1/T2 (сериализовать между собой — общий файл) ∥ T3 ∥ T4 ∥ T5 ∥ T6; в Phase 3 T12 ∥ T13 ∥ T14; в Phase 4 T15 ∥ T16 ∥ T17.
- **Строго последовательно:** T10a→T10b; T16a→T16b; T9→T11; T9/T10→T17; миграции (T8/T16a/T19) не пересекать по таблицам в одном запуске.
- **Координация контракта:** все правки роутов → один прогон `gen:api-types` на фазу.

# Milestone 4 — что осталось и следующие шаги

> Обновлённый план остатка работ по M4. Дата: ~4 июня 2026. До дедлайна (27 июня) — **~3 недели**.
> Источник: договорный план M4 (`M4_Work_Plan`) + фактическое состояние кода.
> Связанные доки: [plan.md](plan.md) (W9 PnL-accuracy), [frontend-spec.md](frontend-spec.md), [../RISK_GOVERNANCE.md](../RISK_GOVERNANCE.md) (W8).

---

## 0. TL;DR

| | Статус |
|---|---|
| Acceptance Criteria (7) | **3 ✅ · 3 🟨 (AC#4, AC#5, AC#7) · 1 🟨-UI (AC#1)** |
| Главный блокер | **AC#4 — нет in-trade auto-pause / kill-switch** (pre-trade BLOCK есть, авто-паузы нет) |
| Критический путь | AC#4 (auto-pause+KPI-guard+kill-switch) → AC#7 (live-KPI+dashboard) → AC#5 (2FA) + UI |
| Тесты бэка | 747 passed / 1 skipped (Checkpoint Final W9) |

**Что произошло с момента прошлого аудита:** закрыты **W8** (риск-фундамент: Pre-Trade Risk Engine,
Strategy Health Score, Data Freshness) и **W9-as-built** — но W9 ушёл не в kill-switch/KPI, а в
**точность PnL** (realized из биржевого ledger + funding + BNB-комиссии, T1–T14). Это **правильный
фундамент**: нельзя авто-паузить по KPI, посчитанным из неверного PnL. Теперь PnL точный — можно
строить in-trade governance. Плюс багфиксы (WS-revive, REST-reconcile закрытых позиций).

---

## 1. Acceptance Criteria — текущая готовность

| # | Критерий | Статус | Что есть / чего нет |
|---|---|---|---|
| 1 | Research Module (каталог + Δ vs Baseline) | 🟨 | Бэкенд ✅. UI **только в админке**, нет «attach to strategy» |
| 2 | Autonomous Execution (SL trailing/breakeven/volatility) | ✅ | Полностью |
| 3 | Multi-Strategy ≥3 без коллизий | ✅ | Sub-account-per-strategy |
| 4 | **Risk Enforcement (auto-pause на KPI Guard / Max DD / Loss/day)** | 🟨 | Pre-Trade Engine **блокирует входы** (W8), но **не паузит** running-стратегию и **нет volatility kill-switch**. `set_running(False)` по риск-метрике/KPI — нигде |
| 5 | Security (API Vault + 2FA) | 🟨 | Vault/Fernet ✅. **2FA (pyotp) — нет** (нет в зависимостях и коде) |
| 6 | Asset Expansion BTC + ETH | ✅ | Symbol-agnostic + тесты |
| 7 | KPI Transparency live dashboard (Sharpe-proxy, WR, ROI) | 🟨 | Точный PnL ✅, Health Score ✅, но **live-агрегации KPI по running-стратегиям нет** (`PortfolioSummaryResponse` без sharpe/wr/roi), **дашборда нет** |

**Итог:** 3 ✅, 3 🟨 (AC#4/5/7), AC#1 🟨-UI. Эти 3+1 «частично» — весь acceptance-критический путь.

### Что уже сделано хорошо (признать)
- **W4 Dynamic Params** — AI Trend Overlay реализован (entry-lock / ATR-scale / RSI-scale), `ai_overlay`.
- **W8** — Pre-Trade Risk Engine (5 правил), Strategy Health Score, Data Freshness 4h (+ review-фиксы C1/I1–I7).
- **W9 PnL-accuracy** — `realized_pnl` из ledger, funding-ledger, BNB-комиссии по mark, `net=gross−comm+funding`, daily-loss numerator теперь pure-DB net (T14). 747 тестов.

---

## 2. Остаток работ — приоритеты

### 🔴 MUST для acceptance (закрыть 3 «частично»)

**A. AC#4 — In-Trade Governance (auto-pause + kill-switch).** Самый большой и ценный; разблокирован
(W8 risk-engine + W9 точный PnL + W8 health).
- **Live-KPI по running-стратегии**: rolling net-PnL / running max-DD / win-rate за окно — переиспользовать
  W9 ledger + W8 `backtesting/common.py`. (Сначала закрыть review-**I6**: нормировка `max_dd_pct`/`pnl_pct`
  — иначе KPI-guard сработает по кривой метрике.)
- **KPI-Guard auto-pause**: расширить `auto_trade_risk_configs` (`kpi_guard_max_dd_pct`,
  `kpi_guard_min_win_rate_pct`, `kpi_guard_min_trades`) + cron (1–5 мин) и on-trade-close hook →
  при нарушении `set_running(config_id, False)` + risk-событие. **Это и есть AC#4.**
- **Volatility Kill-Switch**: при spike (ATR/price-move порог) — авто-закрыть открытые позиции конфига +
  пауза. Хук в существующий watcher/`live_tracker`.
- **Portfolio DD watcher (W11)**: cron → если портфельный DD > порога → `set_running_bulk(False)` (пауза всех) + alert.

**B. AC#7 — Live KPI pipeline + dashboard.**
- Расширить `PortfolioSummaryResponse` + per-strategy live-KPI endpoint: Sharpe-proxy / Win Rate / ROI /
  running-DD (по **точному net-PnL** из W9, не из бэктеста).
- Frontend: Live Monitor с этими KPI-карточками.

**C. AC#5 — 2FA TOTP** (`pyotp`, подтверждён context7).
- `enroll` → `secret = pyotp.random_base32()`, `provisioning_uri` (QR), хранить **зашифрованным** через
  существующий `SecretCipher` (API Vault). До подтверждения 2FA не считать включённой.
- `verify` → `TOTP(secret).verify(code, valid_window=1)` (constant-time, ±30s).
- `step-up` → short-lived JWT для critical actions (start auto-trade, смена exchange-key, изменение risk-config).

### 🟠 Сильно желательно (закрыть UI-разрыв под acceptance-демо)
- **Risk Config UI** (constructor-front): форма к `auto_trade_risk_configs` (daily-loss, max-dd, max-open,
  exposure, leverage-ceiling, conflicting-policy, kpi-guard). Сейчас в форме только `risk_mode` — риск-движок
  не настраивается из UI.
- **AI Forecast Catalogue trader-UI** (закрывает AC#1): вынести из админки, фильтры + «attach to strategy».
- **SSE event channel** (W12, `sse-starlette`): единый `/events/stream` (risk_blocked / risk_check_degraded /
  data_stale / kpi_guard / kill_switch) → питает Live Monitor и 2FA-флоу. Сейчас `EventSource` не используется.

### 🟡 Defer в M5 (согласовать change-request с заказчиком)
- **W10 Strategy Promotion Pipeline** (Sandbox→KPI Gate→Live FSM) — полностью отсутствует, большой объём.
- **Strategy anomaly detection** (W12) — отдельный ML-пайплайн.
- **Risk alerting каналы** (Telegram/email) — на M4 достаточно SSE + лог.
- **conflicting-signal `net`/`replace`** (W10) — сейчас interface-only.
- **Frontend test-suite** — добавить smoke-набор; полное покрытие defer.

---

## 3. Календарь остатка (~W10–W12)

| Неделя | Бэкенд | Фронт |
|---|---|---|
| **W10** 9–13 июн | AC#4: live-KPI per strategy + KPI-Guard auto-pause + Volatility Kill-Switch + Portfolio DD watcher; risk-config schema (`kpi_guard_*`); фикс review-**I6** | — |
| **W11** 16–20 июн | AC#5 2FA (enroll/verify/step-up) + SSE event channel + `PortfolioSummaryResponse` с live-KPI | начать Risk Config UI |
| **W12** 23–27 июн | QA, staging, acceptance review, change-request письмо | Live Monitor KPIs · Risk Config UI · 2FA UI · Catalogue trader-UI |

> Это **Сценарий A (compressed scope)** из исходного аудита — реалистичный путь к acceptance.
> Math (остаток ~7–9 чел-недель / 3 недели / ~1.5–2 FTE) сходится только при урезании (W10 promotion,
> anomaly → M5) **или** +3-й разработчик на UI/2FA (Сценарий B, +slip).

---

## 4. Ваши следующие шаги (решения и greenlight)

1. **Change-request заказчику (ключевое решение):** письменно согласовать перенос **Strategy Promotion
   Pipeline (W10)** и **anomaly detection** в M5. Без согласования acceptance провалится по этим пунктам
   договора. Обоснование: без них приёмка возможна, с ними при текущем темпе — нет.
2. **Greenlight AC#4 in-trade governance** как немедленный следующий бэкенд-стрим (главный блокер; все
   зависимости готовы).
3. **Откалибровать риск-пороги (реальные деньги):** до включения auto-pause согласовать с трейдерами
   `kpi_guard_max_dd_pct`, дневные лимиты, порог volatility-spike, портфельный DD. Это защита счёта —
   значения нельзя ставить «на глаз».
4. **Дать live demo-ключи биржи:** часть W9-чекпоинтов («⚑ нужны live-ключи») и сверка `/positions`/
   `/portfolio` против Binance UI заблокированы без реального аккаунта.
5. **Ресурс на фронт:** UI-разрыв большой (Risk Config, Live Monitor KPIs, 2FA, Catalogue). Подтвердить
   фронтенд-разработчика на W12, иначе командой 2 чел. UI не закрыть.
6. **Техдолг до прод-демо:** ротация утёкших секретов; проверить `gpt-5.2` в seed (если ещё открыто).
7. **Закрыть «⚑ Ревью с человеком»** в [todo.md](todo.md): семантика net daily-loss (раннее срабатывание
   при учёте funding/комиссий — желаемо?), атрибуция funding, демо-сверка.

---

## 5. Открытые review-пункты (W8), которые цепляют остаток
- **I6** — нормировка health `max_dd_pct`/`pnl_pct` (R-space или реальный баланс): **сделать до того, как
  KPI-Guard начнёт паузить по `health_score`** — иначе авто-пауза по кривой метрике.
- **I7** — гонка portfolio-cap (±1) задокументирована; freshness-upsert переведён на `on_conflict` ✅.
- S1–S7 — мелочи (вынести правила в helpers, `/health/agents` из probe-роутера, `data_stale` only-on-transition).

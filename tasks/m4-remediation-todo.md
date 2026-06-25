# M4 Remediation — TODO

> Чеклист к [m4-remediation-plan.md](m4-remediation-plan.md). Источник: [reports/M4_AUDIT.md](../reports/M4_AUDIT.md).
> Легенда: `[ ]` todo · `[~]` в работе · `[x]` done · 🔴 блокер · 🟡 обещание · �split · ⛏decision

## Phase 0 — Security блокеры (fail-fast)
- [x] 🔴 **T1** Startup-guard на дефолтные секреты (S4) — `config.py`/`.env.example` — **S** ✅ `4cac9a2`
- [x] 🔴 **T2** Стойкая деривация ключа (strict Fernet + MultiFernet legacy, убран sha256-fallback) (S5) — `security.py` — **S** ✅ `21b967d` ⚠️prod: задать валидный ENCRYPTION_KEY + ENCRYPTION_KEY_LEGACY=<старый> при деплое
- [x] 🔴 **T3** Step-up на PATCH/DELETE ключа биржи (S1/S2) — back `exchange.py` ✅ `6f6979a` + front step-up modal ✅ `574558e`
- [x] 🔴 **T4** Удалён `/encrypt`-оракул (был без auth, 0 вызовов) (S6) — back `a62be95` + front `c7a8dc3`
- [x] 🔴 **T5** Rate-limit логина (per-IP+email) + step-up jti fail-closed (S7/S8) — `ratelimit.py`/`auth.py`/`deps.py` — **M** ✅ `320111e`
- [x] 🟡 **T6** Прод-CORS warning при `*` + non-debug (S9) — `config.py` — **XS** ✅ `81defcb`
- [x] ✅ **Checkpoint A** — pytest зелёный (только 2 pre-existing date-drift); mypy/ruff чисто; все критмутации (play/promote/edit-risk/create+update+delete ключа) под step-up; ⚠️ перед прод-деплоем: задать сильные секреты + ENCRYPTION_KEY(+LEGACY); demo-прогон + review

## Phase 1 — Execution integrity блокеры
- [x] 🔴 **T7** Подключить потребителя watcher event-bus (runtime supervisor + resilient consumer) (W5b) — `service.py`/`main.py`/`pipeline.py` — **M** ✅ `4309d41`
- [x] 🔴 **T8** Sandbox = стадия верификации + fail-safe дефолт `sandbox` [Q2✓] (W10e) — модель+миграция `0036`+docstring — **S→M** ✅ `88568b2` (alembic up/down verified on PG; guard P4-4 + gate уже были)
- [x] ✅ **Checkpoint B** — watcher consumer в рантайме (T7); новые стратегии стартуют sandbox, real-money только в `live` (T8); full suite зелёный (только 2 pre-existing date-drift); migration up/down OK

## Phase 2 — AI feedback loop (W2/W3)
- [x] 🟡 **T9** Apply весов перепривязывает `AiConfig` (bindAgentWeights) (W3b) — core `ai-config.service.ts`/`agent-accuracy.service.ts` — **S** ✅ `32d1085`
- [x] 🟡 **T10a** constructor отдаёт реальные исходы `/internal/agent-outcomes` (join по `decision_event_id`) (W3a) — **M** ✅ `e2f1382` (+front `dbc52ca`)
- [x] 🟡 **T10b** core: blend real+synthetic accuracy + `realSampleSize` (W3a) — `agent-accuracy.service.ts` — **M** ✅ core `7962254`
- [x] 🟡 **T11** `ai_trend` взвешенная агрегация (opt-in на custom weights) [Q4✓] (W2a) — core `decision-analytics.ts`+ADR 0002 — **M** ✅ core `e5e8b58`
- [x] ✅ **Checkpoint C** — accuracy→weight→apply→ai_trend смещён (петля замкнута); core 68 tests + build зелёные

## Phase 3 — Risk / Portfolio (W8/W11)
- [x] 🟡 **T12** True merged-equity portfolio DD (один equity-curve, guard+summary) (W11a) — `portfolio.py`/`service.py`/`health.py` — **M** ✅ `2ac8493`
- [x] 🟡 **T13** Убраны `net`/`replace` (схема+DB-CHECK 0037+engine+front) [Q5✓] (W8c) — **S** ✅ back `5423622` front `faa8abb`
- [x] 🟡 **T14** Freshness **действует** (pre-trade gate, off-by-default) (W8b) — `freshness.py`/`service.py` — **M** ✅ `7ab6c20`
  - [ ] ↳ **T14b (follow-up, cross-service)** true per-agent freshness timestamps — нужен core, который отдаёт `AiDecisionEvent.perAgent` data-ages (форма как T10). Пока profile-level recency.
- [x] ✅ **Checkpoint D** — merged-DD халтит равномерную просадку (T12); нет interface-only опций (T13); stale блокирует вход за флагом (T14); full suite зелёный (только 2 pre-existing date-drift); migrations 0036/0037 up/down OK. Калибровка порогов → T18.

## Phase 4 — Transparency / UX (W12)
- [x] 🟡 **T15** «Live» KPI через SSE-push (portfolio_kpi cron+store) (W12g/AC#7) — **M** ✅ back `f551d04` front `6cbd8f7`
- [x] 🟡 **T16a** Backend forecast↔live (`attached_forecast_id` model+0038+schema+service) (W12e/AC#1) — **M** ✅ `017d720`
- [x] 🟡 **T16b** Frontend «Attach forecast» в auto-trade config form (round-trip) (W12e) — **M** ✅ front `13b3bf2`
- [x] 🟡 **T17** Agent accuracy/weights в трейдерском UI (AgentAccuracyPanel + apply step-up) (W12f) — **S→M** ✅ back `a125b58` front `acb8493`
- [x] ✅ **Checkpoint E** — KPI push-live по SSE (T15); forecast→live attach (T16); трейдер видит accuracy+apply (T17); контракт реген; back 1034 pass (+2 date-drift), front 26 vitest, migrations 0036–0038 OK

## Phase 5 — Governance / audit / решения
- [x] 🟡 **T18** Калибровка+безопасное включение governance (RISK_GOVERNANCE.md §7) (§7) — **M** ✅ `c1f5f39`
- [x] 🟡 **T19** First-class таблица lifecycle-аудита (`strategy_promotion_events` 0039 + promote/demote/gate-fail writes) (W10f) — **S** ✅ `bb6fcaf`
- [x] 🟡 **T20** Email-confirmation через **Resend** (httpx, gated, single-use+TTL+hash, rate-limit) [Q1✓] (W11c) — **M** ✅ back `4cdd026` front `6d304ab`
- [x] ❌ **T21** Worker isolation — **НЕ ДЕЛАЕМ** [Q3✓], нота зафиксирована (W7c, опц.) — `docs/change-requests/worker-isolation-out-of-scope.md` — **XS** ✅ `c1f5f39`
- [x] 🟡 **T22** Сверена документация с деревом (closeout banner + stale-fixes) (§8) — **S** ✅ `c1f5f39`
- [x] ✅ **Checkpoint F** — Phase 5 done: T18/T19/T20/T22 ✅, T21 excluded-by-note. Back 1045 pass (+2 pre-existing date-drift), front tsc+vitest green, migrations 0036–0040 up/down OK. Остаётся: T14b (cross-service, defer) + staging deploy + acceptance review с заказчиком.

## Решения по развилкам (заказчик, 2026-06-20) — ЗАФИКСИРОВАНЫ
- [x] **Q1** Email-confirmation → **реализовать (Resend)**; старые аккаунты не ломать → T20
- [x] **Q2** Sandbox → **стадия верификации работоспособности** на demo, затем гейт → live → T8
- [x] **Q3** Worker isolation → **НЕ делать** (исключить из scope нотой) → T21
- [x] **Q4** `ai_trend` → **реализовать** взвешенную агрегацию агентов → T11
- [x] **Q5** Conflicting `net`/`replace` → **убрать** из схемы → T13

## Сводка
- **Блокеры (8):** T1,T2,T3,T4,T5,T7,T8 (+T6 хардненинг). Phase 0–1, первыми.
- **Невыполненные обещания (13 в работе):** T9,T10(a/b),T11,T12,T13,T14,T15,T16(a/b),T17,T18,T19,T20,T22.
- **Исключено по согласованию:** T21 (worker isolation).
- **Все 5 развилок (Q1–Q5) решены.**

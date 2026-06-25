# TODO — Phase 2 (W11: 2FA + SSE backend)

Источник: [m4-closeout-plan.md](m4-closeout-plan.md) Phase 2. Backend-стримы B1 (2FA, AC#5) и B3 (SSE, W12). Frontend F2/F4/F5 — Phase 3.
Легенда: `[S]`≈≤0.5д · `[M]`≈1–1.5д · `[L]`≈2–3д · deps — предшественники.

## B1 — 2FA TOTP (AC#5)
- [x] **P2-T1** [M] `pyotp` dep + `user_totp` model + migration `0028` (verified up/down on PG) + `TotpService` (enroll/verify/is_enabled/disable); секрет шифруется `SecretCipher`, активна только после verify · deps: — · `services/totp.py`, `models/user_totp.py`, `migrations/` · ✅ `9ecdb5d`
- [x] **P2-T2** [M] API: `POST /auth/2fa/enroll` (QR uri + one-time secret, 409 если уже активна), `POST /auth/2fa/verify`, `GET /auth/2fa/status`, `DELETE /auth/2fa`; контракт фронта синхронизирован · deps: P2-T1 · `api/v1/endpoints/auth.py`, `schemas/auth.py` · ✅ back `2512ee2` / front `b0f4c07`
- [x] **P2-T3** [L] Step-up: `POST /auth/2fa/step-up` (fresh-code → 5-мин JWT) + `require_step_up` dep гейтит start auto-trade, `PUT /auto-trade/config` (risk), `POST /accounts` (exchange-key), `DELETE /2fa` — только когда 2FA включена (иначе pass-through) · deps: P2-T2 · `core/auth.py`, `api/deps.py`, `endpoints/{auth,live,exchange}.py` · ✅ back `e16ea40` / front `13d561f`
- [x] **P2-T4** [M] Recovery codes: 10 хэш-кодов при enroll (показ один раз), `user_recovery_code` + миграция `0029` (verified PG); приём через verify/step-up как one-time fallback только для подтверждённой 2FA; очистка при disable · deps: P2-T1 · ✅ back `6507011` / front `8269c9a`
- [ ] **P2-T5** [S] Email confirmation как второй фактор подтверждения (или defer по согласованию) · deps: P2-T2

## B3 — SSE event channel (W12)
- [x] **P2-T6** [M] `sse-starlette` + `GET /events/stream` (EventSourceResponse) поверх Redis pub/sub; `_emit_event` публикует streamable risk-события в `events:user:{id}` (best-effort, self-filter, never raises); `is_disconnected()` + `CancelledError` cleanup · deps: — · ✅ back `f224561` / front `2c385ae`

## Code-review fixes (5-axis review of Phase 2)
- [x] **C1** [Critical] brute-force lockout на `/2fa/verify` + `/step-up` (failed_attempts/locked_until, миграция `0030`, 429+Retry-After) · ✅ `636e50b`
- [x] **I1** [Important] SSE-публикация только после commit (after_commit hook, drop on rollback) — нет phantom-событий · ✅ `79397f7`
- [x] **I2** [Important] `/2fa/verify` TOTP-only; recovery-код принимает только `/step-up` (`allow_recovery`) · ✅ `dae4d03`
- [x] **I3** [Important] атомарное потребление recovery-кода (conditional UPDATE + rowcount) · ✅ `dae4d03`
- [x] **I4** [Important] one-time step-up (jti + Redis SETNX, fail-open) — один re-auth = одно действие · ✅ `632eae0`
- [x] **I5** [Important] `publish_user_event` exception-safe (json.dumps внутри try + default=str) · ✅ `b1adaad`
- [x] **I6** [Important] тесты: step-up expiry/wrong-type/cross-user, recovery exhaustion · ✅ `d9cf0bf`
- [x] **S1** [Suggestion] лимит одновременных SSE-стримов на пользователя (429) · ✅ `6fc6acd`
- [x] **S4** [Suggestion] recovery-коды 40→64 бит · ✅ `dae4d03` · **S5** verify коммитит один раз · ✅ `636e50b`
- [ ] **S2/S3** [Suggestion] deployment-заметки (не код): SSE-токен не в query-string (cookie/proxy для F5); CORS `["*"]` переопределить в prod-env

Final: **911 passed, 1 skipped**; ruff/mypy без новых ошибок; миграция `0030` сверена на PG; контракт фронта синхронизирован.

---
Примечание: миграции проверять на throwaway-PG (:55432) — см. память «Alembic migration local verify».

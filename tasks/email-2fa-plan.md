# Implementation Plan: Email confirmation as a full second factor (2FA)

> **Goal:** make **email-confirm a co-equal, opt-in second factor** — like TOTP — that
> participates in BOTH security flows: **login** (`/signin` → challenge → exchange code)
> and **step-up** (re-auth for critical actions). Today TOTP is the only factor and
> email-confirm (T20) is a standalone request/verify pair wired to nothing.
> **Режим составления:** read-only plan; код не менялся. Сверка библиотек — context7 (Resend, pyotp паттерн зеркалим).
> **Repos:** `constructor` (FastAPI) · `constructor-front` (Next.js). Core not involved.

---

## Current architecture (as-is, verified in code)

- **TOTP** (`app/models/user_totp.py`, `app/services/totp.py`): enrollment becomes active
  when `confirmed_at` is set; `totp_service.is_enabled(user)` = has a confirmed enrollment.
  Per-enrollment lockout (`failed_attempts`/`locked_until`). Recovery codes.
- **Step-up:** `POST /auth/2fa/step-up` verifies a TOTP code → `create_step_up_token(email)`
  (short-lived JWT). `require_step_up` (`app/api/deps.py`) enforces it on critical
  endpoints — **but only for users where `totp_service.is_enabled`** (now also the opt-in
  `step_up_require_2fa` hard-block for no-2FA users).
- **Login-2FA:** `POST /auth/signin` — if `totp_service.is_enabled` → returns
  `TwoFactorRequiredResponse(challenge_token)`; `POST /auth/2fa/login` exchanges
  `challenge_token` + TOTP code → token pair.
- **Email-confirm (T20):** `app/services/email_confirm.py` (Resend over httpx),
  `user_email_confirmations` table (hashed, single-use, TTL), `POST /auth/email-confirm/{request,verify}`.
  Standalone — **not** a factor, no enrollment, doesn't drive login or step-up.
- **The single coupling point:** every gate calls `totp_service.is_enabled(...)`. Email-2FA
  must hook in at exactly those three call sites.

## Architecture decisions

1. **Reuse, don't duplicate.** Email-2FA reuses the existing `email_confirm` service
   (Resend send + hashed/single-use/TTL codes) for code delivery/verification. We add an
   **enrollment** concept + wire it into login & step-up. Reserved `action` values:
   `email_2fa_enroll`, `email_2fa_login`, `email_2fa_step_up` (distinct so a code minted
   for one purpose can't be replayed for another).
2. **Enrollment model mirrors TOTP** for symmetry: a `user_email_2fa` row with
   `confirmed_at` (active only after the user verifies a code sent to their email) +
   per-factor lockout. Email-2FA is **per-user opt-in** and requires proving control of
   the account email first (verify-on-enroll).
3. **Unify the "has a second factor" check.** Introduce a small `two_factor` helper
   (`app/services/two_factor.py`): `has_second_factor(user)` = TOTP confirmed OR email-2FA
   confirmed; `available_factors(user) -> set[{"totp","email"}]`. Replace the three
   `totp_service.is_enabled` gate call-sites with `has_second_factor` / factor-aware logic.
   This is the linchpin — do it once, correctly, with tests.
4. **Step-up via email is two HTTP calls** (request code → submit code), unlike TOTP's one.
   The frontend step-up modal must support both: a factor picker (or auto when only one).
   The backend `/auth/2fa/step-up` gains an email path that consumes an `email_2fa_step_up`
   code → mints the same `create_step_up_token`. The step-up **token** is factor-agnostic,
   so `require_step_up` and the gated-endpoint list are unchanged.
5. **Login challenge advertises factors.** `TwoFactorRequiredResponse` gains
   `factors: ["totp","email"]` so the UI knows what to offer; for email it must first call
   a "send login code" endpoint. The challenge token stays the proof-of-password artifact.
6. **No new dependency** (httpx + existing Resend path). **Off-by-default preserved:** if
   Resend isn't configured, email-2FA can't be enrolled (enroll send fails clearly) and
   `available_factors` never includes `email`. TOTP-only users are 100% unaffected.
7. **Patterns to follow:** lockout/hash like `user_totp`/`totp.py`; rate-limit via
   `app/core/ratelimit.py`; migrations batch-mode (0041); front step-up via the existing
   `lib/api/step-up.ts` resolver + `step-up-modal.tsx`; contract regen (Node 20).

---

## Dependency graph

```
E1 user_email_2fa model + migration 0041
      │
E2 two_factor service (has_second_factor / available_factors)  ──┐
      │                                                          │
E3 email-2FA enrollment endpoints (enroll→send, confirm, disable, status)
      │                                                          │
E4 step-up via email (request + submit on /2fa/step-up)         │
      │                                                          │
E5 login via email (signin challenge advertises factors;        │
      /2fa/login email path; send-login-code endpoint)          │
      │                                                          ▼
   (contract regen) ──► F1 security-settings: enroll/manage email-2FA
                        F2 step-up modal: factor picker + email code flow
                        F3 login screen: factor picker + email code flow
```

Backend E1→E2→E3 sequential (foundation); E4 and E5 both depend on E2/E3 and are
independent of each other (parallelizable). Frontend F1/F2/F3 each follow their backend
slice; F2←E4, F3←E5, F1←E3.

---

## Task list

### Phase 1 — Foundation

## E1: `user_email_2fa` enrollment model + migration
**Description:** Per-user email-2FA enrollment, mirroring `user_totp`: active only after the
user verifies a code emailed to their account address. Holds per-factor lockout.

**Acceptance criteria:**
- [ ] Model `UserEmail2FA` (`user_email_2fa`): `user_id` (unique FK), `confirmed_at` (nullable),
      `failed_attempts` (int, default 0), `locked_until` (nullable), timestamps. Registered in `models/__init__.py`.
- [ ] Alembic `0041` (up/down) verified on Postgres; table + unique(user_id).
- [ ] No behavior change yet (descriptive model only).

**Verification:** `uv run pytest tests/test_email_2fa.py -k model`; `alembic up/down` on throwaway PG (:55432, see memory).
**Dependencies:** None · **Files:** `app/models/user_email_2fa.py`, `app/models/__init__.py`, `migrations/versions/20260621_0041_*.py`, test · **Scope:** S

## E2: `two_factor` service — unified factor checks
**Description:** Single source of truth for "does the user have any second factor" and "which".
Replaces scattered `totp_service.is_enabled` gate logic.

**Acceptance criteria:**
- [ ] `app/services/two_factor.py`: `has_second_factor(session, user_id) -> bool` (TOTP confirmed OR
      email-2FA confirmed); `available_factors(session, user_id) -> set[str]` ⊆ {"totp","email"};
      email factor only counts when `email_confirm.is_enabled()` (Resend configured).
- [ ] `app/api/deps.py` `require_step_up` uses `has_second_factor` (keeps the `step_up_require_2fa` hard-block).
- [ ] Pure-ish, fully unit-tested (TOTP-only, email-only, both, neither, Resend-off).

**Verification:** `uv run pytest tests/test_two_factor.py`; regression: existing `test_totp_endpoints.py` step-up tests still green.
**Dependencies:** E1 · **Files:** `app/services/two_factor.py`, `app/api/deps.py`, test · **Scope:** S→M

### Checkpoint A — Foundation
- [ ] Model + migration up/down OK; `two_factor` unit-tested; full suite green (TOTP flows unchanged).

### Phase 2 — Enrollment (vertical slice: a user turns email-2FA on)

## E3: Email-2FA enrollment endpoints (enroll → confirm → status → disable)
**Description:** Opt-in flow proving control of the account email before activating email-2FA.

**Acceptance criteria:**
- [ ] `POST /auth/2fa/email/enroll` (CurrentUser): requires `email_confirm.is_enabled()` (else 503);
      creates/refreshes an unconfirmed `user_email_2fa`; sends an `email_2fa_enroll` code; rate-limited.
- [ ] `POST /auth/2fa/email/confirm` (CurrentUser, body `{code}`): verifies the `email_2fa_enroll`
      code → sets `confirmed_at` (activates). Wrong/expired → 400; lockout after N fails.
- [ ] `GET /auth/2fa/email/status` → `{enabled, available}`; `DELETE /auth/2fa/email`
      (RequireStepUp) disables it (can't strip a factor without re-auth).
- [ ] Disabling the **last** remaining factor is allowed but returns the user to "no 2FA".
- [ ] Contract regenerated.

**Verification:** `uv run pytest tests/test_email_2fa_endpoints.py` (enroll→confirm happy path, wrong code, disable-needs-step-up); manual: enroll, receive code (Resend test domain), confirm → status enabled.
**Dependencies:** E1, E2 · **Files:** `app/api/v1/endpoints/auth.py`, `app/schemas/auth.py`, `app/services/email_confirm.py` (reserved actions), tests, `openapi.json` · **Scope:** M

## F1: Security settings — enroll & manage email-2FA
**Description:** UI to turn email-2FA on/off in `settings/security`, alongside the TOTP section.

**Acceptance criteria:**
- [ ] "Email authentication" card: status; "Enable" → triggers enroll (sends code) → input to confirm.
- [ ] "Disable" routes through the step-up modal (DELETE is step-up-gated).
- [ ] Client services for the E3 endpoints; loading/error states; 503 ("not configured") shown gracefully.

**Verification:** `npx vitest run` (service + a component test); `npx tsc --noEmit`; manual enroll/confirm/disable.
**Dependencies:** E3 · **Files:** `app/(app)/settings/security/page.tsx`, `components/auth/*`, `lib/api/services/auth*.ts` · **Scope:** M

### Checkpoint B — Enrollment
- [ ] A user can enable email-2FA end-to-end (UI → email → confirm) and disable it (step-up). TOTP unaffected.

### Phase 3 — Step-up via email (vertical slice: re-auth a critical action by email)

## E4: Step-up via email on `/auth/2fa/step-up`
**Description:** Let a user with email-2FA obtain a step-up token via an emailed code (TOTP path unchanged).

**Acceptance criteria:**
- [ ] `POST /auth/2fa/step-up/email/request` (CurrentUser): if email-2FA confirmed → send an
      `email_2fa_step_up` code (rate-limited); else 400.
- [ ] `POST /auth/2fa/step-up` accepts EITHER a TOTP code (existing) OR `{method:"email", code}` →
      verifies the `email_2fa_step_up` code → `create_step_up_token`. Single-use code; lockout on fails.
- [ ] The step-up **token** is unchanged → `require_step_up` and the gated-endpoint list need no change.

**Verification:** `uv run pytest tests/test_email_2fa_step_up.py` (email step-up mints a valid token that passes a gated endpoint; wrong code rejected); regression: TOTP step-up still works.
**Dependencies:** E2, E3 · **Files:** `app/api/v1/endpoints/auth.py`, `app/schemas/auth.py`, tests, `openapi.json` · **Scope:** M

## F2: Step-up modal — factor picker + email code flow
**Description:** The step-up modal currently assumes a TOTP code. Support both factors.

**Acceptance criteria:**
- [ ] When `available_factors` has both → user picks "Authenticator code" or "Email me a code".
      One factor → auto-select. Email path: "Send code" → input → submit → resolver returns the step-up token.
- [ ] Works through the existing `lib/api/step-up.ts` resolver/interceptor — gated requests still
      retry transparently after the modal resolves a token.
- [ ] Recovery-code path (TOTP) preserved.

**Verification:** `npx vitest run` (modal logic / resolver); `npx tsc --noEmit`; manual: trigger a gated action with email-2FA only → email code → action proceeds.
**Dependencies:** E4 (+ F1 for factor availability) · **Files:** `components/auth/step-up-modal.tsx`, `lib/api/step-up.ts`, services · **Scope:** M

### Checkpoint C — Step-up
- [ ] A user with only email-2FA can re-auth a critical action via emailed code; TOTP users unchanged.

### Phase 4 — Login via email (vertical slice: second factor at sign-in)

## E5: Login second factor via email
**Description:** Make `/signin` + `/2fa/login` factor-aware so email-2FA users get a second factor at login.

**Acceptance criteria:**
- [ ] `/signin`: when `has_second_factor` → `TwoFactorRequiredResponse` now also carries
      `factors: ["totp"|"email"...]` (challenge token unchanged).
- [ ] `POST /auth/2fa/login/email/request` (body `{challenge_token}`): validates the challenge,
      sends an `email_2fa_login` code to that user. Rate-limited; no user enumeration in errors.
- [ ] `POST /auth/2fa/login` accepts `{challenge_token, method:"email", code}` (and existing TOTP) →
      verifies → issues the token pair. Single-use; lockout.
- [ ] Non-2FA and TOTP-only login paths byte-for-byte unchanged.

**Verification:** `uv run pytest tests/test_login_2fa.py -k email` (email login happy path, wrong code, challenge required); regression: existing login-2FA tests green.
**Dependencies:** E2, E3 · **Files:** `app/api/v1/endpoints/auth.py`, `app/schemas/auth.py`, tests, `openapi.json` · **Scope:** M

## F3: Login screen — factor picker + email code flow
**Description:** The 2FA login step must offer the email path when available.

**Acceptance criteria:**
- [ ] After password, if `factors` includes `email` → "Email me a code" (calls request) → input → `/2fa/login` email path.
- [ ] Factor picker when both; auto when one. TOTP + recovery-code paths preserved.

**Verification:** `npx vitest run`; `npx tsc --noEmit`; manual: full email-2FA login.
**Dependencies:** E5 · **Files:** `app/login/*` / login components, `lib/api/services/auth*.ts` · **Scope:** M

### Checkpoint D — Complete
- [ ] Email-2FA works as a full factor: enroll → login-by-email → step-up-by-email → disable.
- [ ] TOTP-only and no-2FA users unchanged (regression suite green). Contract regenerated; tsc + vitest green.
- [ ] Docs: `RISK_GOVERNANCE.md`/security note updated (email-2FA factor, Resend prerequisite). Review.

---

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| `is_enabled` call-sites missed → a flow ignores email-2FA | High | E2 centralizes into `has_second_factor`; grep all 3 sites; regression tests on each flow |
| Email deliverability / latency at login (worse UX than TOTP) | Med | Resend prerequisite; clear "code sent" UX + resend button; TOTP stays primary when enrolled |
| User enables email-2FA but loses email access → lockout | Med | Keep TOTP recovery codes; allow either factor when both enrolled; document recovery |
| User enumeration via login-email-request | Med | Generic responses; validate the challenge token first (proves password already passed) |
| Code reuse across purposes (login code used for step-up) | Med | Distinct reserved `action` values per purpose; verify checks action |
| Resend not configured in an env that enabled email-2FA | Med | `available_factors` gates on `email_confirm.is_enabled()`; enroll send surfaces 503 |
| Brute-force of email codes | Med | High-entropy token (T20), per-factor lockout, per-(user,action) rate-limit (already added in review C1) |

## Decisions (confirmed by client 2026-06-21 — locked)

- **Q-A → CONFIRMED:** Email at **login** is a full, user-chosen factor (not merely a TOTP
  fallback). Any enrolled factor is accepted; when both are enrolled the user picks. → E5/F3.
- **Q-B → CONFIRMED:** Enrollment **always verifies by code** (verify-on-enroll) — the account
  email is not implicitly trusted. → E3.
- **Q-C → CONFIRMED:** Enabling email-2FA **counts** as a second factor for the
  `step_up_require_2fa` hard-block (I8) — `has_second_factor` includes email. → E2.
- **Q-D → CONFIRMED:** Disabling the **last** factor is allowed (returns the user to "no 2FA"),
  consistent with current TOTP disable. → E3.

These are baked into the task ACs above; no open questions remain. Implementation NOT started.

## Parallelization

- Sequential: E1 → E2 (foundation; everything depends on E2).
- Parallel after E2/E3: **E4 (step-up)** ∥ **E5 (login)** — independent backend slices.
- Frontend follows its slice: F1←E3, F2←E4, F3←E5; F1/F2/F3 parallelizable once their contract lands.
- Single contract regen per backend phase (Node 20) before the matching frontend task.

## Estimate

Backend E1–E5 ≈ 4–6 focused sessions (S+S+M+M+M). Frontend F1–F3 ≈ 3–4 (M each).
Total ≈ 1.5–2 weeks for one engineer; ~1 week with backend/frontend split after the contracts land.

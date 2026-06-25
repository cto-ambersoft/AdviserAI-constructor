# TODO — Audit Follow-up

> Tracker for [audit-followup-plan.md](audit-followup-plan.md). Scope: audit §2, §3, §5, §7, §8, §9.
> Legend: `[ ]` todo · `[~]` in progress · `[x]` done · 🔴 must · ⚑ needs owner input.
> Out of scope: §1, §4, §6, §10.

## Phase A — Supervisor completeness (§2)
- [x] 🔴 **A1** Leverage ceiling validated vs exchange max (static map binance=125/bybit=100; ValueError→422) — `service.py` (`_exchange_max_leverage`, upsert early-guard + `_apply_risk_config` chokepoint)
  - [x] AC: above-max → ValueError (422); within → ok; no half-created config · 2 tests green
- [x] 🔴 **A2** Bulk apply risk config to all user strategies — `PATCH /live/auto-trade/risk-config/apply-all` (step-up); `apply_risk_config_to_all_strategies` reuses `_apply_risk_config(commit=False)` in one txn (atomic on leverage reject)
  - [x] AC: all configs updated in one call; scoped to caller; atomic leverage reject · 3 tests green (added to `test_auto_trade_service.py`)
- [x] 🔴 **A3** Persist kill-switch / risk-off latch — columns on `auto_trade_configs` (`risk_off_latched/reason/at`, migration `0042`); set in `kill_switch_close_position`, cleared on resume in `set_running(True)`; exposed in `AutoTradeConfigRead`
  - [x] AC: latch survives restart (fresh-session read); resume clears · 2 tests green; migration up/down SQL verified offline
- [x] ✅ **Checkpoint A** — ruff clean (new files); mypy 0 new; migration 0042 up/down SQL verified; full suite 1088 passed / 1 skipped (2 failures are pre-existing date-drift in `test_strategy_health_*`, unrelated — base date 2026-05-20 now outside the 30d window)
- [x] 📝 Document §2.5.5 per-account exposure = covered by design (1 config = 1 sub-account); no code

## Phase B — Manual spot path through supervisor (§3)
- [x] 🔴 **B1** Gate manual-spot OPEN via the supervisor — extracted reusable `evaluate_pre_trade_risk` (pure refactor of the auto-trade gate) + new `precheck_manual_order` (resolves the account's config, opening-only, no-config/no-risk ⇒ allow, emits `risk_blocked`); wired into `endpoints/live.py:_maybe_execute_signal`
  - [x] AC: violation blocked + `risk_blocked`; within limits allowed; no-config unchanged; sell/reduce never gated · 5 tests green (in `test_auto_trade_service.py`)
- [x] ✅ **Checkpoint B** — full suite 1093 passed / 1 skipped (same 2 pre-existing date-drift in `test_strategy_health_*`); gate refactor behaviour-preserving; ruff/mypy 0 new

## Phase C — Rerank (§5) — in `core` repo (branch `feature/constructor-v1`)
- [x] 🔴 **C1** Reranker wired into `qdrant-stream-search` behind a `rerank` input flag (default off) via new testable `rerank-util.ts` (`resolveRerankEnabled` + `applyReranking`); no-key/error ⇒ vector order, never throws
  - [x] AC: off ⇒ identical vector order; on+key ⇒ reranked; no-key ⇒ no crash · 9 unit tests + `nest build` green
- [x] 🔴 **C2** `rerankEnabled` on `AiConfig` (default false) plumbed schema→DTO(zod)→input→resolve→`ResolvedAiConfig`, read in the tool; effective = input ∥ AiConfig ∥ env, all gated on COHERE_API_KEY
  - [x] AC: 3 OFF-by-default sources, toggle via config without redeploy · covered by `resolveRerankEnabled` tests
- [x] **C3** Documented `COHERE_API_KEY` + `RERANK_ENABLED` in `core/.env.example` (key verified working: HTTP 200, `rerank-v3.5`; not committed — set at deploy)
- [x] ✅ **Checkpoint C** — `nest build` clean; core suite 89 passed / 1 pre-existing fail (`binance.spec` live-EMA, unrelated — confirmed identical on baseline); 0 new eslint (pre-existing `openai`-unused only)

## Phase D — Config revisions + rollback (§7)
- [x] 🔴 **D1** Model `AutoTradeConfigRevision` + migration **`0043`** `auto_trade_config_revisions` (config_id, revision_number, content_hash sha256, snapshot_json, actor; append-only, registered in models `__init__`)
  - [x] AC: upgrade/downgrade SQL verified offline
- [x] 🔴 **D2** Write revision on every `upsert_config` (both create+update paths, after `_apply_risk_config`); hash dedup; snapshot excludes runtime state (is_running/lifecycle/risk_off/timestamps)
  - [x] AC: create ⇒ rev1; edit ⇒ rev2; identical re-save ⇒ none · 3 tests
- [x] 🔴 **D3** Rollback `POST /live/auto-trade/config/{id}/rollback/{revision_id}` (step-up + ownership; re-applies snapshot via `upsert_config` → new revision; runtime state untouched)
  - [x] AC: restores fields + new revision; cross-user/foreign-revision → LookupError(404); step-up required · 3 service tests + 2 step-up route tests
- [x] ✅ **Checkpoint D** — ruff clean (new files; fixed a `_upsert_payload` helper-name collision that briefly shadowed the gate tests' helper); mypy 0 new; migration 0043 up/down SQL verified; full suite 1101 passed / 1 skipped (same 2 pre-existing date-drift)

## Phase E — Tests (§8) — ALREADY SATISFIED by the per-task tests (no new code)
> Each phase shipped its own tests (TDD), so Phase E required no separate work — only confirmation.
- [x] 🔴 **E1** revision/rollback — covered in `test_auto_trade_service.py`: `test_upsert_config_records_initial_revision` (create), `test_editing_config_records_new_revision` (immutable: old snapshot unchanged), `test_identical_resave_does_not_duplicate_revision` (hash-dedup), `test_rollback_config_restores_prior_revision`, `..._rejects_other_users_config` (ownership), `..._rejects_revision_of_other_config`; + `test_rollback_config_requires_step_up` (step-up route)
- [x] 🔴 **E2** manual-spot gate — `test_precheck_manual_order_*` ×5 (blocked / passes / no-config-unchanged / no-risk-config / sell-never-gated)
- [x] 🔴 **E3** bulk-apply + leverage-422 + latch — `test_bulk_apply_*` ×3, `test_leverage_ceiling_above/within_exchange_max` ×2, `test_kill_switch_risk_off_latch_persists_across_restart`, `test_resume_clears_risk_off_latch`; + `test_bulk_apply_risk_config_requires_step_up` (step-up route)
- [x] **E-gap** sandbox→live eligibility — confirmed still covered, no gap: `test_sandbox_execution_guard.py` (5) + `test_promotion_kpi_gate.py` (7, live-eligibility criteria) + `test_promotion_{state_machine,service,endpoints,gate_sweep}.py`
- [x] ✅ **Checkpoint E** — confirmation run green: 45 passed (endpoints + sandbox + promotion) + 19 passed (E1/E2/E3 selection). No new tests written (would duplicate existing coverage).

## Phase F — RAG source/freshness in admin (§9) — repos `files-to-vector` (F1) + `admin-panel` (F2)
- [x] 🔴 **F1** `files-to-vector`: `source` (per-collection default) + `ingestedAt` (Date.now default) added to all 5 ingested models, indexed; schema defaults populate on ingest (no route changes); old rows null until re-saved
  - [x] AC: new rows carry source+ingestedAt; old rows unaffected · 5 jest tests (defaults on construction, no DB)
- [x] 🔴 **F2** `admin-panel`: Documents lists/filters `source`+`ingestedAt`; Messari/Blockworks/Delphi/Tweet collections registered as read-only AdminJS resources (nav "Knowledge Base") with source/title/author/url/ingestedAt + filters; models use `strict:false`
  - [x] AC: admin can filter by source + see last-collected time · config smoke-verified (require-hook stub; admin-panel has no jest)
- [x] ✅ **Checkpoint F** — F1 jest 5/5; F2 options.js loads + all resource/property assertions pass

## Owner input needed ⚑
- [ ] A1: static per-exchange leverage map vs adapter fetch? (rec: static)
- [ ] C2: toggle on `AiConfig` vs global env? (rec: `AiConfig.rerankEnabled` + env override)
- [ ] F2: separate AdminJS resources per report collection vs one unified Sources view? (rec: separate)

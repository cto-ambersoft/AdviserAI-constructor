# PLAN — Audit Follow-up (post-M4 hardening)

> Source: 10-point security/risk audit (2026-06-25). This plan covers the **6 items the
> owner greenlit**: §2, §3, §5, §7, §8, §9. Items §1, §4, §6, §10 are explicitly out of
> scope (owner decision — see "Out of scope" below).
> Status: **Draft — awaiting review before implementation.** Read-only; no code changed yet.
> Services touched: `constructor` (FastAPI), `core` (NestJS/Mastra), `admin-panel` (AdminJS),
> `files-to-vector` (ingestion).
> Migration head at planning time: **`20260621_0041`** → new migrations start at `0042`.

---

## 0. Scope & decisions

| Item | Decision | Effort posture |
|---|---|---|
| §2 Supervisor params on all strategies | **DO** — close the gaps, **no extra functionality / no complex logic** | minimal |
| §3 Manual spot path through supervisor | **DO** — gate it **carefully, must not break current trading** | surgical |
| §5 Rerank | **DO** — make it work; **default OFF**, toggle in admin config; key already verified | minimal |
| §7 Append-only config revisions + hash + rollback | **DO** — working impl with **minimal effort** | minimal |
| §8 Tests (revisions/rollback/live-eligibility) | **DO** | per-task |
| §9 RAG source/freshness in admin panel | **DO** — minimal but working | minimal |

**Out of scope (owner):** §1 (no-2FA bypass — accepted risk), §4 (formulas doc — done later separately),
§6 (sandbox gate — already DONE), §10 (tech-model governance — done later separately).

**Pre-verified facts:**
- Cohere key works: `POST https://api.cohere.com/v2/rerank`, model `rerank-v3.5` → HTTP 200, 1 search-unit billed.
- Risk config is per-config 1:1 (`auto_trade_risk_configs`); enforcement already correct per-config.
- Manual spot open path: `app/api/v1/endpoints/live.py:83` (`_maybe_execute_signal`) → `:117` (`place_spot_order`).
- `check_pre_trade` lives in `app/services/auto_trade/risk/engine.py`; auto-trade entry already gated.
- Reranker tool exists but is **not called** in retrieval: `core/src/mastra/tools/reranker.ts`,
  retrieval at `core/src/mastra/tools/qdrant-stream-search.ts` (final slice ~`:1084`).
- Admin list config: `admin-panel/admin/options.js:43` (Documents `listProperties`).

---

## 1. Dependency graph

```
Phase A (Supervisor §2)        Phase C (Rerank §5)         Phase F (RAG admin §9)
  A1 leverage-vs-exchange        C1 wire rerank (off)        F1 ingest writes source+ingestedAt
  A2 bulk apply-to-all           C2 admin toggle  ← C1       F2 admin surfaces+filters ← F1
  A3 kill-switch latch persist   C3 set COHERE key (deploy)
        │                              (independent)               (independent)
        ▼
Phase B (Manual spot §3)
  B1 gate manual-spot open  (uses engine.check_pre_trade; no code dep on A)
        │
        ▼
Phase D (Revisions §7)         Phase E (Tests §8) — after A, B, D
  D1 table+migration 0042         E1 revisions+rollback tests
  D2 write revision on upsert     E2 manual-spot gate tests
  D3 rollback endpoint ← D1,D2    E3 supervisor (bulk/leverage/latch) tests
```

Phases A, B, C, D, F are largely independent and can be built in parallel by area.
**E** depends on A/B/D landing. Each implementation task carries its own unit tests as DoD;
**E** is the cross-cutting sweep that proves the full suite stays green.

---

## 2. Phases & tasks (vertical slices)

Each task is one complete path (model → service → API → test). Conventions to follow:
SQLAlchemy 2.0 `Mapped`/`mapped_column`; Pydantic v2; hand-written Alembic migrations chained to
head; `ruff` + `mypy` clean; reuse existing helpers; new risk behaviour stays **opt-in / fail-safe**.

### Phase A — Supervisor completeness (§2)

> Goal: close the real gaps with **minimal** surface. Note: §2.5.5 (per-account exposure) is
> already covered by design (1 strategy = 1 sub-account → per-user exposure == per-account); we
> **document** this, no code. §5.2/5.4/5.6/5.7/5.8 already enforced per-config.

**A1 — Leverage ceiling validated against the exchange maximum.**
Today `leverage_ceiling` is bounded only by a hardcoded `≤125` (Binance max), so a Bybit
strategy can be set to 110 and silently get rejected by the exchange.
- Add a small per-exchange max-leverage lookup (static map is acceptable — minimal; e.g.
  Binance USDT-M 125, Bybit 100). Prefer the adapter if it already exposes it.
- Validate on risk-config upsert: `leverage_ceiling` (and `config.leverage`) ≤ exchange max for
  the config's account exchange → 422 with a clear message.
- Files: `app/schemas/auto_trade.py` (or validator in service), `app/services/auto_trade/service.py`
  (upsert path ~`:1115`), exchange map near `app/services/exchange/`.
- **AC:** setting `leverage_ceiling` above the account-exchange max → 422; within max → ok;
  pre-trade enforcement unchanged.
- **Verify:** `pytest tests/test_auto_trade_risk_engine.py -k leverage` + a new upsert-validation test.

**A2 — Bulk apply a risk config to ALL of a user's strategies.**
There is no user/admin way to set risk params across all strategies at once.
- New endpoint `PATCH /live/auto-trade/risk-config/apply-all` (step-up gated, `RequireStepUp`):
  body = the same nested `AutoTradeRiskConfig` shape; loop over the user's configs and upsert the
  risk row for each in **one transaction**. No new table, no new abstraction — reuse the existing
  per-config risk upsert.
- Files: `app/api/v1/endpoints/live.py`, `app/services/auto_trade/service.py` (extract the existing
  single-config risk upsert into a reusable helper, call it in a loop).
- **AC:** one call writes identical risk params to every config of the user; each config remains
  individually editable afterward; ownership enforced; step-up required.
- **Verify:** new `tests/test_risk_config_bulk_apply.py` (3 configs → all updated; other user's
  configs untouched).

**A3 — Persist the kill-switch / risk-off latch across restarts.**
Today the risk-off pause is inferred from events; there is no explicit persisted latch.
- Add columns to `auto_trade_configs` (migration `0042`... pick the next free index relative to the
  revisions migration): `risk_off_latched: bool default false`, `risk_off_reason: str|None`,
  `risk_off_at: datetime|None`. Set on kill-switch trip (`service.py` kill-switch close path),
  clear on manual resume (`set_running(True)`), expose in the auto-trade state response.
- **AC:** after a kill-switch trip + process restart, the latch + reason are still readable; manual
  resume clears them; no behavioural change when kill-switch disabled.
- **Verify:** `tests/test_auto_trade_service.py` add a latch-persistence test (set latch → reload
  from DB → still latched → resume → cleared).

**Checkpoint A:** `uv run ruff check . && uv run mypy app && uv run pytest -k "leverage or bulk or latch"`;
migrations up/down clean.

---

### Phase B — Manual spot path through the supervisor (§3)

**B1 — Gate the manual-spot OPEN path through `check_pre_trade`.**
`_maybe_execute_signal` (`live.py:83`) calls `place_spot_order` (`:117`) without the pre-trade
engine. Add the gate **only for orders that OPEN/increase exposure**; never touch close/reduce.
- Before the opening `place_spot_order`, build the minimal `check_pre_trade` context (user, symbol,
  intended side/size, leverage=1 for spot) and call the engine. If blocked → skip the order, emit a
  `risk_blocked` event, return the same "no execution" shape the caller already handles.
- **Fail-safe / non-breaking:** if the strategy has no risk config or it's disabled → behave exactly
  as today (current logic unchanged). Reuse the existing engine; do not duplicate rules.
- Files: `app/api/v1/endpoints/live.py` (`_maybe_execute_signal`), possibly a thin wrapper in
  `app/services/execution/trading_service.py`.
- **AC:** a manual spot OPEN that violates a configured rule is blocked + `risk_blocked` event;
  within limits → executes exactly as before; no-risk-config → identical to today; closes/reduces
  never gated.
- **Verify:** `tests/test_manual_spot_risk_gate.py` (blocked / passes / no-config-unchanged /
  close-unaffected). Run the full `live` endpoint tests to prove no regression.

**Checkpoint B:** full `uv run pytest` green (esp. existing live/paper tests) — proves trading logic intact.

---

### Phase C — Rerank wired, default-off, admin-toggleable (§5)

> Cohere key verified working. Keep retrieval behaviour **identical when off**.

**C1 — Wire the reranker into the retrieval path behind a flag (default off).**
- Add optional input `rerank?: boolean` (default `false`) to `qdrantStreamSearch`. When `true`:
  after candidate assembly and before the final `.slice(0, limit)` (~`:1084`), call
  `rerankDocuments` on the top candidates, then take top-`limit`. On **any** error or missing key →
  log + fall back to current ordering (never throw). Verified against Cohere v2 rerank docs (context7).
- Files: `core/src/mastra/tools/qdrant-stream-search.ts`, `core/src/mastra/tools/reranker.ts` (reuse).
- **AC:** flag off → byte-identical result ordering to today; flag on + key → reranked order;
  key missing → no crash, graceful fallback.
- **Verify:** core unit test for the tool with rerank on/off; `npm run build` green.

**C2 — Make the toggle configurable from admin config (default off).**
- Add `rerankEnabled` (default `false`) to the AI config (`AiConfig` schema in
  `core/src/analysis`), editable through the existing AI-config admin surface; the research/twitter
  agents pass it into `qdrantStreamSearch`. Env `RERANK_ENABLED` as an override fallback.
- **AC:** toggling `rerankEnabled` in config flips behaviour without code change/redeploy.
- **Verify:** test that the agent forwards the flag; manual toggle smoke.

**C3 — Provision `COHERE_API_KEY` in deployment (default off, so non-blocking).**
- Put the verified key in `core` env/secret store. Document that rerank stays off until `rerankEnabled`.
- **AC:** key present where `core` runs; rerank works when enabled. (Key already validated: HTTP 200.)

**Checkpoint C:** `core` build + tests green; manual smoke: enable flag → response reranked; disable → unchanged.

---

### Phase D — Append-only config revisions + content hash + rollback (§7)

> Minimal, working design: one snapshot table + write-on-upsert + a rollback that re-applies a
> snapshot through the **existing** upsert path (so rollback itself is auditable).

**D1 — Revisions table + migration.**
- New model `app/models/auto_trade_config_revision.py` → table `auto_trade_config_revisions`:
  `id` PK, `config_id` FK(index), `revision_number` (int, per-config), `content_hash` (str, sha256),
  `snapshot_json` (JSON of the canonical config fields), `actor` (str|None), `created_at`.
  Append-only (no update/delete in code).
- Migration `20260622_0042_add_auto_trade_config_revisions.py`, `down_revision="...0041"`.
- **AC:** `alembic upgrade head` and `downgrade -1` clean.

**D2 — Write a revision on every config create/update.**
- In `upsert_config` (`service.py:1115`): build canonical JSON of the persisted config fields,
  `content_hash = sha256(...)`; insert a revision **only if the hash differs** from the latest
  (no dup on identical re-save); `revision_number = last + 1`.
- **AC:** editing a config inserts an immutable revision with incrementing number + hash; identical
  re-save inserts nothing; concurrent edits don't collide (rely on per-config ordering).
- **Verify:** unit test (edit → revision; re-save same → no new row).

**D3 — Rollback endpoint.**
- `POST /live/auto-trade/config/{config_id}/rollback/{revision_id}` (`RequireStepUp`, ownership):
  load the snapshot, re-apply it through `upsert_config` (which writes a *new* revision capturing
  the rollback). Lifecycle stage is **not** rolled back (stays as-is — rollback is config-content only).
- **AC:** rollback restores the prior field values + creates a new revision; cannot roll back another
  user's config; requires step-up.
- **Verify:** covered in Phase E.

**Checkpoint D:** `ruff`/`mypy` clean; migrations up/down; revision + rollback unit tests green.

---

### Phase E — Tests (§8)

> Promotion FSM / KPI-gate / sandbox live-eligibility are **already covered** (~34 tests). This phase
> adds the missing coverage for the new work and the previously-untested gaps.

**E1 — Revision history + rollback** (`tests/test_config_revisions.py`):
created-on-edit, immutability, hash dedup, rollback restores fields + new revision, ownership, step-up.

**E2 — Manual-spot pre-trade gate** (`tests/test_manual_spot_risk_gate.py`):
blocked on violation + event; passes within limits; no-config unchanged; closes never gated.

**E3 — Supervisor gaps** (`tests/test_risk_config_bulk_apply.py`, leverage + latch in existing files):
bulk apply to all configs; leverage-vs-exchange 422; kill-switch latch persists across reload.

**E-live-eligibility (gap check):** confirm existing `test_sandbox_execution_guard.py` +
`test_promotion_*` still cover sandbox→live; add a test only if a gap is found.

**Checkpoint E:** full `uv run pytest` green; record new test count.

---

### Phase F — RAG source/freshness in admin panel (§9)

**F1 — Ingestion writes `source` + `ingestedAt` into Mongo.**
- In `files-to-vector` processors (Document/Messari/Blockworks/Delphi/Tweet), set `source` and
  `ingestedAt = new Date()` on the Mongo upsert (data already exists in the Qdrant payload — just
  mirror it into Mongo). Add the two fields to the corresponding Mongoose models.
- **AC:** newly ingested rows carry `source` + `ingestedAt`; existing rows unaffected (nullable).

**F2 — Admin panel surfaces + filters source/freshness.**
- `admin-panel`: add `source`, `ingestedAt`, `url` to the Documents `listProperties`
  (`admin/options.js:43`) and enable them as filters; register the report collections as AdminJS
  resources (or a combined view) so an admin can browse per-source + see last-collected time.
  Verified against AdminJS resource/property options (context7).
- **AC:** an admin can list and **filter by source** and see **last-collected (`ingestedAt`)** per
  document/source.
- **Verify:** manual admin smoke (load admin → filter by source → see ingestedAt column).

**Checkpoint F:** admin loads; filtering by source works; ingestedAt visible.

---

## 3. Cross-cutting safety rules

- New risk behaviour is **opt-in / fail-safe**: missing/disabled risk config ⇒ today's behaviour.
- §3 gate touches **only opening orders**; closes/reduces are never gated.
- §5 rerank is **off by default**; any rerank error falls back to current ordering (never throws).
- Migrations hand-written, chained to head, up/down verified; never `--autogenerate`.
- `ruff` + `mypy` + full `pytest` green before any task is "done".
- Confirm library specifics via context7 during build: Cohere v2 rerank, SQLAlchemy 2.0 / Alembic
  batch ops, AdminJS resource options.

## 4. Suggested order

1. **D** (revisions) + **A** (supervisor) — backend, independent, highest audit value.
2. **B** (manual-spot gate) — after the engine is fresh in context.
3. **C** (rerank) — isolated in `core`.
4. **F** (admin RAG) — isolated in `admin-panel`/`files-to-vector`.
5. **E** — verification sweep once A/B/D land.

## 5. Open questions for the owner

- A1: prefer a **static per-exchange max-leverage map** (zero network, minimal) vs fetching from the
  adapter at upsert? (Recommend static map — minimal.)
- C2: where should the toggle live — on `AiConfig` (per-config, admin-editable) or a single global
  env switch? (Recommend `AiConfig.rerankEnabled` so it's admin-toggleable per the ask; env as override.)
- F2: register the 4 report collections as separate AdminJS resources, or one unified "Sources"
  read-only view? (Recommend separate resources — least code.)

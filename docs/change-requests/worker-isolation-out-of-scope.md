# Change note — Per-strategy worker isolation is out of M4 scope

**Date:** 2026-06-20 · **Decision:** client-confirmed (Q3) · **Audit ref:** W7c

## Summary

Per-strategy **worker isolation** (a dedicated Taskiq worker / queue per strategy
with independent lifecycle management) is **excluded from the Milestone 4 scope**
by agreement with the client.

## Rationale

- The M4 Plan of Works lists Worker Isolation as an **optional** item ("опциональный
  пункт; архитектура спроектирована с заделом на расширение, реализация может быть
  скорректирована по согласованию сторон").
- It is **not** one of the seven Acceptance Criteria.
- The isolation guarantees that matter for AC#3 (Multi-Strategy ≥3 without signal
  collisions) are already met by other means:
  - **one strategy = one exchange sub-account** (`UniqueConstraint(user_id, account_id)`),
  - a partial unique index capping each account at one open position
    (`uq_auto_trade_positions_user_account_open`),
  - serialized signal processing via `SELECT … FOR UPDATE SKIP LOCKED`,
  - the pre-trade conflicting-signal rule.

The current single `RedisStreamBroker` worker pool processes all strategies safely
under these guarantees.

## Status

Not implemented in M4. May be revisited in a later milestone if per-strategy
resource isolation (CPU/memory/blast-radius) becomes a requirement.

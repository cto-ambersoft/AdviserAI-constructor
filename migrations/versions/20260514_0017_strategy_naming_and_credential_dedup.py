"""strategy naming and credential dedup for multi-strategy support

Revision ID: 20260514_0017
Revises: 20260513_0016
Create Date: 2026-05-14 00:00:00

W7 of Milestone 4 — Multi-Strategy Account Partitioning.

This migration adds two narrow columns and one partial unique index so the
platform can safely host ≥3 strategies per user via separate exchange
sub-accounts:

  1) ``auto_trade_configs.strategy_name`` — optional human label that the
     frontend strategy switcher shows next to the profile symbol. NULL is
     valid (frontend falls back to profile.symbol).
  2) ``exchange_credentials.api_key_hash`` — sha256 of the decrypted api_key.
     Backfilled inline for existing rows.
  3) Partial unique index ``uq_exchange_credentials_user_api_key_hash`` on
     ``(user_id, api_key_hash) WHERE api_key_hash IS NOT NULL`` — rejects
     two credential rows that point at the same physical sub-account, which
     would otherwise undo the per-credential isolation that defines a
     "strategy" in the W7 model.

The backfill is best-effort: any row whose api_key cannot be decrypted
(missing or rotated SECRETS_ENCRYPTION_KEY) is left with hash=NULL and
will be re-hashed lazily on first interaction. The partial index tolerates
NULLs so this is safe.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260514_0017"
down_revision: str | None = "20260513_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _get_column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _get_index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _backfill_api_key_hashes(connection: sa.engine.Connection) -> int:
    """Decrypt + hash existing credentials.

    Returns the number of rows updated. Failures are swallowed per-row so a
    single corrupt cipher does not block the migration; affected rows keep
    hash=NULL and will be repaired on next service call.
    """

    try:
        from app.core.config import get_settings
        from app.core.security import SecretCipher
    except Exception:  # pragma: no cover — alembic env without app deps
        return 0

    try:
        cipher = SecretCipher(get_settings().encryption_key)
    except Exception:  # pragma: no cover — encryption key not configured
        return 0

    rows = connection.execute(
        sa.text(
            "SELECT id, encrypted_api_key FROM exchange_credentials "
            "WHERE api_key_hash IS NULL"
        )
    ).fetchall()

    updated = 0
    for row in rows:
        cred_id = row[0]
        encrypted_api_key = row[1]
        try:
            api_key = cipher.decrypt(encrypted_api_key)
        except Exception:
            continue
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        connection.execute(
            sa.text(
                "UPDATE exchange_credentials SET api_key_hash = :h WHERE id = :id"
            ),
            {"h": digest, "id": cred_id},
        )
        updated += 1
    return updated


def upgrade() -> None:
    # 1) AutoTradeConfig.strategy_name
    if "strategy_name" not in _get_column_names("auto_trade_configs"):
        with op.batch_alter_table("auto_trade_configs") as batch_op:
            batch_op.add_column(sa.Column("strategy_name", sa.String(length=64), nullable=True))

    # 2) ExchangeCredential.api_key_hash
    if "api_key_hash" not in _get_column_names("exchange_credentials"):
        with op.batch_alter_table("exchange_credentials") as batch_op:
            batch_op.add_column(sa.Column("api_key_hash", sa.String(length=64), nullable=True))

    # 3) Best-effort inline backfill. Safe to re-run (only touches NULLs).
    _backfill_api_key_hashes(op.get_bind())

    # 4) Partial unique index — rejects duplicate physical sub-account
    #    registrations per user, lets legacy NULL rows coexist. The partial
    #    index covers (user_id, api_key_hash) lookups used by the dedup
    #    check in ``ExchangeCredentialsService.create_account``.
    if "uq_exchange_credentials_user_api_key_hash" not in _get_index_names(
        "exchange_credentials"
    ):
        op.create_index(
            "uq_exchange_credentials_user_api_key_hash",
            "exchange_credentials",
            ["user_id", "api_key_hash"],
            unique=True,
            postgresql_where=sa.text("api_key_hash IS NOT NULL"),
            sqlite_where=sa.text("api_key_hash IS NOT NULL"),
        )


def downgrade() -> None:
    if "uq_exchange_credentials_user_api_key_hash" in _get_index_names("exchange_credentials"):
        op.drop_index(
            "uq_exchange_credentials_user_api_key_hash",
            table_name="exchange_credentials",
        )
    if "api_key_hash" in _get_column_names("exchange_credentials"):
        with op.batch_alter_table("exchange_credentials") as batch_op:
            batch_op.drop_column("api_key_hash")
    if "strategy_name" in _get_column_names("auto_trade_configs"):
        with op.batch_alter_table("auto_trade_configs") as batch_op:
            batch_op.drop_column("strategy_name")

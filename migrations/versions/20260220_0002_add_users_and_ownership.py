"""add users and ownership

Revision ID: 20260220_0002
Revises: 20260212_0001
Create Date: 2026-02-20 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260220_0002"
down_revision: str | None = "20260212_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=False)

    op.add_column(
        "strategies",
        sa.Column("user_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "exchange_credentials",
        sa.Column("user_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_strategies_user_id", "strategies", ["user_id"], unique=False)
    op.create_index(
        "ix_exchange_credentials_user_id", "exchange_credentials", ["user_id"], unique=False
    )
    op.create_foreign_key(
        "fk_strategies_user_id_users",
        "strategies",
        "users",
        ["user_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_exchange_credentials_user_id_users",
        "exchange_credentials",
        "users",
        ["user_id"],
        ["id"],
    )

    bind = op.get_bind()
    bootstrap_email = "system@local"
    bootstrap_password = "__bootstrap_not_for_login__"
    insert_user = sa.text(
        """
        INSERT INTO users (email, hashed_password, is_active)
        SELECT :email, :hashed_password, true
        WHERE EXISTS (SELECT 1 FROM strategies WHERE user_id IS NULL)
           OR EXISTS (SELECT 1 FROM exchange_credentials WHERE user_id IS NULL)
        ON CONFLICT (email) DO NOTHING
        """
    )
    bind.execute(
        insert_user,
        {"email": bootstrap_email, "hashed_password": bootstrap_password},
    )

    user_id_result = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :email"),
        {"email": bootstrap_email},
    ).scalar()
    if user_id_result is not None:
        bind.execute(
            sa.text("UPDATE strategies SET user_id = :user_id WHERE user_id IS NULL"),
            {"user_id": user_id_result},
        )
        bind.execute(
            sa.text("UPDATE exchange_credentials SET user_id = :user_id WHERE user_id IS NULL"),
            {"user_id": user_id_result},
        )

    op.alter_column("strategies", "user_id", nullable=False)
    op.alter_column("exchange_credentials", "user_id", nullable=False)

    op.drop_constraint("strategies_name_key", "strategies", type_="unique")
    op.create_unique_constraint("uq_strategies_user_id_name", "strategies", ["user_id", "name"])


def downgrade() -> None:
    op.drop_constraint("uq_strategies_user_id_name", "strategies", type_="unique")
    op.create_unique_constraint("strategies_name_key", "strategies", ["name"])

    op.drop_constraint(
        "fk_exchange_credentials_user_id_users",
        "exchange_credentials",
        type_="foreignkey",
    )
    op.drop_constraint("fk_strategies_user_id_users", "strategies", type_="foreignkey")
    op.drop_index("ix_exchange_credentials_user_id", table_name="exchange_credentials")
    op.drop_index("ix_strategies_user_id", table_name="strategies")
    op.drop_column("exchange_credentials", "user_id")
    op.drop_column("strategies", "user_id")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

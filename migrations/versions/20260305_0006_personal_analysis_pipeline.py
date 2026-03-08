"""add personal analysis pipeline tables

Revision ID: 20260305_0006
Revises: 20260225_0005
Create Date: 2026-03-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260305_0006"
down_revision: str | None = "20260225_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "personal_analysis_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("query_prompt", sa.Text(), nullable=True),
        sa.Column("agents", sa.JSON(), nullable=False),
        sa.Column("agent_weights", sa.JSON(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_personal_analysis_profiles_user_id_is_active",
        "personal_analysis_profiles",
        ["user_id", "is_active"],
        unique=False,
    )
    op.create_index(
        "ix_personal_analysis_profiles_is_active_next_run_at",
        "personal_analysis_profiles",
        ["is_active", "next_run_at"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_personal_analysis_profiles_interval_minutes",
        "personal_analysis_profiles",
        "interval_minutes >= 5 AND interval_minutes <= 1440",
    )

    op.create_table(
        "personal_analysis_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("core_job_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("core_deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["profile_id"], ["personal_analysis_profiles.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("core_job_id"),
    )
    op.create_index(
        "ix_personal_analysis_jobs_status_next_poll_at",
        "personal_analysis_jobs",
        ["status", "next_poll_at"],
        unique=False,
    )
    op.create_index(
        "ix_personal_analysis_jobs_user_id_created_at",
        "personal_analysis_jobs",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_personal_analysis_jobs_attempt_bounds",
        "personal_analysis_jobs",
        "attempt >= 1 AND max_attempts >= 1 AND attempt <= max_attempts",
    )

    op.create_table(
        "personal_analysis_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("trade_job_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("analysis_data", sa.JSON(), nullable=False),
        sa.Column("core_completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["profile_id"], ["personal_analysis_profiles.id"]),
        sa.ForeignKeyConstraint(["trade_job_id"], ["personal_analysis_jobs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trade_job_id"),
    )
    op.create_index(
        "ix_personal_analysis_history_user_id_created_at",
        "personal_analysis_history",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_personal_analysis_history_profile_id_created_at",
        "personal_analysis_history",
        ["profile_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_personal_analysis_history_profile_id_created_at",
        table_name="personal_analysis_history",
    )
    op.drop_index(
        "ix_personal_analysis_history_user_id_created_at",
        table_name="personal_analysis_history",
    )
    op.drop_table("personal_analysis_history")

    op.drop_constraint(
        "ck_personal_analysis_jobs_attempt_bounds",
        "personal_analysis_jobs",
        type_="check",
    )
    op.drop_index(
        "ix_personal_analysis_jobs_user_id_created_at",
        table_name="personal_analysis_jobs",
    )
    op.drop_index(
        "ix_personal_analysis_jobs_status_next_poll_at",
        table_name="personal_analysis_jobs",
    )
    op.drop_table("personal_analysis_jobs")

    op.drop_constraint(
        "ck_personal_analysis_profiles_interval_minutes",
        "personal_analysis_profiles",
        type_="check",
    )
    op.drop_index(
        "ix_personal_analysis_profiles_is_active_next_run_at",
        table_name="personal_analysis_profiles",
    )
    op.drop_index(
        "ix_personal_analysis_profiles_user_id_is_active",
        table_name="personal_analysis_profiles",
    )
    op.drop_table("personal_analysis_profiles")

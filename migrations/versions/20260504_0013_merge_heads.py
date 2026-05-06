"""merge auto-trade pipeline heads

Revision ID: 20260504_0013
Revises: 20260404_0012, 20260407_0011
Create Date: 2026-05-04 00:00:00

"""
from collections.abc import Sequence

revision: str = "20260504_0013"
down_revision: str | Sequence[str] | None = ("20260404_0012", "20260407_0011")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

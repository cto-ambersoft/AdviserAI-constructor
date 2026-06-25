from sqlalchemy import JSON, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AutoTradeConfigRevision(Base, TimestampMixin):
    """Append-only revision history of a strategy's editable config content (§7).

    One immutable row per content change to an :class:`AutoTradeConfig` (and its
    1:1 risk row): a monotonically increasing ``revision_number``, a ``content_hash``
    (sha256 of the canonical snapshot) for cheap change-detection/dedup, and the
    full ``snapshot_json`` so a prior revision can be re-applied (rollback). The
    service never updates or deletes a row here — a rollback writes a *new* revision
    capturing the restored state.
    """

    __tablename__ = "auto_trade_config_revisions"
    __table_args__ = (
        Index(
            "ix_auto_trade_config_revisions_config_number",
            "config_id",
            "revision_number",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("auto_trade_configs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False)
    # Who/what produced this revision: 'user' (upsert), 'rollback', ...
    actor: Mapped[str | None] = mapped_column(String(24), nullable=True)

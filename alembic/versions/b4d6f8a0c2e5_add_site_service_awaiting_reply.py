"""Ждём ответа клиенту: отметка, уведомление и эскалация по каждому сообщению."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b4d6f8a0c2e5"
down_revision = "d6e8f0a2b4c6"
branch_labels = None
depends_on = None

_COLUMNS = (
    "awaiting_reply_since",
    "awaiting_reply_notified_at",
    "awaiting_reply_escalated_at",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(
            "site_service_request_case",
            sa.Column(column, sa.DateTime(timezone=True), nullable=True),
        )
    op.create_index(
        "ix_site_service_request_case_awaiting_reply",
        "site_service_request_case",
        ["awaiting_reply_since"],
        postgresql_where=sa.text("awaiting_reply_since IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_service_request_case_awaiting_reply",
        table_name="site_service_request_case",
    )
    for column in reversed(_COLUMNS):
        op.drop_column("site_service_request_case", column)

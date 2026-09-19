"""Автозакрытие обращений сайта, где клиент замолчал после ответа."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c7e9a1b3d5f2"
down_revision = "b4d6f8a0c2e5"
branch_labels = None
depends_on = None


_COLUMNS = ("auto_closed_at", "auto_close_checked_at")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(
            "site_service_request_case",
            sa.Column(column, sa.DateTime(timezone=True), nullable=True),
        )
    # Лента автозакрытия берёт давно не проверявшиеся открытые карточки.
    op.create_index(
        "ix_site_service_request_case_auto_close",
        "site_service_request_case",
        ["auto_close_checked_at"],
        postgresql_where=sa.text("auto_closed_at IS NULL AND closed_without_response_at IS NULL"),
    )
    # Индекс из b4d6f8a0c2e5 существует в базе, но не был объявлен в модели —
    # объявление добавлено этим же изменением, чтобы autogenerate не дрейфовал.


def downgrade() -> None:
    op.drop_index(
        "ix_site_service_request_case_auto_close",
        table_name="site_service_request_case",
    )
    for column in reversed(_COLUMNS):
        op.drop_column("site_service_request_case", column)

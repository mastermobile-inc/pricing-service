"""add site service administrative close without response

Revision ID: c9e1a3b5d7f2
Revises: e8f9012345a6
Create Date: 2026-09-12 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c9e1a3b5d7f2"
down_revision: str | None = "e8f9012345a6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_case",
        sa.Column("closed_without_response_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("close_without_response_reason", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("site_service_request_case", "close_without_response_reason")
    op.drop_column("site_service_request_case", "closed_without_response_at")

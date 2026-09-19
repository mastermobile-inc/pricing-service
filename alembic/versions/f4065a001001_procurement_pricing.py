"""Task 4065: immutable pricing decisions and personal filters."""

import sqlalchemy as sa

from alembic import op

revision = "f4065a001001"
down_revision = "c6d7e8f90123"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "procurement_price_batch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_key", sa.String(64), nullable=False, unique=True),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("message_id", sa.String(64), unique=True),
        sa.Column("document_ref", sa.String(64)),
        sa.Column("document_number", sa.String(64)),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_procurement_price_batch_owner", "procurement_price_batch", ["owner"])
    op.create_index("ix_procurement_price_batch_status", "procurement_price_batch", ["status"])
    op.create_table(
        "procurement_price_line",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "batch_id", sa.Integer(), sa.ForeignKey("procurement_price_batch.id"), nullable=False
        ),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("price_type", sa.String(16), nullable=False),
        sa.Column("old_price", sa.String(32)),
        sa.Column("new_price", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.UniqueConstraint("batch_id", "code", "price_type"),
    )
    op.create_index("ix_procurement_price_line_batch_id", "procurement_price_line", ["batch_id"])
    op.create_table(
        "procurement_price_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "batch_id", sa.Integer(), sa.ForeignKey("procurement_price_batch.id"), nullable=False
        ),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_procurement_price_event_batch_id", "procurement_price_event", ["batch_id"])
    op.create_table(
        "procurement_price_preset",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner", "name"),
    )
    op.create_index("ix_procurement_price_preset_owner", "procurement_price_preset", ["owner"])


def downgrade():
    for table in (
        "procurement_price_preset",
        "procurement_price_event",
        "procurement_price_line",
        "procurement_price_batch",
    ):
        op.drop_table(table)

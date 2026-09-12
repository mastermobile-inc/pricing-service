"""Bitrix drivers, successful snapshots and draft driver audit."""

import sqlalchemy as sa

from alembic import op

revision = "d6e8f0a2b4c6"
down_revision = "c9e1a3b5d7f2"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind in [
        ("bitrix_user_id", sa.String(64)),
        ("work_position", sa.String(255)),
        ("shift_status", sa.String(32)),
        ("shift_checked_at", sa.DateTime()),
        ("synced_at", sa.DateTime()),
    ]:
        op.add_column("logistics_driver", sa.Column(name, kind, nullable=True))
    op.create_index(
        "ix_logistics_driver_bitrix_user_id", "logistics_driver", ["bitrix_user_id"], unique=True
    )
    op.create_table(
        "logistics_sync_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False, unique=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "logistics_draft_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("logistics_draft.id"), nullable=False),
        sa.Column(
            "actor_user_id", sa.Integer(), sa.ForeignKey("logistics_user.id"), nullable=False
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("logistics_draft_audit")
    op.drop_table("logistics_sync_status")
    op.drop_index("ix_logistics_driver_bitrix_user_id", table_name="logistics_driver")
    for name in (
        "synced_at",
        "shift_checked_at",
        "shift_status",
        "work_position",
        "bitrix_user_id",
    ):
        op.drop_column("logistics_driver", name)

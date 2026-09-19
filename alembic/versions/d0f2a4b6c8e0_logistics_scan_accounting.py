"""Durable scan-led accounting acknowledgements and final goods counts."""

import sqlalchemy as sa

from alembic import op

revision = "d0f2a4b6c8e0"
down_revision = "c9e1a3b5d7f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "logistics_accounting_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "unit_id",
            sa.Integer(),
            sa.ForeignKey("logistics_order_plan_unit.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "physical_event_id",
            sa.Integer(),
            sa.ForeignKey("logistics_transfer_event.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime()),
        sa.UniqueConstraint("unit_id", "operation", name="uq_logistics_accounting_unit_operation"),
    )
    op.create_table(
        "logistics_receipt_check",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "unit_id",
            sa.Integer(),
            sa.ForeignKey("logistics_order_plan_unit.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("logistics_user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "physical_event_id",
            sa.Integer(),
            sa.ForeignKey("logistics_transfer_event.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("damaged", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    # Removing persisted physical/accounting evidence is never a code rollback.
    raise RuntimeError("Disable scan-led commands; retain receipt and accounting evidence")

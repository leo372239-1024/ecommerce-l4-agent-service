"""Bounded latest-receipt lookup; exact operation reconciliation uses the primary key."""

from alembic import op

revision = "0006_bounded_receipts"
down_revision = "0005_handoff_cases"
branch_labels = depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX proposals_order_receipts ON business_proposals(order_id,customer_id,created_at DESC)"
    )


def downgrade():
    op.drop_index("proposals_order_receipts", table_name="business_proposals")

"""Infrastructure only; business/agent tables are added in later reviewed stages."""

import sqlalchemy as sa
from alembic import op

revision = "0001_infrastructure"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(128), primary_key=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
    )


def downgrade():
    op.drop_table("worker_heartbeats")
    # The extension can be shared by later tables; never remove it automatically.

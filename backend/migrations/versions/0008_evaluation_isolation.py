"""Freeze evaluation runs out of learning and workflow reuse."""

from alembic import op

revision = "0008_evaluation_isolation"
down_revision = "0007_context_memory"
branch_labels = depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN dataset_partition text NOT NULL DEFAULT 'development' CHECK(dataset_partition IN ('development','evaluation'))"
    )


def downgrade():
    op.drop_column("agent_runs", "dataset_partition")

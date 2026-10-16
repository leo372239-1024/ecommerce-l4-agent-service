"""Owned ordered conversations, independent of graph execution identities."""

from alembic import op

revision = "0010_conversations"
down_revision = "0009_security_identity"
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE agent_conversations (
      id uuid PRIMARY KEY, customer_id text NOT NULL REFERENCES customer_accounts(id),
      next_seq integer NOT NULL DEFAULT 1 CHECK(next_seq BETWEEN 1 AND 201),
      created_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL DEFAULT now()+interval '30 days',
      deleted_at timestamptz, UNIQUE(id,customer_id)
    )""")
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN conversation_id uuid, ADD COLUMN conversation_seq integer"
    )
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT owned_conversation_fk FOREIGN KEY(conversation_id,customer_id) REFERENCES agent_conversations(id,customer_id)"
    )
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT conversation_pair CHECK ((conversation_id IS NULL) = (conversation_seq IS NULL))"
    )
    op.execute(
        "CREATE UNIQUE INDEX conversation_turn_unique ON agent_runs(conversation_id,conversation_seq) WHERE conversation_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX conversation_owner ON agent_conversations(customer_id,created_at DESC)"
    )
    op.execute("CREATE INDEX run_owner_created ON agent_runs(customer_id,created_at DESC)")


def downgrade():
    op.execute("DROP INDEX run_owner_created")
    op.execute("ALTER TABLE agent_runs DROP COLUMN conversation_id, DROP COLUMN conversation_seq")
    op.drop_table("agent_conversations")

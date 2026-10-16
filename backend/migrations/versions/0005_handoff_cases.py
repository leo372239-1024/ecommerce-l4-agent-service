"""Persistent staff ownership and auditable closure of handoff cases."""

from alembic import op

revision = "0005_handoff_cases"
down_revision = "0004_operation_reconcile"
branch_labels = depends_on = None


def upgrade():
    ddl = """
    CREATE TABLE handoff_cases (
      id uuid PRIMARY KEY, workflow_id uuid UNIQUE NOT NULL REFERENCES agent_runs(id),
      customer_id text NOT NULL REFERENCES customer_accounts(id), tenant_id text NOT NULL,
      order_ids text[] NOT NULL, status text NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLAIMED','CLOSED')),
      revision bigint NOT NULL DEFAULT 1, assigned_to text REFERENCES customer_accounts(id),
      packet jsonb NOT NULL, resolution text, created_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz
    );
    CREATE INDEX cases_dispatch ON handoff_cases(tenant_id,status,created_at);
    CREATE TABLE case_audit (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, case_id uuid NOT NULL REFERENCES handoff_cases(id),
      actor_id text NOT NULL REFERENCES customer_accounts(id), action text NOT NULL,
      revision bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    """
    for statement in ddl.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError("Human case audit must be preserved")

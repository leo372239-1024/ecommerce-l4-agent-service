"""Durable runs, authenticated sessions, event log and read-only business simulator."""

from alembic import op

revision = "0002_agent_core"
down_revision = "0001_infrastructure"
branch_labels = None
depends_on = None


def upgrade():
    schema = """
        CREATE TABLE customer_accounts (
          id text PRIMARY KEY, username text UNIQUE NOT NULL, password_hash text NOT NULL
        );
        CREATE TABLE customer_sessions (
          token_hash text PRIMARY KEY, customer_id text NOT NULL REFERENCES customer_accounts(id),
          csrf_hash text NOT NULL, expires_at timestamptz NOT NULL
        );
        CREATE INDEX sessions_expiry ON customer_sessions(expires_at);
        CREATE TABLE commerce_orders (
          id text PRIMARY KEY, customer_id text NOT NULL REFERENCES customer_accounts(id),
          version bigint NOT NULL CHECK (version > 0), payload jsonb NOT NULL
        );
        CREATE TABLE agent_runs (
          id uuid PRIMARY KEY, customer_id text NOT NULL REFERENCES customer_accounts(id),
          message text NOT NULL, request_key text NOT NULL, request_hash text NOT NULL,
          status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','WAITING_USER','SUCCEEDED',
            'PARTIAL','HANDOFF','FAILED','CANCELLED')),
          revision bigint NOT NULL DEFAULT 1, generation integer NOT NULL DEFAULT 1,
          invocation_id uuid NOT NULL, resume_input jsonb, cancel_requested boolean NOT NULL DEFAULT false,
          result jsonb, event_seq bigint NOT NULL DEFAULT 0,
          lease_owner text, lease_until timestamptz, fencing bigint NOT NULL DEFAULT 0,
          active_seconds double precision NOT NULL DEFAULT 0, active_accounted_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (customer_id, request_key)
        );
        CREATE INDEX runs_dispatch ON agent_runs(status, lease_until, created_at);
        CREATE INDEX runs_customer ON agent_runs(customer_id, created_at DESC);
        CREATE TABLE agent_events (
          workflow_id uuid NOT NULL REFERENCES agent_runs(id), seq bigint NOT NULL,
          event_key text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(workflow_id, seq), UNIQUE(workflow_id, event_key)
        );
        CREATE TABLE agent_outbox (
          workflow_id uuid NOT NULL, seq bigint NOT NULL, delivered_at timestamptz,
          PRIMARY KEY(workflow_id, seq),
          FOREIGN KEY(workflow_id, seq) REFERENCES agent_events(workflow_id, seq)
        );
        CREATE INDEX outbox_pending ON agent_outbox(workflow_id, seq) WHERE delivered_at IS NULL;
    """
    # asyncpg prepares one statement at a time. This is fixed DDL, with no literal semicolons.
    for statement in schema.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    for table in (
        "agent_outbox",
        "agent_events",
        "agent_runs",
        "commerce_orders",
        "customer_sessions",
        "customer_accounts",
    ):
        op.execute(f"DROP TABLE {table}")

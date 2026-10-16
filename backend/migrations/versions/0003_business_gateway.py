"""Tool catalog, merchant policy, bound approval and transactional operation receipts."""

from alembic import op

revision = "0003_business_gateway"
down_revision = "0002_agent_core"
branch_labels = depends_on = None


def upgrade():
    ddl = """
    ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
    ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check CHECK(status IN
      ('QUEUED','RUNNING','WAITING_USER','WAITING_APPROVAL','SUCCEEDED','PARTIAL','HANDOFF','FAILED','CANCELLED'));
    ALTER TABLE customer_accounts ADD COLUMN role text NOT NULL DEFAULT 'customer'
      CHECK (role IN ('customer','support','supervisor'));
    ALTER TABLE customer_accounts ADD COLUMN tenant_id text NOT NULL DEFAULT 'demo-shop';
    ALTER TABLE customer_accounts ADD COLUMN active boolean NOT NULL DEFAULT true;
    CREATE TABLE merchant_policies (
      tenant_id text PRIMARY KEY, version bigint NOT NULL CHECK(version>0),
      max_return_days integer NOT NULL CHECK(max_return_days>=0),
      supervisor_threshold_minor bigint NOT NULL CHECK(supervisor_threshold_minor>=0)
    );
    INSERT INTO merchant_policies VALUES ('demo-shop',1,7,10000);
    CREATE TABLE tool_catalog (
      tool_id text PRIMARY KEY, schema_hash text NOT NULL, metadata jsonb NOT NULL,
      embedding vector(1024), updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE business_proposals (
      operation_id uuid PRIMARY KEY, workflow_id uuid NOT NULL REFERENCES agent_runs(id),
      customer_id text NOT NULL REFERENCES customer_accounts(id), tenant_id text NOT NULL,
      order_id text NOT NULL REFERENCES commerce_orders(id), action text NOT NULL
        CHECK(action IN ('create_return','submit_refund','cancel_order')),
      entity_version bigint NOT NULL, policy_version bigint NOT NULL,
      amount_minor bigint NOT NULL CHECK(amount_minor>=0), arg_hash text NOT NULL,
      status text NOT NULL DEFAULT 'PENDING' CHECK(status IN
        ('PENDING','CONFIRMED','APPROVED','REJECTED','INVALIDATED','EXECUTED')),
      revision bigint NOT NULL DEFAULT 1, requires_supervisor boolean NOT NULL,
      confirmed_by text REFERENCES customer_accounts(id), approved_by text REFERENCES customer_accounts(id),
      expires_at timestamptz NOT NULL DEFAULT now()+interval '15 minutes',
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(workflow_id,order_id,action,entity_version,policy_version)
    );
    CREATE TABLE business_operations (
      operation_id uuid PRIMARY KEY REFERENCES business_proposals(operation_id),
      arg_hash text NOT NULL, status text NOT NULL CHECK(status IN ('PREPARED','SUCCEEDED','REJECTED')),
      receipt jsonb, created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz
    );
    CREATE TABLE merchant_outbox (
      event_id uuid PRIMARY KEY, order_id text NOT NULL REFERENCES commerce_orders(id),
      entity_version bigint NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      delivered_at timestamptz, UNIQUE(order_id,entity_version)
    );
    CREATE TABLE merchant_inbox (
      event_id uuid PRIMARY KEY, received_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE environment_versions (
      order_id text PRIMARY KEY REFERENCES commerce_orders(id), entity_version bigint NOT NULL
    );
    CREATE TABLE approval_audit (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      operation_id uuid NOT NULL REFERENCES business_proposals(operation_id), actor_id text NOT NULL,
      decision text NOT NULL, proposal_revision bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    """
    for statement in ddl.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError("Business audit migration requires an explicit data preservation plan")

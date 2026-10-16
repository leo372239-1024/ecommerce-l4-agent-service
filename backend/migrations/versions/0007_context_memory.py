"""Scoped retrieval, durable ingestion, episodic outbox and reviewed workflow memory."""

from alembic import op

revision = "0007_context_memory"
down_revision = "0006_bounded_receipts"
branch_labels = depends_on = None


def upgrade():
    schema = """
    ALTER TABLE customer_accounts ADD COLUMN memory_enabled boolean NOT NULL DEFAULT true;
    CREATE TABLE knowledge_documents (
      id uuid PRIMARY KEY, tenant_id text NOT NULL, owner_id text NOT NULL REFERENCES customer_accounts(id),
      scope text NOT NULL CHECK(scope IN ('private','tenant')), title text NOT NULL,
      kind text NOT NULL CHECK(kind IN ('reference','policy')), version integer NOT NULL DEFAULT 1,
      status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','INDEXING','READY','FAILED','DELETED')),
      validation text NOT NULL DEFAULT 'UNREVIEWED' CHECK(validation IN ('UNREVIEWED','APPROVED')),
      content_hash text NOT NULL, source_text text NOT NULL, source_name text NOT NULL,
      visible_roles text[] NOT NULL DEFAULT ARRAY['customer','support','supervisor'],
      effective_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      reviewed_by text REFERENCES customer_accounts(id), error_code text,
      attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT now(),
      lease_token uuid, lease_until timestamptz, lexical_ready boolean NOT NULL DEFAULT false
    );
    CREATE INDEX knowledge_dispatch ON knowledge_documents(status,next_attempt_at,lease_until);
    CREATE INDEX knowledge_namespace ON knowledge_documents(tenant_id,owner_id,status);
    CREATE TABLE knowledge_chunks (
      id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
      ordinal integer NOT NULL, content text NOT NULL, content_hash text NOT NULL,
      embedding vector(1024) NOT NULL, embedding_model text NOT NULL,
      UNIQUE(document_id,ordinal)
    );
    CREATE TABLE customer_memories (
      id uuid PRIMARY KEY, tenant_id text NOT NULL, customer_id text NOT NULL REFERENCES customer_accounts(id),
      kind text NOT NULL CHECK(kind IN ('preference','episodic')), memory_key text NOT NULL,
      content jsonb NOT NULL, source_run_id uuid REFERENCES agent_runs(id),
      validation text NOT NULL, generator_version text NOT NULL DEFAULT 'context-v1',
      embedding vector(1024), embedding_model text, created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
      deleted_at timestamptz, UNIQUE(tenant_id,customer_id,kind,memory_key)
    );
    CREATE INDEX memory_namespace ON customer_memories(tenant_id,customer_id,kind,expires_at);
    CREATE TABLE memory_outbox (
      workflow_id uuid PRIMARY KEY REFERENCES agent_runs(id), status text NOT NULL DEFAULT 'PENDING',
      attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT now(),
      lease_token uuid, lease_until timestamptz, error_code text
    );
    CREATE TABLE workflow_memories (
      id uuid PRIMARY KEY, tenant_id text NOT NULL, owner_id text NOT NULL REFERENCES customer_accounts(id),
      source_run_id uuid NOT NULL REFERENCES agent_runs(id), template jsonb NOT NULL,
      fingerprint text NOT NULL, tool_versions jsonb NOT NULL, policy_version bigint NOT NULL,
      status text NOT NULL DEFAULT 'CANDIDATE' CHECK(status IN ('CANDIDATE','VALIDATED','APPROVED','DISABLED')),
      regression jsonb, reviewed_by text REFERENCES customer_accounts(id),
      created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL DEFAULT now()+interval '90 days',
      UNIQUE(tenant_id,owner_id,source_run_id)
    );
    CREATE TABLE context_audit (
      id bigserial PRIMARY KEY, tenant_id text NOT NULL, actor_id text NOT NULL,
      resource_id uuid NOT NULL, action text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    """
    for statement in schema.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    for name in (
        "context_audit",
        "workflow_memories",
        "memory_outbox",
        "customer_memories",
        "knowledge_chunks",
        "knowledge_documents",
    ):
        op.execute(f"DROP TABLE {name}")
    op.drop_column("customer_accounts", "memory_enabled")

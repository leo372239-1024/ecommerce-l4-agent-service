"""Explicit operator-provisioned OIDC identities; never trust role claims."""

from alembic import op

revision = "0009_security_identity"
down_revision = "0008_evaluation_isolation"
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE identity_bindings (
      issuer text NOT NULL CHECK(length(issuer)<=2048),
      subject_hash text NOT NULL CHECK(length(subject_hash)=64),
      customer_id text NOT NULL REFERENCES customer_accounts(id),
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(issuer,subject_hash), UNIQUE(issuer,customer_id)
    )""")


def downgrade():
    op.drop_table("identity_bindings")

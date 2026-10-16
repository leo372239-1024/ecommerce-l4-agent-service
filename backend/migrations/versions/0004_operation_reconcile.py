"""Explicit dispatch/uncertain outcome states; immutable revision after applying 0003."""

from alembic import op

revision = "0004_operation_reconcile"
down_revision = "0003_business_gateway"
branch_labels = depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE commerce_order_tenants(order_id text PRIMARY KEY REFERENCES commerce_orders(id) ON DELETE CASCADE,tenant_id text NOT NULL)"
    )
    op.execute(
        "INSERT INTO commerce_order_tenants SELECT o.id,a.tenant_id FROM commerce_orders o JOIN customer_accounts a ON a.id=o.customer_id"
    )
    op.execute("""CREATE FUNCTION assign_order_tenant() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      INSERT INTO commerce_order_tenants VALUES(NEW.id,(SELECT tenant_id FROM customer_accounts WHERE id=NEW.customer_id));
      RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER capture_order_tenant AFTER INSERT ON commerce_orders FOR EACH ROW EXECUTE FUNCTION assign_order_tenant()"
    )
    op.execute("ALTER TABLE business_operations DROP CONSTRAINT business_operations_status_check")
    op.execute(
        "ALTER TABLE business_operations ADD CONSTRAINT business_operations_status_check CHECK(status IN ('PREPARED','DISPATCHED','RECONCILING','SUCCEEDED','REJECTED'))"
    )


def downgrade():
    raise RuntimeError("Operation receipts require a data preservation plan")

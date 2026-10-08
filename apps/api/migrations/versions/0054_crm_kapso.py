"""Kapso onboarding and the client CRM."""
from alembic import op
import sqlalchemy as sa

revision = "0054_crm_kapso"
down_revision = "0053_template_webhooks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clients", sa.Column("kapso_customer_id", sa.String(80), nullable=True))
    op.create_unique_constraint("uq_clients_kapso_customer", "clients", ["kapso_customer_id"])
    for name in ("crm_settings", "billing"):
        op.add_column("clients", sa.Column(name, sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("clients", sa.Column("message_quota_period", sa.String(7), nullable=False, server_default=""))
    op.add_column("clients", sa.Column("message_quota_used", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("whatsapp_cloud_channels", sa.Column("provider", sa.String(20), nullable=False, server_default="meta"))
    for name in ("kapso_setup_link_id", "kapso_webhook_id"):
        op.add_column("whatsapp_cloud_channels", sa.Column(name, sa.String(80), nullable=True))
    op.create_index("uq_kapso_phone", "whatsapp_cloud_channels", ["phone_number_id"], unique=True,
                    postgresql_where=sa.text("provider = 'kapso' AND phone_number_id <> ''"))
    op.add_column("contacts", sa.Column("whatsapp_opt_in_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("contacts", sa.Column("whatsapp_opt_in_source", sa.String(240), nullable=True))
    client = lambda: sa.Column("client_id", sa.Uuid(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    contact = lambda: sa.Column("contact_id", sa.Uuid(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    conversation = lambda: sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    identity = lambda: sa.Column("id", sa.Uuid(), primary_key=True)
    created = lambda: sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)
    op.create_table("opportunities", identity(), client(), contact(), conversation(),
        sa.Column("title", sa.String(240), nullable=False), sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("value_cents", sa.Integer(), nullable=False), sa.Column("notes", sa.Text(), nullable=False),
        created(), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", name="uq_opportunities_conversation"))
    op.create_table("appointments", identity(), client(), contact(), conversation(),
        sa.Column("title", sa.String(240), nullable=False), sa.Column("resource", sa.String(80), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False), sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("reminder_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_status", sa.String(20), nullable=False), sa.Column("reminder_error", sa.Text(), nullable=True),
        sa.Column("reminder_message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True), created())
    op.create_table("client_payments", identity(), client(), sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False), sa.Column("period", sa.String(7), nullable=False),
        sa.Column("reference", sa.String(240), nullable=False),
        sa.Column("recorded_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False), created(),
        sa.UniqueConstraint("client_id", "reference", name="uq_client_payment_reference"))
    op.create_table("kapso_webhook_events", identity(),
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("whatsapp_cloud_channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False), sa.Column("last_error", sa.Text(), nullable=True),
        created(), sa.UniqueConstraint("channel_id", "event_key", name="uq_kapso_event"))
    for table in ("opportunities", "appointments", "client_payments"):
        op.create_index(f"ix_{table}_client_id", table, ["client_id"])
    for column in ("starts_at", "reminder_due_at"):
        op.create_index(f"ix_appointments_{column}", "appointments", [column])
    for column in ("channel_id", "state"):
        op.create_index(f"ix_kapso_webhook_events_{column}", "kapso_webhook_events", [column])


def downgrade():
    for table in ("kapso_webhook_events", "client_payments", "appointments", "opportunities"):
        op.drop_table(table)
    for name in ("whatsapp_opt_in_source", "whatsapp_opt_in_at"):
        op.drop_column("contacts", name)
    op.drop_index("uq_kapso_phone", table_name="whatsapp_cloud_channels")
    for name in ("provider", "kapso_setup_link_id", "kapso_webhook_id"):
        op.drop_column("whatsapp_cloud_channels", name)
    op.drop_constraint("uq_clients_kapso_customer", "clients", type_="unique")
    for name in ("kapso_customer_id", "crm_settings", "billing", "message_quota_period", "message_quota_used"):
        op.drop_column("clients", name)

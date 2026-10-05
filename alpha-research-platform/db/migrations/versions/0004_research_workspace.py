"""Durable research campaigns, scoped catalogue snapshots and provider preferences."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("catalog_scope",
        sa.Column("scope_id", sa.Text, primary_key=True), sa.Column("settings", JSONB, nullable=False),
        sa.Column("datasets", JSONB, nullable=False), sa.Column("fields", JSONB, nullable=False),
        sa.Column("capabilities", JSONB, nullable=False), sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text), sa.Column("synced_at", sa.DateTime(timezone=True)))
    op.create_table("research_campaign",
        sa.Column("campaign_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False), sa.Column("objective", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False), sa.Column("category", sa.Text, nullable=False),
        sa.Column("source_expression", sa.Text), sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("concurrency", sa.Integer, nullable=False), sa.Column("criteria", JSONB, nullable=False),
        sa.Column("provider", sa.Text, nullable=False), sa.Column("model", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False), sa.Column("brief", JSONB), sa.Column("error", sa.Text),
        sa.Column("stop_requested", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("research_candidate",
        sa.Column("candidate_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", UUID(as_uuid=True), sa.ForeignKey("research_campaign.campaign_id"), nullable=False),
        sa.Column("config_id", UUID(as_uuid=True), sa.ForeignKey("alpha_config.config_id")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("simulation_run.run_id")),
        sa.Column("ordinal", sa.Integer, nullable=False), sa.Column("expression", sa.Text, nullable=False),
        sa.Column("template", sa.Text, nullable=False), sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False), sa.Column("status", sa.Text, nullable=False),
        sa.Column("evaluation", JSONB), sa.Column("decision", sa.Text), sa.Column("feedback", sa.Text),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("campaign_id", "ordinal", name="uq_campaign_ordinal"))
    op.create_index("ix_research_candidate_campaign_id", "research_candidate", ["campaign_id"])
    op.create_table("workspace_preference", sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", JSONB, nullable=False))


def downgrade():
    for table in ("workspace_preference", "research_candidate", "research_campaign", "catalog_scope"):
        op.drop_table(table)

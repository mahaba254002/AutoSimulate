"""Account-scoped submitted history with resumable staging and atomic publication."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("submitted_import",
        sa.Column("account_id",sa.Text(),primary_key=True),sa.Column("import_id",UUID(),nullable=False),
        sa.Column("completed_import_id",UUID()),sa.Column("status",sa.Text(),nullable=False),
        sa.Column("downloaded",sa.Integer(),nullable=False),sa.Column("total",sa.Integer()),
        sa.Column("error",sa.Text()),sa.Column("retry_at",sa.DateTime(timezone=True)),
        sa.Column("completed_at",sa.DateTime(timezone=True)),
        sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column("cancel_requested",sa.Boolean(),nullable=False),sa.Column("restart_required",sa.Boolean(),nullable=False))
    op.create_table("submitted_page",
        sa.Column("import_id",UUID(),primary_key=True),sa.Column("offset",sa.Integer(),primary_key=True),
        sa.Column("account_id",sa.Text(),sa.ForeignKey("submitted_import.account_id",ondelete="CASCADE"),nullable=False),
        sa.Column("payload",JSONB(),nullable=False))
    op.create_table("submitted_alpha",
        sa.Column("account_id",sa.Text(),primary_key=True),sa.Column("alpha_id",sa.Text(),primary_key=True),
        sa.Column("import_id",UUID(),nullable=False),sa.Column("name",sa.Text()),sa.Column("alpha_type",sa.Text()),
        sa.Column("status",sa.Text()),sa.Column("expression",sa.Text()),sa.Column("region",sa.Text()),
        sa.Column("universe",sa.Text()),sa.Column("delay",sa.SmallInteger()),sa.Column("date_submitted",sa.DateTime(timezone=True)),
        sa.Column("score",sa.Numeric()),sa.Column("metrics",JSONB(),nullable=False),sa.Column("search_text",sa.Text(),nullable=False),
        sa.Column("payload",JSONB(),nullable=False))
    op.create_index("ix_submitted_snapshot","submitted_alpha",["account_id","import_id","date_submitted"])
    op.create_index("ix_submitted_market","submitted_alpha",["account_id","region","delay"])


def downgrade():
    op.drop_table("submitted_alpha")
    op.drop_table("submitted_page")
    op.drop_table("submitted_import")

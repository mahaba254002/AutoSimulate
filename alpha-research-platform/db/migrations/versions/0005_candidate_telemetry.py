"""Live research progress, diagnostics and local cancellation requests."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("research_candidate", sa.Column("telemetry", JSONB, nullable=False,
                                                   server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column("research_candidate", "telemetry")

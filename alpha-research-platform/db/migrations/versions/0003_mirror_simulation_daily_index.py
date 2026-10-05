"""Ensure the daily index mirrored in the ORM exists (already created by 0001)."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_simrun_config_per_day
                  ON simulation_run (config_id, immutable_date_utc(submitted_at))
                  WHERE status NOT IN ('ERROR','FAIL','CANCELLED')""")


def downgrade():
    # The index belongs to 0001 and must survive downgrading this mirror revision.
    pass

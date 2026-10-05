"""add max_trade/simulation_mode to alpha_config; per-second/minute rate limits

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. alpha_config: fields confirmed present in ace_lib.generate_alpha()
    #    but absent from the BRAIN API doc's documented payload shape.
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE alpha_config
            ADD COLUMN max_trade TEXT,
            ADD COLUMN simulation_mode TEXT CHECK (simulation_mode IN ('FULL', 'QUICK'))
    """)

    # ------------------------------------------------------------------
    # 2. simulation_run: ace_lib's _check_rate_limit reads per-second AND
    #    per-minute headers (x-ratelimit-*-second, x-ratelimit-*-minute),
    #    not just the daily X-Ratelimit-* the BRAIN API doc documents.
    #    Keep the original daily columns (still populated from the doc's
    #    headers when present) and add the finer-grained pair alongside.
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE simulation_run
            ADD COLUMN ratelimit_limit_second INT,
            ADD COLUMN ratelimit_remaining_second INT,
            ADD COLUMN ratelimit_limit_minute INT,
            ADD COLUMN ratelimit_remaining_minute INT
    """)

    # ------------------------------------------------------------------
    # 3. quota_ledger was daily-only. Add a companion table for the
    #    finer-grained per-minute window, since that's what actually
    #    throttles ace_lib's request loop (60s sleep when remaining<=1).
    #    Kept separate from quota_ledger (which stays the daily source
    #    of truth) rather than overloading one table with two grains.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE quota_window_minute (
            window_start           TIMESTAMPTZ PRIMARY KEY,
            limit_per_minute          INT NOT NULL,
            simulations_used             INT NOT NULL DEFAULT 0,
            last_known_remaining            INT,
            last_updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quota_window_minute")
    op.execute("""
        ALTER TABLE simulation_run
            DROP COLUMN IF EXISTS ratelimit_limit_second,
            DROP COLUMN IF EXISTS ratelimit_remaining_second,
            DROP COLUMN IF EXISTS ratelimit_limit_minute,
            DROP COLUMN IF EXISTS ratelimit_remaining_minute
    """)
    op.execute("""
        ALTER TABLE alpha_config
            DROP COLUMN IF EXISTS max_trade,
            DROP COLUMN IF EXISTS simulation_mode
    """)
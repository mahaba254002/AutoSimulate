"""initial schema — alpha knowledge base

Revision ID: 0001
Revises:
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ------------------------------------------------------------------
    # 1. ALPHA CONFIG
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE alpha_config (
            config_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            config_hash         CHAR(64) NOT NULL UNIQUE,

            sim_type            TEXT NOT NULL CHECK (sim_type IN ('REGULAR','SUPER')),
            regular_code        TEXT,
            combo_code          TEXT,
            selection_code      TEXT,

            instrument_type      TEXT NOT NULL,
            region                TEXT NOT NULL,
            universe               TEXT NOT NULL,
            delay                  SMALLINT NOT NULL CHECK (delay IN (0,1)),
            decay                  SMALLINT,
            neutralization         TEXT,
            truncation             NUMERIC(5,4),
            pasteurization         TEXT,
            test_period             TEXT,
            unit_handling            TEXT,
            nan_handling              TEXT,
            selection_handling         TEXT,
            selection_limit              TEXT,
            language                       TEXT NOT NULL DEFAULT 'FASTEXPR',
            visualization                    BOOLEAN DEFAULT FALSE,

            origin                 TEXT NOT NULL CHECK (origin IN
                                    ('manual','llm_hypothesis','gp_mutation',
                                     'gp_crossover','bandit_selected','seed')),
            parent_config_id       UUID REFERENCES alpha_config(config_id),
            generation              INT DEFAULT 0,

            created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_alpha_config_hash ON alpha_config(config_hash)")
    op.execute("CREATE INDEX idx_alpha_config_region ON alpha_config(region, delay, universe)")
    op.execute("CREATE INDEX idx_alpha_config_origin ON alpha_config(origin)")
    op.execute("CREATE INDEX idx_alpha_config_parent ON alpha_config(parent_config_id)")
    op.execute("CREATE INDEX idx_alpha_config_expr_trgm ON alpha_config USING gin (regular_code gin_trgm_ops)")

    # ------------------------------------------------------------------
    # 2. STRUCTURAL FEATURES
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE alpha_structure (
            config_id            UUID PRIMARY KEY REFERENCES alpha_config(config_id) ON DELETE CASCADE,

            operators             TEXT[] NOT NULL,
            operator_count           INT NOT NULL,
            unique_operator_count       INT NOT NULL,

            data_fields                TEXT[] NOT NULL,
            field_count                    INT NOT NULL,
            dataset_ids                       TEXT[] NOT NULL,
            dataset_count                        INT NOT NULL,

            expression_depth                       INT NOT NULL,
            expression_node_count                     INT NOT NULL,
            complexity_score                            NUMERIC,

            ast_json                                      JSONB,
            ast_hash                                        CHAR(64)
        )
    """)
    op.execute("CREATE INDEX idx_structure_operators ON alpha_structure USING gin (operators)")
    op.execute("CREATE INDEX idx_structure_fields ON alpha_structure USING gin (data_fields)")
    op.execute("CREATE INDEX idx_structure_ast_hash ON alpha_structure(ast_hash)")

    # ------------------------------------------------------------------
    # 3. SIMULATION RUN
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE simulation_run (
            run_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            config_id             UUID NOT NULL REFERENCES alpha_config(config_id),

            platform_simulation_id  TEXT,
            parent_simulation_id     TEXT,
            alpha_id                    TEXT,

            status                        TEXT NOT NULL CHECK (status IN
                                          ('WAITING','SIMULATING','CANCELLED',
                                           'COMPLETE','WARNING','ERROR','TIMEOUT','FAIL')),
            error_message                   TEXT,
            error_property                    TEXT,
            error_line                          INT,

            ratelimit_limit                       INT,
            ratelimit_remaining                     INT,
            ratelimit_reset_seconds                   INT,

            submitted_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at                                TIMESTAMPTZ,

            triggered_by                                  TEXT NOT NULL CHECK (triggered_by IN
                                                          ('gp_engine','bandit_policy','manual','scheduled_resim'))
        )
    """)
    op.execute("CREATE INDEX idx_simrun_config ON simulation_run(config_id)")
    op.execute("CREATE INDEX idx_simrun_status ON simulation_run(status)")
    op.execute("CREATE INDEX idx_simrun_alpha_id ON simulation_run(alpha_id)")
    op.execute("CREATE INDEX idx_simrun_submitted ON simulation_run(submitted_at)")
    # submitted_at::date is not IMMUTABLE on its own (depends on session
    # timezone), so Postgres won't allow it directly in an index. Wrapping
    # it through a small IMMUTABLE function pinned to UTC fixes this.
    op.execute("""
        CREATE OR REPLACE FUNCTION immutable_date_utc(ts TIMESTAMPTZ)
        RETURNS DATE AS $$
            SELECT (ts AT TIME ZONE 'UTC')::date
        $$ LANGUAGE SQL IMMUTABLE
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_simrun_config_per_day
            ON simulation_run (config_id, immutable_date_utc(submitted_at))
            WHERE status NOT IN ('ERROR','FAIL','CANCELLED')
    """)

    # ------------------------------------------------------------------
    # 4. PERFORMANCE METRICS
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE alpha_performance (
            run_id                  UUID PRIMARY KEY REFERENCES simulation_run(run_id) ON DELETE CASCADE,
            config_id               UUID NOT NULL REFERENCES alpha_config(config_id),

            sharpe                    NUMERIC(8,4),
            fitness                     NUMERIC(8,4),
            turnover                      NUMERIC(6,4),
            returns_annualized               NUMERIC(8,4),
            drawdown_max                        NUMERIC(6,4),
            margin                                 NUMERIC(10,6),

            sub_universe_sharpe                       NUMERIC(8,4),
            self_correlation                             NUMERIC(6,4),
            production_correlation                          NUMERIC(6,4),

            ladder_metrics                                     JSONB,
            cluster_metrics                                       JSONB,

            is_submittable                                           BOOLEAN,
            computed_at                                                 TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_perf_sharpe ON alpha_performance(sharpe DESC)")
    op.execute("CREATE INDEX idx_perf_fitness ON alpha_performance(fitness DESC)")
    op.execute("CREATE INDEX idx_perf_selfcorr ON alpha_performance(self_correlation)")
    op.execute("CREATE INDEX idx_perf_config ON alpha_performance(config_id)")
    op.execute("CREATE INDEX idx_perf_screen ON alpha_performance(fitness DESC, self_correlation, turnover)")

    # ------------------------------------------------------------------
    # 5. RECORD SETS
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE alpha_recordset (
            recordset_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id                 UUID NOT NULL REFERENCES simulation_run(run_id) ON DELETE CASCADE,
            recordset_name           TEXT NOT NULL,
            object_storage_path         TEXT NOT NULL,
            row_count                      INT,
            fetched_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(run_id, recordset_name)
        )
    """)

    # ------------------------------------------------------------------
    # 6. EMBEDDINGS INDEX
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE alpha_embedding (
            config_id             UUID PRIMARY KEY REFERENCES alpha_config(config_id) ON DELETE CASCADE,
            qdrant_point_id          UUID NOT NULL UNIQUE,
            embedding_model_version     TEXT NOT NULL,
            embedding_dim                 INT NOT NULL,
            structural_cluster_id            INT,
            computed_at                        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_embedding_cluster ON alpha_embedding(structural_cluster_id)")

    # ------------------------------------------------------------------
    # 7. GENETIC PROGRAMMING
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE gp_population (
            population_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_label                 TEXT NOT NULL,
            generation_number            INT NOT NULL,
            config_id                       UUID NOT NULL REFERENCES alpha_config(config_id),
            fitness_rank                       INT,
            selected_for_breeding                 BOOLEAN DEFAULT FALSE,
            mutation_type                            TEXT,
            crossover_partner_config_id                 UUID REFERENCES alpha_config(config_id),
            created_at                                     TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(run_label, generation_number, config_id)
        )
    """)
    op.execute("CREATE INDEX idx_gp_run_gen ON gp_population(run_label, generation_number)")

    # ------------------------------------------------------------------
    # 8. RL / BANDIT
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE rl_decision (
            decision_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            config_id                 UUID NOT NULL REFERENCES alpha_config(config_id),
            run_id                       UUID REFERENCES simulation_run(run_id),

            policy_version                  TEXT NOT NULL,
            state_snapshot                     JSONB NOT NULL,
            action_taken                          JSONB NOT NULL,

            predicted_value                          NUMERIC,
            observed_reward                             NUMERIC,
            reward_components                              JSONB,

            decided_at                                       TIMESTAMPTZ NOT NULL DEFAULT now(),
            rewarded_at                                         TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX idx_rl_config ON rl_decision(config_id)")
    op.execute("CREATE INDEX idx_rl_policy ON rl_decision(policy_version)")

    # ------------------------------------------------------------------
    # 9. LLM HYPOTHESES
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE llm_hypothesis (
            hypothesis_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            prompt_context_ref           TEXT,
            hypothesis_text                 TEXT NOT NULL,
            proposed_operators                 TEXT[],
            proposed_fields                       TEXT[],
            reasoning_summary                        TEXT,
            model_name                                  TEXT NOT NULL,
            model_version                                  TEXT,

            resulting_config_id                               UUID REFERENCES alpha_config(config_id),
            accepted                                             BOOLEAN,
            created_at                                              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_llm_hyp_config ON llm_hypothesis(resulting_config_id)")

    # ------------------------------------------------------------------
    # 10. DATASET / FIELD CATALOG
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE dataset_catalog (
            dataset_id            TEXT PRIMARY KEY,
            dataset_name             TEXT NOT NULL,
            category                    TEXT,
            region                        TEXT,
            delay                          SMALLINT,
            coverage_pct                      NUMERIC(5,2),
            last_synced_at                      TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE TABLE field_catalog (
            field_id                TEXT PRIMARY KEY,
            dataset_id                  TEXT NOT NULL REFERENCES dataset_catalog(dataset_id),
            field_name                     TEXT NOT NULL,
            field_type                        TEXT,
            description                          TEXT,
            alpha_count                             INT,
            user_count                                 INT,
            last_synced_at                                TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX idx_field_dataset ON field_catalog(dataset_id)")

    # ------------------------------------------------------------------
    # 11. DAILY QUOTA LEDGER
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE quota_ledger (
            ledger_date              DATE PRIMARY KEY,
            daily_limit                 INT NOT NULL,
            simulations_used                INT NOT NULL DEFAULT 0,
            last_known_remaining               INT,
            last_updated_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quota_ledger")
    op.execute("DROP TABLE IF EXISTS field_catalog")
    op.execute("DROP TABLE IF EXISTS dataset_catalog")
    op.execute("DROP TABLE IF EXISTS llm_hypothesis")
    op.execute("DROP TABLE IF EXISTS rl_decision")
    op.execute("DROP TABLE IF EXISTS gp_population")
    op.execute("DROP TABLE IF EXISTS alpha_embedding")
    op.execute("DROP TABLE IF EXISTS alpha_recordset")
    op.execute("DROP TABLE IF EXISTS alpha_performance")
    op.execute("DROP TABLE IF EXISTS simulation_run")
    op.execute("DROP FUNCTION IF EXISTS immutable_date_utc(TIMESTAMPTZ)")
    op.execute("DROP TABLE IF EXISTS alpha_structure")
    op.execute("DROP TABLE IF EXISTS alpha_config")
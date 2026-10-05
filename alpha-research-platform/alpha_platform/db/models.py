"""
SQLAlchemy ORM models — exact mirror of db/migrations/versions/0001_initial_schema.py.
This is what application code imports and queries against. If the schema
changes, update BOTH this file and a new Alembic revision; they must stay
in lockstep since env.py uses Base.metadata for autogenerate diffing.
"""
import uuid

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey,
    Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, func, Index, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship, deferred

Base = declarative_base()


class SubmittedImport(Base):
    __tablename__ = "submitted_import"
    account_id = Column(Text, primary_key=True)
    import_id = Column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    completed_import_id = Column(UUID(as_uuid=True))
    status = Column(Text, nullable=False, default="QUEUED")
    downloaded = Column(Integer, nullable=False, default=0)
    total = Column(Integer)
    error = Column(Text)
    retry_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    cancel_requested = Column(Boolean, nullable=False, default=False)
    restart_required = Column(Boolean, nullable=False, default=False)


class SubmittedPage(Base):
    __tablename__ = "submitted_page"
    import_id = Column(UUID(as_uuid=True), primary_key=True)
    offset = Column(Integer, primary_key=True)
    account_id = Column(Text, ForeignKey("submitted_import.account_id", ondelete="CASCADE"), nullable=False)
    payload = Column(JSONB, nullable=False)


class SubmittedAlpha(Base):
    __tablename__ = "submitted_alpha"
    account_id = Column(Text, primary_key=True)
    alpha_id = Column(Text, primary_key=True)
    import_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(Text)
    alpha_type = Column(Text)
    status = Column(Text)
    expression = Column(Text)
    region = Column(Text)
    universe = Column(Text)
    delay = Column(SmallInteger)
    date_submitted = Column(DateTime(timezone=True))
    score = Column(Numeric)
    metrics = Column(JSONB, nullable=False)
    search_text = Column(Text, nullable=False)
    payload = deferred(Column(JSONB, nullable=False))
    __table_args__ = (Index("ix_submitted_snapshot", "account_id", "import_id", "date_submitted"),
                     Index("ix_submitted_market", "account_id", "region", "delay"),)


class AlphaConfig(Base):
    __tablename__ = "alpha_config"

    config_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_hash = Column(String(64), nullable=False, unique=True)

    sim_type = Column(Text, nullable=False)
    regular_code = Column(Text)
    combo_code = Column(Text)
    selection_code = Column(Text)

    instrument_type = Column(Text, nullable=False)
    region = Column(Text, nullable=False)
    universe = Column(Text, nullable=False)
    delay = Column(SmallInteger, nullable=False)
    decay = Column(SmallInteger)
    neutralization = Column(Text)
    truncation = Column(Numeric(5, 4))
    pasteurization = Column(Text)
    test_period = Column(Text)
    unit_handling = Column(Text)
    nan_handling = Column(Text)
    selection_handling = Column(Text)
    selection_limit = Column(Text)
    language = Column(Text, nullable=False, default="FASTEXPR")
    visualization = Column(Boolean, default=False)
    max_trade = Column(Text)
    simulation_mode = Column(Text)

    origin = Column(Text, nullable=False)
    parent_config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id"))
    generation = Column(Integer, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("sim_type IN ('REGULAR','SUPER')", name="ck_alpha_config_sim_type"),
        CheckConstraint("delay IN (0,1)", name="ck_alpha_config_delay"),
        CheckConstraint(
            "origin IN ('manual','llm_hypothesis','gp_mutation','gp_crossover','bandit_selected','seed')",
            name="ck_alpha_config_origin",
        ),
    )

    structure = relationship("AlphaStructure", back_populates="config", uselist=False, cascade="all, delete-orphan")
    simulation_runs = relationship("SimulationRun", back_populates="config")
    embedding = relationship("AlphaEmbedding", back_populates="config", uselist=False, cascade="all, delete-orphan")


class AlphaStructure(Base):
    __tablename__ = "alpha_structure"

    config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id", ondelete="CASCADE"), primary_key=True)

    operators = Column(ARRAY(Text), nullable=False)
    operator_count = Column(Integer, nullable=False)
    unique_operator_count = Column(Integer, nullable=False)

    data_fields = Column(ARRAY(Text), nullable=False)
    field_count = Column(Integer, nullable=False)
    dataset_ids = Column(ARRAY(Text), nullable=False)
    dataset_count = Column(Integer, nullable=False)

    expression_depth = Column(Integer, nullable=False)
    expression_node_count = Column(Integer, nullable=False)
    complexity_score = Column(Numeric)

    ast_json = Column(JSONB)
    ast_hash = Column(String(64))

    config = relationship("AlphaConfig", back_populates="structure")


class SimulationRun(Base):
    __tablename__ = "simulation_run"

    run_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id"), nullable=False)

    platform_simulation_id = Column(Text)
    parent_simulation_id = Column(Text)
    alpha_id = Column(Text)

    status = Column(Text, nullable=False)
    error_message = Column(Text)
    error_property = Column(Text)
    error_line = Column(Integer)

    ratelimit_limit = Column(Integer)
    ratelimit_remaining = Column(Integer)
    ratelimit_reset_seconds = Column(Integer)
    ratelimit_limit_second = Column(Integer)
    ratelimit_remaining_second = Column(Integer)
    ratelimit_limit_minute = Column(Integer)
    ratelimit_remaining_minute = Column(Integer)

    submitted_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True))

    triggered_by = Column(Text, nullable=False)

    __table_args__ = (
        Index("uq_simrun_config_per_day", "config_id", func.immutable_date_utc(submitted_at),
              unique=True, postgresql_where=text("status NOT IN ('ERROR','FAIL','CANCELLED')")),
        CheckConstraint(
            "status IN ('WAITING','SIMULATING','CANCELLED','COMPLETE','WARNING','ERROR','TIMEOUT','FAIL')",
            name="ck_simrun_status",
        ),
        CheckConstraint(
            "triggered_by IN ('gp_engine','bandit_policy','manual','scheduled_resim')",
            name="ck_simrun_triggered_by",
        ),
    )

    config = relationship("AlphaConfig", back_populates="simulation_runs")
    performance = relationship("AlphaPerformance", back_populates="run", uselist=False, cascade="all, delete-orphan")
    recordsets = relationship("AlphaRecordset", back_populates="run", cascade="all, delete-orphan")


class AlphaPerformance(Base):
    __tablename__ = "alpha_performance"

    run_id = Column(UUID(as_uuid=True), ForeignKey("simulation_run.run_id", ondelete="CASCADE"), primary_key=True)
    config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id"), nullable=False)

    sharpe = Column(Numeric(8, 4))
    fitness = Column(Numeric(8, 4))
    turnover = Column(Numeric(6, 4))
    returns_annualized = Column(Numeric(8, 4))
    drawdown_max = Column(Numeric(6, 4))
    margin = Column(Numeric(10, 6))

    sub_universe_sharpe = Column(Numeric(8, 4))
    self_correlation = Column(Numeric(6, 4))
    production_correlation = Column(Numeric(6, 4))

    ladder_metrics = Column(JSONB)
    cluster_metrics = Column(JSONB)

    is_submittable = Column(Boolean)
    computed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    run = relationship("SimulationRun", back_populates="performance")


class AlphaRecordset(Base):
    __tablename__ = "alpha_recordset"

    recordset_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("simulation_run.run_id", ondelete="CASCADE"), nullable=False)
    recordset_name = Column(Text, nullable=False)
    object_storage_path = Column(Text, nullable=False)
    row_count = Column(Integer)
    fetched_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("run_id", "recordset_name", name="uq_recordset_run_name"),)

    run = relationship("SimulationRun", back_populates="recordsets")


class AlphaEmbedding(Base):
    __tablename__ = "alpha_embedding"

    config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id", ondelete="CASCADE"), primary_key=True)
    qdrant_point_id = Column(UUID(as_uuid=True), nullable=False, unique=True)
    embedding_model_version = Column(Text, nullable=False)
    embedding_dim = Column(Integer, nullable=False)
    structural_cluster_id = Column(Integer)
    computed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    config = relationship("AlphaConfig", back_populates="embedding")


class GpPopulation(Base):
    __tablename__ = "gp_population"

    population_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_label = Column(Text, nullable=False)
    generation_number = Column(Integer, nullable=False)
    config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id"), nullable=False)
    fitness_rank = Column(Integer)
    selected_for_breeding = Column(Boolean, default=False)
    mutation_type = Column(Text)
    crossover_partner_config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("run_label", "generation_number", "config_id", name="uq_gp_run_gen_config"),
    )


class RlDecision(Base):
    __tablename__ = "rl_decision"

    decision_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id"), nullable=False)
    run_id = Column(UUID(as_uuid=True), ForeignKey("simulation_run.run_id"))

    policy_version = Column(Text, nullable=False)
    state_snapshot = Column(JSONB, nullable=False)
    action_taken = Column(JSONB, nullable=False)

    predicted_value = Column(Numeric)
    observed_reward = Column(Numeric)
    reward_components = Column(JSONB)

    decided_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    rewarded_at = Column(DateTime(timezone=True))


class LlmHypothesis(Base):
    __tablename__ = "llm_hypothesis"

    hypothesis_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_context_ref = Column(Text)
    hypothesis_text = Column(Text, nullable=False)
    proposed_operators = Column(ARRAY(Text))
    proposed_fields = Column(ARRAY(Text))
    reasoning_summary = Column(Text)
    model_name = Column(Text, nullable=False)
    model_version = Column(Text)

    resulting_config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id"))
    accepted = Column(Boolean)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DatasetCatalog(Base):
    __tablename__ = "dataset_catalog"

    dataset_id = Column(Text, primary_key=True)
    dataset_name = Column(Text, nullable=False)
    category = Column(Text)
    region = Column(Text)
    delay = Column(SmallInteger)
    coverage_pct = Column(Numeric(5, 2))
    last_synced_at = Column(DateTime(timezone=True))

    fields = relationship("FieldCatalog", back_populates="dataset")


class FieldCatalog(Base):
    __tablename__ = "field_catalog"

    field_id = Column(Text, primary_key=True)
    dataset_id = Column(Text, ForeignKey("dataset_catalog.dataset_id"), nullable=False)
    field_name = Column(Text, nullable=False)
    field_type = Column(Text)
    description = Column(Text)
    alpha_count = Column(Integer)
    user_count = Column(Integer)
    last_synced_at = Column(DateTime(timezone=True))

    dataset = relationship("DatasetCatalog", back_populates="fields")


class QuotaLedger(Base):
    __tablename__ = "quota_ledger"

    ledger_date = Column(Date, primary_key=True)
    daily_limit = Column(Integer, nullable=False)
    simulations_used = Column(Integer, nullable=False, default=0)
    last_known_remaining = Column(Integer)
    last_updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class QuotaWindowMinute(Base):
    __tablename__ = "quota_window_minute"

    window_start = Column(DateTime(timezone=True), primary_key=True)
    limit_per_minute = Column(Integer, nullable=False)
    simulations_used = Column(Integer, nullable=False, default=0)
    last_known_remaining = Column(Integer)
    last_updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CatalogScope(Base):
    """One complete account-visible catalogue for a settings combination."""
    __tablename__ = "catalog_scope"
    scope_id = Column(Text, primary_key=True)
    settings = Column(JSONB, nullable=False)
    # Legacy snapshots remain for compatibility; queries no longer load them.
    legacy_datasets = deferred(Column("datasets", JSONB, nullable=False, default=list))
    legacy_fields = deferred(Column("fields", JSONB, nullable=False, default=list))
    _dataset_rows = relationship("ScopedDataset", cascade="all, delete-orphan", lazy="select", order_by="ScopedDataset.dataset_id")
    _field_rows = relationship("ScopedField", cascade="all, delete-orphan", lazy="select", order_by="ScopedField.field_id")
    capabilities = Column(JSONB, nullable=False, default=dict)
    status = Column(Text, nullable=False, default="QUEUED")
    error = Column(Text)
    synced_at = Column(DateTime(timezone=True))

    @property
    def datasets(self):
        return [row.payload for row in self._dataset_rows]

    @datasets.setter
    def datasets(self, values):
        from alpha_platform.research.catalog_store import dataset_record, field_record
        self._dataset_rows = [ScopedDataset(**dataset_record(p)) for p in {v['id']:v for v in values}.values()]
        if '_field_rows' in self.__dict__:
            metadata = {p['id']:p for p in values}
            for row in self._field_rows:
                for key, value in field_record(row.payload, metadata).items():
                    setattr(row, key, value)

    @property
    def fields(self):
        return [row.payload for row in self._field_rows]

    @fields.setter
    def fields(self, values):
        from alpha_platform.research.catalog_store import field_record
        metadata = {p['id']:p for p in self.datasets}
        self._field_rows = [ScopedField(**field_record(p, metadata)) for p in {v['id']:v for v in values}.values()]


class ScopedDataset(Base):
    __tablename__ = "catalog_dataset"
    scope_id = Column(Text, ForeignKey("catalog_scope.scope_id", ondelete="CASCADE"), primary_key=True)
    dataset_id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    category_id = Column(Text)
    category_name = Column(Text)
    instrument_coverage = Column(Numeric(8,4))
    date_coverage = Column(Numeric(8,4))
    search_text = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False)
    __table_args__ = (Index("ix_catalog_dataset_category", "scope_id", "category_id"),)


class ScopedField(Base):
    __tablename__ = "catalog_field"
    scope_id = Column(Text, ForeignKey("catalog_scope.scope_id", ondelete="CASCADE"), primary_key=True)
    field_id = Column(Text, primary_key=True)
    dataset_id = Column(Text)
    dataset_name = Column(Text)
    category_id = Column(Text)
    category_name = Column(Text)
    field_type = Column(Text)
    instrument_coverage = Column(Numeric(8,4))
    date_coverage = Column(Numeric(8,4))
    search_text = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False)
    __table_args__ = (Index("ix_catalog_field_dataset_coverage", "scope_id", "dataset_id", "instrument_coverage", "date_coverage"),
                     Index("ix_catalog_field_category", "scope_id", "category_id"),)


class ResearchCampaign(Base):
    __tablename__ = "research_campaign"
    campaign_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False)
    objective = Column(Text, nullable=False)
    mode = Column(Text, nullable=False)
    category = Column(Text, nullable=False)
    source_expression = Column(Text)
    max_attempts = Column(Integer, nullable=False)
    concurrency = Column(Integer, nullable=False)
    criteria = Column(JSONB, nullable=False)
    provider = Column(Text, nullable=False)
    model = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="DRAFT")
    brief = Column(JSONB)
    error = Column(Text)
    stop_requested = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ResearchCandidate(Base):
    __tablename__ = "research_candidate"
    candidate_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey("research_campaign.campaign_id"), nullable=False, index=True)
    config_id = Column(UUID(as_uuid=True), ForeignKey("alpha_config.config_id"))
    run_id = Column(UUID(as_uuid=True), ForeignKey("simulation_run.run_id"))
    ordinal = Column(Integer, nullable=False)
    expression = Column(Text, nullable=False)
    template = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False)
    status = Column(Text, nullable=False, default="PLANNED")
    evaluation = Column(JSONB)
    telemetry = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    decision = Column(Text)
    feedback = Column(Text)
    decided_at = Column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("campaign_id", "ordinal", name="uq_campaign_ordinal"),)


class WorkspacePreference(Base):
    __tablename__ = "workspace_preference"
    key = Column(Text, primary_key=True)
    value = Column(JSONB, nullable=False)

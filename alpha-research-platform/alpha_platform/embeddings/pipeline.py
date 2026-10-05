"""
Glue: alpha_structure row -> tree_encoder.encode() -> Qdrant upsert ->
alpha_embedding row in Postgres. This is the single entrypoint callers
(ace_lib_adapter.py, backfill scripts) should use rather than calling
tree_encoder/qdrant_client directly, so the Postgres<->Qdrant link
stays consistent.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from alpha_platform.db.models import AlphaEmbedding, AlphaStructure
from alpha_platform.embeddings.qdrant_client import upsert_embedding
from alpha_platform.embeddings.tree_encoder import MODEL_VERSION, EMBEDDING_DIM, encode

logger = logging.getLogger(__name__)


def embed_and_store(db: Session, structure: AlphaStructure) -> AlphaEmbedding | None:
    """
    Encodes one alpha_structure row, writes the vector to Qdrant, and
    creates the corresponding alpha_embedding row in Postgres (added to
    the session, not committed -- caller controls the transaction, same
    pattern as get_or_create_config).

    Returns None if this config_id already has an embedding for the
    current MODEL_VERSION (idempotent -- safe to call repeatedly, e.g.
    from a backfill script).
    """
    existing = (
        db.query(AlphaEmbedding)
        .filter_by(config_id=structure.config_id, embedding_model_version=MODEL_VERSION)
        .one_or_none()
    )
    if existing is not None:
        return None

    vector = encode(
        operators=structure.operators,
        data_fields=structure.data_fields,
        expression_depth=structure.expression_depth,
        expression_node_count=structure.expression_node_count,
        operator_count=structure.operator_count,
        unique_operator_count=structure.unique_operator_count,
        field_count=structure.field_count,
        complexity_score=float(structure.complexity_score or 0),
    )

    point_id = upsert_embedding(structure.config_id, vector)

    embedding = AlphaEmbedding(
        config_id=structure.config_id,
        qdrant_point_id=point_id,
        embedding_model_version=MODEL_VERSION,
        embedding_dim=EMBEDDING_DIM,
    )
    db.add(embedding)
    logger.info("Embedded config_id=%s -> qdrant_point_id=%s", structure.config_id, point_id)
    return embedding
"""
Thin wrapper around the Qdrant client for the alpha_embeddings
collection. Vectors are the structural-hash embeddings from
tree_encoder.py (currently an uncalibrated coarse filter -- see that
module's docstring for what similarity values can and can't be
trusted for).

Qdrant is similarity-search only. It is never the source of truth for
anything -- alpha_embedding.config_id in Postgres is the only link
back, kept intentionally thin so re-embedding (new MODEL_VERSION)
never risks losing data: Qdrant points can be dropped and rebuilt from
Postgres at any time.
"""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from alpha_platform.config.settings import get_settings
from alpha_platform.embeddings.tree_encoder import EMBEDDING_DIM

_COLLECTION_NAME = "alpha_embeddings"


def get_qdrant_client() -> QdrantClient:
    settings = get_settings()
    return QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)


def ensure_collection_exists(client: QdrantClient | None = None) -> None:
    """
    Idempotent -- safe to call on every startup. Creates the collection
    with cosine distance (matches how tree_encoder.py's sub-vectors are
    L2-normalized before combining) if it doesn't already exist.
    """
    client = client or get_qdrant_client()
    existing = [c.name for c in client.get_collections().collections]
    if _COLLECTION_NAME in existing:
        return
    client.create_collection(
        collection_name=_COLLECTION_NAME,
        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
    )


def upsert_embedding(
    config_id: uuid.UUID,
    vector: list[float],
    *,
    payload: dict | None = None,
    client: QdrantClient | None = None,
) -> uuid.UUID:
    """
    Writes one embedding to Qdrant, keyed by a fresh point_id (returned
    to the caller so it can be stored in alpha_embedding.qdrant_point_id
    for the Postgres<->Qdrant link). payload is optional metadata
    (region, delay, universe, etc) for filtered similarity queries
    without a round-trip to Postgres.
    """
    client = client or get_qdrant_client()
    ensure_collection_exists(client)

    point_id = uuid.uuid4()
    client.upsert(
        collection_name=_COLLECTION_NAME,
        points=[
            PointStruct(
                id=str(point_id),
                vector=vector,
                payload={"config_id": str(config_id), **(payload or {})},
            )
        ],
    )
    return point_id


def find_similar(
    vector: list[float],
    *,
    limit: int = 10,
    score_threshold: float | None = None,
    query_filter: dict | None = None,
    client: QdrantClient | None = None,
) -> list[dict]:
    """
    Nearest-neighbor search. Returns a list of {config_id, score,
    payload} dicts, ordered by similarity (highest first).

    score_threshold, if given, drops results below that cosine score --
    per tree_encoder.py's documented limits, don't set this to a
    precise-sounding value like 0.85 and treat it as meaningful; use it
    generously (e.g. 0.3-0.5) as a coarse pre-filter, and treat the
    ranking (not the absolute scores) as the reliable signal.
    """
    client = client or get_qdrant_client()
    ensure_collection_exists(client)

    results = client.search(
        collection_name=_COLLECTION_NAME,
        query_vector=vector,
        limit=limit,
        score_threshold=score_threshold,
        query_filter=query_filter,
    )
    return [
        {"config_id": r.payload.get("config_id"), "score": r.score, "payload": r.payload}
        for r in results
    ]


def delete_embedding(point_id: uuid.UUID, *, client: QdrantClient | None = None) -> None:
    client = client or get_qdrant_client()
    client.delete(collection_name=_COLLECTION_NAME, points_selector=[str(point_id)])
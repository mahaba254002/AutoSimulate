"""
One-off backfill: finds every alpha_structure row without a matching
alpha_embedding (for the current MODEL_VERSION) and embeds it.

Safe to re-run any number of times -- embed_and_store() is idempotent
per (config_id, MODEL_VERSION).

Usage:
    python scripts/backfill_embeddings.py
    python scripts/backfill_embeddings.py --dry-run
"""
import argparse
import logging

from alpha_platform.db.models import AlphaEmbedding, AlphaStructure
from alpha_platform.db.session import SessionLocal
from alpha_platform.embeddings.pipeline import embed_and_store
from alpha_platform.embeddings.tree_encoder import MODEL_VERSION

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def find_structures_missing_embedding(db):
    subq = db.query(AlphaEmbedding.config_id).filter_by(embedding_model_version=MODEL_VERSION)
    return db.query(AlphaStructure).filter(~AlphaStructure.config_id.in_(subq)).all()


def backfill(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        missing = find_structures_missing_embedding(db)
        logger.info("Found %d structure(s) missing embedding (model_version=%s).", len(missing), MODEL_VERSION)

        if not missing:
            return

        for structure in missing:
            if dry_run:
                logger.info("[dry-run] would embed config_id=%s", structure.config_id)
                continue
            embed_and_store(db, structure)

        if not dry_run:
            db.commit()
            logger.info("Committed embeddings for %d config(s).", len(missing))

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
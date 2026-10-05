"""
One-off backfill: finds every REGULAR alpha_config row that has no
matching alpha_structure row (e.g. created before structure extraction
was wired into get_or_create_config) and populates it.

Safe to re-run any number of times -- only processes rows missing a
structure entry, so already-backfilled rows are skipped automatically.

Usage:
    python scripts/backfill_structure.py
    python scripts/backfill_structure.py --dry-run   # report only, no writes
"""
import argparse
import logging

from alpha_platform.db.models import AlphaConfig, AlphaStructure
from alpha_platform.db.session import SessionLocal
from alpha_platform.structure.features import extract_features
from alpha_platform.structure.parser import FastExprSyntaxError

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def find_configs_missing_structure(db):
    """
    REGULAR configs with a non-null regular_code, where no alpha_structure
    row exists yet -- a LEFT JOIN / NOT EXISTS pattern via SQLAlchemy.
    """
    subq = db.query(AlphaStructure.config_id)
    return (
        db.query(AlphaConfig)
        .filter(AlphaConfig.sim_type == "REGULAR")
        .filter(AlphaConfig.regular_code.isnot(None))
        .filter(~AlphaConfig.config_id.in_(subq))
        .all()
    )


def backfill(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        missing = find_configs_missing_structure(db)
        logger.info("Found %d config(s) missing alpha_structure.", len(missing))

        if not missing:
            return

        succeeded = 0
        failed = 0

        for config in missing:
            try:
                features = extract_features(config.regular_code)
            except FastExprSyntaxError as e:
                logger.warning(
                    "SKIP config_id=%s regular_code=%r: parse error: %s",
                    config.config_id, config.regular_code, e,
                )
                failed += 1
                continue

            logger.info(
                "config_id=%s  expr=%r  -> operators=%s fields=%s depth=%d",
                config.config_id, config.regular_code,
                features.operators, features.data_fields, features.expression_depth,
            )

            if dry_run:
                succeeded += 1
                continue

            structure = AlphaStructure(
                config_id=config.config_id,
                operators=features.operators,
                operator_count=features.operator_count,
                unique_operator_count=features.unique_operator_count,
                data_fields=features.data_fields,
                field_count=features.field_count,
                dataset_ids=[],
                dataset_count=0,
                expression_depth=features.expression_depth,
                expression_node_count=features.expression_node_count,
                complexity_score=features.complexity_score,
                ast_json=features.ast_json,
                ast_hash=features.ast_hash,
            )
            db.add(structure)
            succeeded += 1

        if not dry_run:
            db.commit()
            logger.info("Committed %d new alpha_structure row(s). %d skipped due to parse errors.", succeeded, failed)
        else:
            logger.info("[dry-run] Would have written %d row(s). %d would be skipped due to parse errors.", succeeded, failed)

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report what would be backfilled without writing.")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
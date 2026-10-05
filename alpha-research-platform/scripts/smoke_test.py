"""
Smoke test for the ace_lib -> Postgres integration chain.

SAFETY: this script NEVER submits a simulation by default. Running it
plain only exercises auth + generate_alpha + the dedupe/KB-lookup path,
none of which touch /simulations or spend quota.

To actually submit one real simulation (spends 1 quota slot), you must
BOTH pass --confirm on the command line AND type "yes" at the
interactive prompt this script shows you. Either omitted -> no submit.

Usage:
    python scripts/smoke_test.py                # dry run only, safe
    python scripts/smoke_test.py --confirm       # will ask before submitting
"""
from alpha_platform.brain_client.session_cache import get_authenticated_session
import argparse
import sys

from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.brain_client.ace_lib_adapter import (
    get_or_create_config,
    should_skip_simulation,
    generate_and_simulate,
)
from alpha_platform.db.session import SessionLocal
from alpha_platform.dedupe.hashing import hash_alpha


def dry_run(db) -> dict:
    """
    Builds the alpha dict and checks it against the KB, but submits
    nothing. Safe to run any number of times.
    """
    print("Building test alpha expression (regular='close', trivial baseline)...")
    simulate_data = ace.generate_alpha(
        regular="close",
        region="USA",
        universe="TOP3000",
        delay=1,
        decay=15,
        neutralization="SUBINDUSTRY",
        truncation=0.08,
    )
    print("simulate_data:", simulate_data)

    print("\nChecking dedupe/KB lookup (no API call, Postgres only)...")
    config = get_or_create_config(db, simulate_data, origin="manual")
    db.commit()
    print(f"config_id={config.config_id}  config_hash={config.config_hash}")

    skip = should_skip_simulation(db, config)
    print(f"should_skip_simulation (already run today?) -> {skip}")

    return {"simulate_data": simulate_data, "config": config, "would_skip": skip}


def confirmed_submit(db, session):
    """
    Only reached if --confirm was passed AND the interactive prompt
    below is answered "yes". This is the only path in this script that
    calls ace.start_simulation / spends quota.
    """
    print("\n" + "=" * 60)
    print("ABOUT TO SUBMIT A REAL SIMULATION TO BRAIN.")
    print("This will consume 1 simulation from today's quota.")
    print("=" * 60)
    answer = input("Type 'yes' to proceed, anything else to abort: ").strip().lower()
    if answer != "yes":
        print("Aborted. No simulation submitted.")
        return

    print("\nSubmitting...")
    result = generate_and_simulate(
        db, session,
        origin="manual",
        triggered_by="manual",
        regular="close",
        region="USA",
        universe="TOP3000",
        delay=1,
        decay=15,
        neutralization="SUBINDUSTRY",
        truncation=0.08,
        confirmed_hash=hash_alpha(
            ace.generate_alpha(regular="close", region="USA", universe="TOP3000", delay=1,
                               decay=15, neutralization="SUBINDUSTRY", truncation=0.08)
        ),
    )

    if result["skipped"]:
        print("Skipped: this exact config was already simulated today.")
    else:
        run = result["run"]
        print(f"Done. run_id={run.run_id}  status={run.status}  alpha_id={run.alpha_id}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm", action="store_true",
        help="Enable the submit phase (still requires typing 'yes' at the prompt).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        dry_run_result = dry_run(db)

        if not args.confirm:
            print("\n--confirm not passed -- stopping here. No simulation submitted.")
            print("Re-run with --confirm to be given the option to submit one.")
            return

        if dry_run_result["would_skip"]:
            print("\nThis config was already simulated today -- nothing to submit.")
            return

        print("\nAuthenticating with BRAIN (will reuse cached session if valid)...")
        session = get_authenticated_session()
        print("Authenticated.")

        confirmed_submit(db, session)

    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

"""Durable pre-submit reservations. Unknown outcomes keep their quota slot."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text

from alpha_platform.config.settings import get_settings
from alpha_platform.db.models import AlphaConfig, AlphaStructure, QuotaLedger, SimulationRun

# All managed submissions serialize reservations, not network polling.
SUBMIT_LOCK = 736281901
FAILED = ("ERROR", "FAIL", "CANCELLED")


class SubmissionBlocked(ValueError):
    pass


def today_utc():
    return datetime.now(timezone.utc).date()


def known_structure(db, ast_hash):
    return db.query(SimulationRun.run_id).join(
        AlphaStructure, SimulationRun.config_id == AlphaStructure.config_id
    ).filter(AlphaStructure.ast_hash == ast_hash, SimulationRun.status.notin_(FAILED)).first() is not None


def quota_snapshot(db):
    today = today_utc()
    ledger = db.get(QuotaLedger, today)
    recorded = db.query(func.count(SimulationRun.run_id)).filter(
        func.immutable_date_utc(SimulationRun.submitted_at) == today
    ).scalar()
    limit = min(5000, get_settings().daily_simulation_budget)
    used = max(recorded, ledger.simulations_used if ledger else 0)
    remaining = max(0, limit - used)
    if ledger and ledger.last_known_remaining is not None:
        remaining = min(remaining, max(0, ledger.last_known_remaining))
    return {"date": str(today), "limit": limit, "used": used, "remaining": remaining,
            "platform_remaining": ledger.last_known_remaining if ledger else None}


def reserve_submission(db, simulate_data, *, origin, triggered_by, confirmed_hash):
    from alpha_platform.brain_client.ace_lib_adapter import get_or_create_config, should_skip_simulation
    from alpha_platform.dedupe.hashing import hash_alpha
    from alpha_platform.structure.features import extract_features

    if confirmed_hash != hash_alpha(simulate_data):
        raise SubmissionBlocked("Explicit confirmation for this exact simulation is required")
    if simulate_data.get("type") != "REGULAR":
        raise SubmissionBlocked("Only REGULAR expressions are supported by the guarded workflow")
    features = extract_features(simulate_data["regular"])
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SUBMIT_LOCK})
    config = get_or_create_config(db, simulate_data, origin=origin)
    if should_skip_simulation(db, config) or known_structure(db, features.ast_hash):
        db.commit()
        return config, None
    quota = quota_snapshot(db)
    if quota["remaining"] <= 0:
        db.rollback()
        if quota["used"] >= quota["limit"]:
            raise SubmissionBlocked(f"App daily simulation limit reached ({quota['used']} / {quota['limit']}). Saved results are retained.")
        raise SubmissionBlocked("BRAIN reported no remaining simulation quota. Saved results are retained.")
    today = today_utc()
    ledger = db.get(QuotaLedger, today)
    if ledger:
        latest = db.query(SimulationRun).order_by(SimulationRun.submitted_at.desc()).first()
        if latest:
            for remaining, seconds in ((latest.ratelimit_remaining_minute, 60),
                                       (latest.ratelimit_remaining_second, 1)):
                if remaining is not None and remaining <= 1 and datetime.now(timezone.utc) < (
                    ledger.last_updated_at + timedelta(seconds=seconds)
                ):
                    db.rollback()
                    raise SubmissionBlocked("BRAIN's short-window rate limit is low. Wait a minute, then review again.")
    if ledger is None:
        ledger = QuotaLedger(ledger_date=today, daily_limit=quota["limit"], simulations_used=quota["used"])
        db.add(ledger)
    ledger.daily_limit = quota["limit"]
    ledger.simulations_used = quota["used"] + 1
    if ledger.last_known_remaining is not None:
        ledger.last_known_remaining = max(0, ledger.last_known_remaining - 1)
    ledger.last_updated_at = datetime.now(timezone.utc)
    run = SimulationRun(config_id=config.config_id, status="WAITING", triggered_by=triggered_by)
    db.add(run)
    db.commit()  # reservation survives a process crash or ambiguous POST outcome
    return config, run


def capture_limits(db, run, values, status_code, rejection_code=None):
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SUBMIT_LOCK})
    for name, value in values.items():
        setattr(run, name, value)
    ledger = db.get(QuotaLedger, run.submitted_at.astimezone(timezone.utc).date())
    remaining = values.get("ratelimit_remaining")
    if ledger:
        if status_code == 429 and rejection_code == "CONCURRENT_SIMULATION_LIMIT_EXCEEDED":
            # Explicit concurrency rejection is not evidence of daily exhaustion.
            pass
        elif status_code == 429:
            # A 429 has an unknown scope; never retry automatically or use another
            # candidate to get around it. Conservatively close today's local queue.
            ledger.last_known_remaining = 0
        elif remaining is not None:
            ledger.last_known_remaining = min(remaining, ledger.last_known_remaining) if ledger.last_known_remaining is not None else remaining
        ledger.last_updated_at = datetime.now(timezone.utc)
    db.commit()

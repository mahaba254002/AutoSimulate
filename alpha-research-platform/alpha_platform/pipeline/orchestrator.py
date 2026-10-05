"""Local, human-reviewed batches. Creating or reviewing a batch never calls BRAIN."""
from copy import deepcopy
from datetime import datetime, timezone
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from alpha_platform.brain_client.ace_lib_adapter import get_or_create_config, simulate_confirmed
from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.db.models import AlphaConfig, AlphaPerformance, AlphaStructure, GpPopulation, SimulationRun
from alpha_platform.db.session import SessionLocal
from alpha_platform.dedupe.hashing import hash_alpha
from alpha_platform.generation.gp.population import generate_population
from alpha_platform.generation.gp.trees import DEFAULT_FIELDS
from alpha_platform.pipeline.safety import known_structure, quota_snapshot, SubmissionBlocked
from alpha_platform.research.evaluation import finite_number

BATCHES = {}
REVIEWS = {}
JOBS = {}
LOCK = threading.RLock()
WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="confirmed-simulations")


def get_batch(batch_id):
    batch_id = str(uuid.UUID(batch_id))
    with LOCK:
        if batch_id in BATCHES:
            return deepcopy(BATCHES[batch_id])
    # Restore reviewed candidates after server restart from durable population rows.
    with SessionLocal() as db:
        members = db.query(GpPopulation, AlphaConfig, AlphaStructure).join(
            AlphaConfig, AlphaConfig.config_id == GpPopulation.config_id
        ).join(AlphaStructure, AlphaStructure.config_id == AlphaConfig.config_id).filter(
            GpPopulation.run_label == "local-" + batch_id
        ).order_by(GpPopulation.created_at, GpPopulation.population_id).all()
        if not members:
            raise ValueError("Saved population not found")
        rows = []
        for member, config, structure in members:
            payload = ace.generate_alpha(
                regular=config.regular_code, region=config.region, universe=config.universe,
                delay=config.delay, decay=config.decay, neutralization=config.neutralization,
                truncation=float(config.truncation), pasteurization=config.pasteurization,
                test_period=config.test_period, unit_handling=config.unit_handling,
                nan_handling=config.nan_handling, max_trade=config.max_trade,
                visualization=config.visualization, simulation_mode=config.simulation_mode,
            )
            if hash_alpha(payload) != config.config_hash:
                raise ValueError("Saved settings cannot be reproduced exactly. Generate a new population.")
            rows.append({"id": str(config.config_id), "expression": config.regular_code,
                         "kind": member.mutation_type, "ast_hash": structure.ast_hash,
                         "seen": known_structure(db, structure.ast_hash), "payload": payload,
                         "origin": config.origin})
    batch = {"id": batch_id, "label": "local-" + batch_id, "rows": rows,
             "settings": rows[0]["payload"]["settings"], "fields": [], "created_at": time.time()}
    with LOCK:
        if len(BATCHES) >= 100:
            BATCHES.pop(next(iter(BATCHES)))
        BATCHES[batch_id] = batch
    return deepcopy(batch)


def generate_batch(seeds, fields, count, random_seed, settings):
    candidates = generate_population(seeds, fields=fields, count=count, seed=random_seed)
    batch_id = str(uuid.uuid4())
    label = "local-" + batch_id
    rows = []
    with SessionLocal() as db:
        for candidate in candidates:
            payload = ace.generate_alpha(regular=candidate.expression, **settings)
            origin = "seed" if candidate.mutation_type == "seed" else (
                "gp_crossover" if candidate.mutation_type == "crossover" else "gp_mutation")
            parent = get_or_create_config(db, ace.generate_alpha(regular=candidate.parent, **settings), origin="seed")
            partner = get_or_create_config(db, ace.generate_alpha(regular=candidate.partner, **settings), origin="seed") if candidate.partner else None
            config = get_or_create_config(db, payload, origin=origin, known_fields=set(fields))
            if config.config_id != parent.config_id and config.parent_config_id is None:
                config.parent_config_id = parent.config_id
                config.generation = (parent.generation or 0) + 1
            db.add(GpPopulation(run_label=label, generation_number=config.generation or 0,
                                config_id=config.config_id, mutation_type=candidate.mutation_type,
                                crossover_partner_config_id=partner.config_id if partner else None))
            rows.append({"id": str(config.config_id), "expression": candidate.expression,
                         "kind": candidate.mutation_type, "ast_hash": candidate.ast_hash,
                         "seen": known_structure(db, candidate.ast_hash), "payload": payload,
                         "origin": origin})
        db.commit()
    batch = {"id": batch_id, "label": label, "rows": rows, "settings": settings,
             "created_at": time.time(), "fields": fields}
    with LOCK:
        # Batches are persisted in gp_population; memory holds only active review sessions.
        if len(BATCHES) >= 100:
            BATCHES.pop(next(iter(BATCHES)))
        BATCHES[batch_id] = batch
    return batch


def review_batch(batch_id, selected):
    batch = get_batch(batch_id)
    rows = deepcopy([r for r in batch["rows"] if r["id"] in set(selected)])
    if not rows or len(rows) != len(set(selected)):
        raise ValueError("Select valid candidates from this batch")
    eligible, skipped = [], []
    with SessionLocal() as db:
        for row in rows:
            if known_structure(db, row["ast_hash"]):
                skipped.append(row["id"])
            else:
                eligible.append(row)
        quota = quota_snapshot(db)
    if not eligible:
        raise SubmissionBlocked("All selected structures already have a recorded simulation")
    if len(eligible) > quota["remaining"]:
        raise SubmissionBlocked(f"Select at most {quota['remaining']} candidates within today's remaining budget")
    token = secrets.token_urlsafe(32)
    with LOCK:
        now = time.time()
        for key in list(REVIEWS):
            if REVIEWS[key]["expires"] < now:
                del REVIEWS[key]
        if len(REVIEWS) >= 100:
            raise ValueError("Too many open reviews; wait for old reviews to expire")
        REVIEWS[token] = {"rows": eligible, "expires": now + 600, "day": quota["date"]}
    return {"token": token, "count": len(eligible), "phrase": f"RUN {len(eligible)}",
            "rows": eligible, "skipped": skipped, "quota": quota, "expires_in_seconds": 600}


def confirm_review(token, phrase):
    with LOCK:
        review = REVIEWS.get(token)
        if not review or review["expires"] < time.time():
            raise SubmissionBlocked("Review expired; review the batch again")
        if review["day"] != str(datetime.now(timezone.utc).date()):
            raise SubmissionBlocked("UTC date changed; review today's quota again")
        if phrase != f"RUN {len(review['rows'])}":
            raise SubmissionBlocked("Confirmation phrase does not match this reviewed batch")
        if any(j["status"] in {"QUEUED", "RUNNING"} for j in JOBS.values()):
            raise SubmissionBlocked("A batch is already running; wait for completion or stop it")
        del REVIEWS[token]  # one-time approval, consumed before dispatch
        job_id = str(uuid.uuid4())
        if len(JOBS) >= 100:
            JOBS.pop(next(iter(JOBS)))
        JOBS[job_id] = {"id": job_id, "status": "QUEUED", "total": len(review["rows"]),
                        "completed": 0, "results": [], "error": None, "stop": False}
        WORKER.submit(_run_batch, job_id, review["rows"])
        return deepcopy(JOBS[job_id])


def _run_batch(job_id, rows):
    from alpha_platform.brain_client.session_cache import get_cached_session
    with LOCK:
        JOBS[job_id]["status"] = "RUNNING"
    try:
        session = get_cached_session()
        for row in rows:
            with LOCK:
                if JOBS[job_id]["stop"]:
                    JOBS[job_id]["status"] = "STOPPED"
                    return
            with SessionLocal() as db:
                result = simulate_confirmed(db, session, row["payload"], confirmed_hash=hash_alpha(row["payload"]),
                                            origin=row["origin"], triggered_by="gp_engine")
                run = result["run"]
                status = "SKIPPED" if result["skipped"] else run.status
                with LOCK:
                    JOBS[job_id]["completed"] += 1
                    JOBS[job_id]["results"].append({"id": row["id"], "status": status})
                if status not in {"COMPLETE", "SKIPPED"}:
                    raise SubmissionBlocked(run.error_message or "Simulation did not complete; batch stopped")
        with LOCK:
            JOBS[job_id]["status"] = "COMPLETE"
    except Exception as exc:
        with LOCK:
            JOBS[job_id]["status"] = "ERROR"
            # Do not expose database connection strings, credentials or response bodies.
            JOBS[job_id]["error"] = str(exc) if isinstance(exc, (ValueError, SubmissionBlocked)) else (
                "Batch stopped after a connection or processing error. Inspect saved runs before retrying.")


def get_job(job_id):
    with LOCK:
        if job_id not in JOBS:
            raise ValueError("Job not found; saved results remain in History")
        return deepcopy(JOBS[job_id])


def stop_job(job_id):
    with LOCK:
        if job_id not in JOBS:
            raise ValueError("Job not found")
        JOBS[job_id]["stop"] = True
        return deepcopy(JOBS[job_id])


def recent_results(limit=50):
    with SessionLocal() as db:
        rows = db.query(SimulationRun, AlphaConfig, AlphaPerformance).join(
            AlphaConfig, AlphaConfig.config_id == SimulationRun.config_id
        ).outerjoin(AlphaPerformance, AlphaPerformance.run_id == SimulationRun.run_id).order_by(
            SimulationRun.submitted_at.desc()).limit(limit).all()
        return [{"run_id": str(run.run_id), "expression": config.regular_code, "status": run.status,
                 "alpha_id": run.alpha_id, "submitted_at": run.submitted_at.isoformat(),
                 "region": config.region, "universe": config.universe, "delay": config.delay, "error": run.error_message,
                 "settings": {"region":config.region,"universe":config.universe,"delay":config.delay, "neutralization":config.neutralization, "decay":config.decay, "truncation":finite_number(config.truncation)},
                 **{key: finite_number(getattr(perf, key)) if perf else None
                    for key in ("sharpe", "fitness", "turnover", "self_correlation")}}
                for run, config, perf in rows]

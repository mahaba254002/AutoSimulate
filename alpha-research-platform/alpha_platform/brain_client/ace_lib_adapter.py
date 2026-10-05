"""
The real integration point between ace_lib (vendored, unmodified logic)
and the Alpha Knowledge Base (Postgres). Responsibilities, and only
these:

  1. Before submitting: hash the simulate_data dict, check alpha_config
     for an existing row with that hash. If found, skip simulation and
     reuse the existing config_id (and, if already simulated today,
     skip re-submission entirely per the daily-dedup doc). New configs
     also get their alpha_structure row populated in the same
     transaction, via structure/features.py, so the two tables never
     drift out of sync.
  2. After ace_lib returns a result: persist it into
     alpha_config -> simulation_run -> alpha_performance, capturing
     rate-limit headers from the initial POST /simulations response
     along the way.

This module does NOT reimplement session handling, polling, retries,
or threading -- all of that stays inside vendor/ace_lib.py exactly as
the user's working library already does it. This is a thin persistence
layer wrapped around calls the caller would be making anyway.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.dedupe.hashing import hash_alpha
from alpha_platform.db.models import AlphaConfig, AlphaPerformance, AlphaStructure, SimulationRun
from alpha_platform.structure.features import extract_features
from alpha_platform.structure.parser import FastExprSyntaxError

logger = logging.getLogger(__name__)


def get_or_create_config(
    db: Session,
    simulate_data: dict[str, Any],
    *,
    origin: str = "manual",
    known_fields: set[str] | None = None,
) -> AlphaConfig:
    """
    Hash simulate_data and look it up in alpha_config. Returns the
    existing row if found (config already known, whether or not it's
    been simulated today), otherwise creates it AND its alpha_structure
    row in the same transaction.

    Structure extraction runs on simulate_data["regular"] for REGULAR
    alphas. SUPER alphas (combo/selection) are not yet structurally
    parsed -- combo/selection use a different expression sublanguage
    that structure/parser.py doesn't cover yet -- so alpha_structure is
    skipped for those (config row is still created normally).

    known_fields, if given, is passed through to extract_features() for
    field/group-name disambiguation; without it, the parser's built-in
    heuristic is used.

    Does NOT submit anything to BRAIN -- this only manages the KB side
    of dedup. Call should_skip_simulation() separately to decide whether
    today's submission would be wasteful.
    """
    config_hash = hash_alpha(simulate_data)

    existing = db.query(AlphaConfig).filter_by(config_hash=config_hash).one_or_none()
    if existing is not None:
        return existing

    settings = simulate_data.get("settings", {})
    config = AlphaConfig(
        config_hash=config_hash,
        sim_type=simulate_data["type"],
        regular_code=simulate_data.get("regular"),
        combo_code=simulate_data.get("combo"),
        selection_code=simulate_data.get("selection"),
        instrument_type=settings.get("instrumentType"),
        region=settings.get("region"),
        universe=settings.get("universe"),
        delay=settings.get("delay"),
        decay=settings.get("decay"),
        neutralization=settings.get("neutralization"),
        truncation=settings.get("truncation"),
        pasteurization=settings.get("pasteurization"),
        test_period=settings.get("testPeriod"),
        unit_handling=settings.get("unitHandling"),
        nan_handling=settings.get("nanHandling"),
        selection_handling=settings.get("selectionHandling"),
        selection_limit=settings.get("selectionLimit"),
        language=settings.get("language", "FASTEXPR"),
        visualization=settings.get("visualization", False),
        max_trade=settings.get("maxTrade"),
        simulation_mode=settings.get("simulationMode"),
        origin=origin,
    )
    db.add(config)
    db.flush()  # populate config.config_id without committing yet

    regular_code = simulate_data.get("regular")
    if simulate_data.get("type") == "REGULAR" and regular_code:
        try:
            features = extract_features(regular_code, known_fields=known_fields)
            structure = AlphaStructure(
                config_id=config.config_id,
                operators=features.operators,
                operator_count=features.operator_count,
                unique_operator_count=features.unique_operator_count,
                data_fields=features.data_fields,
                field_count=features.field_count,
                dataset_ids=[],  # populated later once field_catalog lookup is wired in
                dataset_count=0,
                expression_depth=features.expression_depth,
                expression_node_count=features.expression_node_count,
                complexity_score=features.complexity_score,
                ast_json=features.ast_json,
                ast_hash=features.ast_hash,
            )
            db.add(structure)
        except FastExprSyntaxError as e:
            # Don't let a parser gap block config creation -- the config
            # row is still valid and simulate-able even if we can't yet
            # parse its structure. Log loudly so gaps get noticed and
            # the grammar can be extended.
            logger.warning(
                "Could not parse regular_code for structure extraction (config_id=%s): %s",
                config.config_id, e,
            )

    return config


def should_skip_simulation(db: Session, config: AlphaConfig) -> bool:
    """
    True if this config already has a non-failed simulation_run
    submitted today (UTC) -- mirrors the DB-level uq_simrun_config_per_day
    constraint, checked here first so callers can skip the ace_lib call
    entirely instead of hitting the constraint and handling an error.
    """
    today = datetime.now(timezone.utc).date()
    existing_today = (
        db.query(SimulationRun)
        .filter(
            SimulationRun.config_id == config.config_id,
            SimulationRun.status.notin_(["ERROR", "FAIL", "CANCELLED"]),
            func.immutable_date_utc(SimulationRun.submitted_at) == today,
        )
        .first()
    )
    return existing_today is not None


def _rate_limit_fields_from_response(response) -> dict:
    """
    Reads BOTH header shapes off a raw requests.Response from
    POST /simulations:
      - daily framing per the BRAIN API doc: X-Ratelimit-Limit/Remaining/Reset
      - per-second/per-minute framing that ace_lib's own _check_rate_limit
        actually reads: x-ratelimit-{limit,remaining}-{second,minute}
    Header names are case-insensitive in requests, so .get() works
    regardless of casing actually sent by the platform.
    """
    def _to_int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    h = response.headers
    return {
        "ratelimit_limit": _to_int(h.get("X-Ratelimit-Limit")),
        "ratelimit_remaining": _to_int(h.get("X-Ratelimit-Remaining")),
        "ratelimit_reset_seconds": _to_int(h.get("X-Ratelimit-Reset")),
        "ratelimit_limit_second": _to_int(h.get("x-ratelimit-limit-second")),
        "ratelimit_remaining_second": _to_int(h.get("x-ratelimit-remaining-second")),
        "ratelimit_limit_minute": _to_int(h.get("x-ratelimit-limit-minute")),
        "ratelimit_remaining_minute": _to_int(h.get("x-ratelimit-remaining-minute")),
    }


def persist_simulation_result(
    db: Session,
    config: AlphaConfig,
    ace_result: dict[str, Any],
    *,
    triggered_by: str = "manual",
    rate_limit_fields: dict | None = None,
    reserved_run: SimulationRun | None = None,
) -> SimulationRun:
    """
    Takes one element of the list returned by ace.simulate_alpha_list_multi
    (or the dict from ace.simulate_single_alpha) and writes it into
    simulation_run + alpha_performance.

    ace_result shape (per ace_lib.get_specified_alpha_stats):
        {
            "alpha_id": str | None,
            "simulate_data": dict,
            "is_stats": pd.DataFrame | None,   # one row, in-sample stats
            "pnl": pd.DataFrame | None,
            "stats": pd.DataFrame | None,      # yearly stats
            "is_tests": pd.DataFrame | None,   # per-test PASS/FAIL/WARNING
            "train": dict | None,
            "test": dict | None,
        }

    If alpha_id is None, the simulation failed or was never submitted --
    still recorded as an ERROR run so the attempt isn't silently lost.

    rate_limit_fields, if provided, comes from
    _rate_limit_fields_from_response() on the initial submit response.
    """
    from alpha_platform.research.evaluation import finite_number, clean_json

    alpha_id = ace_result.get("alpha_id")
    is_stats = ace_result.get("is_stats")
    rate_limit_fields = rate_limit_fields or {}

    if alpha_id is None:
        run = reserved_run or SimulationRun(
            config_id=config.config_id,
            alpha_id=None,
            status="ERROR",
            triggered_by=triggered_by,
            **rate_limit_fields,
        )
        run.status = "ERROR"
        db.add(run)
        db.flush()
        return run

    run = reserved_run or SimulationRun(
        config_id=config.config_id,
        alpha_id=alpha_id,
        status="COMPLETE",
        triggered_by=triggered_by,
        completed_at=datetime.now(timezone.utc),
        **rate_limit_fields,
    )
    run.alpha_id = alpha_id
    run.status = "COMPLETE"
    run.error_message = None
    run.completed_at = datetime.now(timezone.utc)
    db.add(run)
    db.flush()

    if is_stats is not None and not is_stats.empty:
        row = is_stats.iloc[0].to_dict()
        is_tests = ace_result.get("is_tests")
        is_submittable = None
        self_correlation = None
        production_correlation = None

        if is_tests is not None and not is_tests.empty and "result" in is_tests.columns:
            is_submittable = bool((is_tests["result"] == "PASS").all())

        check_name = "test" if is_tests is not None and "test" in is_tests.columns else "name"
        if is_tests is not None and not is_tests.empty and check_name in is_tests.columns and "value" in is_tests.columns:
            self_corr_row = is_tests[is_tests[check_name] == "SELF_CORRELATION"]
            if not self_corr_row.empty:
                self_correlation = self_corr_row.iloc[0]["value"]

            prod_corr_row = is_tests[is_tests[check_name] == "PROD_CORRELATION"]
            if not prod_corr_row.empty:
                production_correlation = prod_corr_row.iloc[0]["value"]

        perf = AlphaPerformance(
            run_id=run.run_id,
            config_id=config.config_id,
            sharpe=finite_number(row.get("sharpe")),
            fitness=finite_number(row.get("fitness")),
            turnover=finite_number(row.get("turnover")),
            returns_annualized=finite_number(row.get("returns")),
            drawdown_max=finite_number(row.get("drawdown")),
            margin=finite_number(row.get("margin")),
            self_correlation=finite_number(self_correlation),
            production_correlation=finite_number(production_correlation),
            is_submittable=is_submittable,
            ladder_metrics={"checks": __import__("json").loads(is_tests.to_json(orient="records"))
                            if is_tests is not None and not is_tests.empty else [],
                            "train": clean_json(ace_result.get("train")), "test": clean_json(ace_result.get("test"))},
        )
        db.add(perf)

    return run


def generate_and_simulate(
    db: Session,
    session: "ace.SingleSession",
    *,
    origin: str = "manual",
    triggered_by: str = "manual",
    force_resubmit: bool = False,
    known_fields: set[str] | None = None,
    check_self_corr: bool = False,
    check_prod_corr: bool = False,
    check_submission: bool = False,
    confirmed_hash: str | None = None,
    **generate_alpha_kwargs,
) -> dict[str, Any]:
    """
    End-to-end: build the alpha dict via ace.generate_alpha(**kwargs),
    dedupe-check it against the KB (creating alpha_config + alpha_structure
    if new), submit via ace.start_simulation + ace.simulation_progress
    (rather than the higher-level simulate_single_alpha) specifically so
    the raw submit Response is available for rate-limit header capture,
    then persist the result.

    check_self_corr / check_prod_corr / check_submission are passed
    through to ace.get_specified_alpha_stats() -- each adds one extra
    API call (not a simulation, so no quota cost) to populate
    alpha_performance.self_correlation / production_correlation /
    is_submittable with real values instead of leaving them NULL.
    Left off by default since they're not needed for a bare submission
    smoke test, but worth turning on for real research runs.

    Returns {"config": AlphaConfig, "run": SimulationRun, "skipped": bool}
    so callers can distinguish a fresh simulation from a dedupe skip.
    """
    simulate_data = ace.generate_alpha(**generate_alpha_kwargs)

    if force_resubmit:
        raise ValueError("Force resubmission is disabled in the guarded workflow")
    return simulate_confirmed(
        db, session, simulate_data, confirmed_hash=confirmed_hash, origin=origin,
        triggered_by=triggered_by, check_self_corr=check_self_corr,
        check_prod_corr=check_prod_corr, check_submission=check_submission,
    )


class MonitoringCancelled(ValueError):
    pass


def response_error(response):
    import re
    try:
        data = response.json()
        detail = data.get("message") or data.get("detail") or data.get("error") or data.get("status")
    except (ValueError, AttributeError):
        detail = None
    text = str(detail or "No error detail returned.")[:600]
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted]", text)


class _BoundedSession:
    """Add request/deadline bounds to ACE without changing the vendor library."""
    def __init__(self, session, on_response=None, cancel_requested=None):
        import time
        self.session = session
        self.deadline = time.monotonic() + 900
        self.auth_response = None
        self.on_response = on_response
        self.cancel_requested = cancel_requested

    def _request(self, method, *args, **kwargs):
        import time
        if self.cancel_requested and self.cancel_requested():
            raise MonitoringCancelled("Local tracking stopped by the user. BRAIN may still be running; the reservation remains held.")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Simulation polling exceeded 15 minutes; inspect the saved run before retrying")
        kwargs.setdefault("timeout", (10, 60))
        response = getattr(self.session, method)(*args, **kwargs)
        if self.on_response and args:
            self.on_response(str(args[0]), response)
        if method == "get" and response.status_code in (401, 403):
            raise ValueError(f"BRAIN session or permission error (HTTP {response.status_code}). Sign in again before checking this saved run.")
        if method == "get" and response.status_code == 429:
            raise TimeoutError("BRAIN rate limit reached during polling; run retained for inspection")
        if method == "get" and response.status_code >= 400:
            raise ValueError(f"BRAIN request failed (HTTP {response.status_code}): {response_error(response)}")
        if method == "get" and args and str(args[0]).startswith(ace.brain_api_url + "/simulations/"):
            try:
                if response.json().get("status") == "ERROR":
                    raise ValueError(f"BRAIN simulation error: {response_error(response)}")
            except AttributeError:
                pass
        retry_after = response.headers.get("Retry-After")
        if method == "get" and retry_after is not None:
            try:
                wait = float(retry_after)
            except (ValueError, TypeError):
                wait = 0
            if wait > self.deadline - time.monotonic():
                raise TimeoutError("Requested retry delay exceeds the polling deadline; no retry attempted")
        return response

    def get(self, *args, **kwargs):
        if self.auth_response is not None and args and args[0] == ace.brain_api_url + "/authentication":
            return self.auth_response
        return self._request("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._request("post", *args, **kwargs)

    def get_relogin_lock(self):
        from contextlib import contextmanager

        @contextmanager
        def guard():
            with self.session.get_relogin_lock():
                # ACE stats call check_session_and_relogin. Refuse interactive
                # relogin in a dashboard worker, and reuse this check in ACE.
                response = self._request("get", ace.brain_api_url + "/authentication")
                expiry = response.json().get("token", {}).get("expiry", 0)
                if response.status_code != 200 or float(expiry) < 2000:
                    raise ValueError("BRAIN session needs renewal. Sign in again in Sync with BRAIN.")
                self.auth_response = response
                try:
                    yield
                finally:
                    self.auth_response = None
        return guard()


def simulate_confirmed(db, session, simulate_data, *, confirmed_hash=None, origin="manual",
                       triggered_by="manual", check_self_corr=False, check_prod_corr=False,
                       check_submission=False, on_run=None, on_response=None, cancel_requested=None):
    """One confirmed POST, with durable reservation and no automatic resubmit."""
    from alpha_platform.pipeline.safety import reserve_submission, capture_limits

    config, run = reserve_submission(db, simulate_data, origin=origin, triggered_by=triggered_by,
                                     confirmed_hash=confirmed_hash)
    if run is None:
        return {"config": config, "run": None, "skipped": True}
    if on_run:
        on_run(run)
    bounded = _BoundedSession(session, on_response, cancel_requested)
    try:
        response = ace.start_simulation(bounded, simulate_data)
        rejection_code = "CONCURRENT_SIMULATION_LIMIT_EXCEEDED" if response.status_code == 429 and "CONCURRENT_SIMULATION_LIMIT_EXCEEDED" in response_error(response) else None
        capture_limits(db, run, _rate_limit_fields_from_response(response), response.status_code, rejection_code=rejection_code)
        if not 200 <= response.status_code < 300:
            run.status = "ERROR"
            run.error_message = f"BRAIN rejected simulation (HTTP {response.status_code}): {response_error(response)} No retry was attempted."
            db.commit()
            return {"config": config, "run": run, "skipped": False}
        run.status = "SIMULATING"
        run.platform_simulation_id = response.headers.get("Location")
        db.commit()
        progress = ace.simulation_progress(bounded, response)
        if not progress["completed"]:
            # ACE does not distinguish polling failure from a rejected expression.
            # Keep an uncertain run blocking duplicate submissions.
            run.status = "TIMEOUT"
            run.error_message = "ACE could not confirm completion. Inspect this run in BRAIN before retrying."
        else:
            run.alpha_id = progress["result"]["id"]
            db.commit()  # preserve alpha ID even if fetching statistics fails
            stats = ace.get_specified_alpha_stats(
                bounded, run.alpha_id, simulate_data, check_self_corr=check_self_corr,
                check_prod_corr=check_prod_corr, check_submission=check_submission,
            )
            persist_simulation_result(db, config, stats, triggered_by=triggered_by, reserved_run=run)
        db.commit()
        return {"config": config, "run": run, "skipped": False}
    except Exception as error:
        db.rollback()
        run.status = "TIMEOUT"
        detail = str(error) if isinstance(error, (ValueError, TimeoutError)) else f"{type(error).__name__} while requesting or processing BRAIN results"
        run.error_message = detail[:700] + " Reservation retained; inspect BRAIN before retrying."
        db.commit()
        logger.exception("Managed simulation interrupted; reservation retained for run %s", run.run_id)
        raise

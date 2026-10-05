"""Paginated, account-scoped BRAIN discovery. No simulation POSTs."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import random
import re
import secrets
from pathlib import Path
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
import requests

from alpha_platform.brain_client.session_cache import _load_session_cookies
from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.db.models import CatalogScope, WorkspacePreference, ScopedDataset, ScopedField
from sqlalchemy import func, select, case, cast, BigInteger
from alpha_platform.db.session import SessionLocal

SYNC_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog-sync")
REQUEST_LOCK = threading.Lock()
SCHEDULE_LOCK = threading.RLock()
NEXT_REQUEST = 0.0
LAST_RATE_HEADERS = {}
RESUME_TIMER = None
RUNNING = False
PROGRESS_LOCK = threading.Lock()
PROGRESS_CACHE = {}
LOGGER = logging.getLogger(__name__)
CHECKPOINTS = Path(__file__).resolve().parents[2] / "data" / "catalog-sync"


def publish_progress(progress, summary):
    """Progress is advisory; the appended page checkpoint is authoritative."""
    with PROGRESS_LOCK:
        PROGRESS_CACHE[str(progress)] = summary
        temporary = progress.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(summary), encoding="utf-8")
            for attempt in range(4):
                try:
                    temporary.replace(progress)
                    return
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(.05 * (2 ** attempt))
        except OSError as exc:
            LOGGER.warning("Catalogue progress update deferred (%s, errno=%s); page checkpoint retained.",
                           type(exc).__name__, exc.errno)


def sync_error_message(exc):
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, requests.Timeout):
        return "BRAIN catalogue request timed out. Saved pages are retained; retry this sync."
    if isinstance(exc, requests.ConnectionError):
        return "Could not connect to BRAIN during catalogue sync. Saved pages are retained; retry this sync."
    if isinstance(exc, OSError):
        return f"Local catalogue file access failed ({type(exc).__name__}, errno={exc.errno}). Saved pages are retained; check file access and retry."
    return f"Catalogue sync failed ({type(exc).__name__}). Saved pages are retained; retry this sync."


def retry_selection(selection, record):
    saved = (record.capabilities or {}).get("selection") if record else None
    if selection and saved and record.status == "ERROR":
        if {k:v for k,v in selection.items() if k != "seed"} == {k:v for k,v in saved.items() if k != "seed"}:
            return saved  # Keep the checkpoint identity for an unchanged failed selection.
    return selection


class CatalogRateLimited(ValueError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__(f"BRAIN request limit reached. The queue will resume after {retry_at.isoformat()}. Downloaded pages are retained.")


def cooldown():
    with SessionLocal() as db:
        record = db.get(WorkspacePreference, "catalog-sync-backoff")
        return datetime.fromisoformat(record.value["retry_at"]) if record else None


def limited(response):
    # Honor a numeric or HTTP-date Retry-After. Without one, use a conservative local backoff.
    now = datetime.now(timezone.utc)
    header = response.headers.get("Retry-After")
    try:
        seconds = float(header)
        retry_at = now + timedelta(seconds=max(5, seconds)) if math.isfinite(seconds) else now + timedelta(minutes=5)
    except (ValueError, TypeError, OverflowError):
        try:
            from email.utils import parsedate_to_datetime
            retry_at = parsedate_to_datetime(header)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            retry_at = max(retry_at, now + timedelta(seconds=5))
        except (ValueError, TypeError, AttributeError):
            retry_at = now + timedelta(minutes=5)
    with SessionLocal() as db:
        existing = db.get(WorkspacePreference, "catalog-sync-backoff")
        if existing:
            retry_at = max(retry_at, datetime.fromisoformat(existing.value["retry_at"]))
            existing.value = {"retry_at": retry_at.isoformat(), "rate_headers": header_pacing(response.headers)[1]}
        else:
            db.add(WorkspacePreference(key="catalog-sync-backoff", value={"retry_at": retry_at.isoformat(),
                                                                      "rate_headers": header_pacing(response.headers)[1]}))
        db.commit()
    return CatalogRateLimited(retry_at)


def header_pacing(headers):
    """Use valid independent minute/second header pairs; retain conservative fallback."""
    values, delay = {}, 1.5
    for period, seconds in (("minute", 60), ("second", 1)):
        parsed = {}
        for kind in ("limit", "remaining"):
            raw = headers.get(f"x-ratelimit-{kind}-{period}")
            try:
                value = int(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if value >= 0:
                parsed[kind] = value
                values[f"{kind}_{period}"] = value
        limit, remaining = parsed.get("limit"), parsed.get("remaining")
        if limit is not None and limit > 0:
            # Leave one request of headroom in the advertised window.
            delay = max(delay, seconds / max(1, limit - 1))
        if remaining is not None and remaining <= (1 if period == "minute" else 0):
            delay = max(delay, seconds)
    return delay, values


def catalog_request(session, method, path, params=None):
    global NEXT_REQUEST, LAST_RATE_HEADERS
    with REQUEST_LOCK:
        until = cooldown()
        if until and until > datetime.now(timezone.utc):
            raise CatalogRateLimited(until)
        time.sleep(max(0, NEXT_REQUEST - time.monotonic()))
        try:
            response = getattr(session, method)(ace.brain_api_url + path, params=params, timeout=(10, 45))
        finally:
            NEXT_REQUEST = time.monotonic() + 1.5
        delay, LAST_RATE_HEADERS = header_pacing(response.headers)
        NEXT_REQUEST = time.monotonic() + delay
        if response.status_code == 429:
            raise limited(response)
        return response


class ResearchSession(requests.Session):
    """Independent cookie jar for every worker; never interactive relogin."""
    def __init__(self):
        super().__init__()
        self._relogin_lock = threading.Lock()
        cookies = _load_session_cookies()
        if not cookies:
            raise ValueError("Sign in using the login form in Sync with BRAIN before syncing or running research.")
        self.cookies.update(cookies)

    def get_relogin_lock(self):
        return self._relogin_lock


def read_json(session, path, params=None, method="get"):
    response = catalog_request(session, method, path, params)
    if response.status_code in (401, 403):
        raise ValueError("BRAIN session or catalogue permissions need attention. Sign in again in Sync with BRAIN.")
    if response.status_code != 200:
        try:
            payload = response.json()
            def clean(value):
                if isinstance(value, dict):
                    return {key: ("[redacted]" if re.search(r"password|token|secret|cookie|credential", key, re.I) else clean(item))
                            for key, item in value.items()}
                if isinstance(value, list):
                    return [clean(item) for item in value[:10]]
                if isinstance(value, str):
                    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted]", value[:600])
                return value
            detail = json.dumps(clean(payload), ensure_ascii=False)[:800]
        except (ValueError, TypeError):
            detail = "No structured error detail returned."
        raise ValueError(f"BRAIN catalogue request failed for {path} (HTTP {response.status_code}). Detail: {detail}")
    return response.json()


def coverage_percent(row, key):
    """BRAIN coverage metadata is a fraction; absent/invalid values stay unknown."""
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value * 100 if math.isfinite(value) and 0 <= value <= 1 else None


def select_fields(rows, selection):
    if not selection:
        return rows
    def qualifies(row):
        if selection.get("dataset_ids") is not None:
            dataset = row.get("dataset")
            dataset_id = dataset.get("id") if isinstance(dataset, dict) else dataset
            if dataset_id not in selection["dataset_ids"]:
                return False
        for key, threshold in (("coverage", selection["min_instrument"]),
                               ("dateCoverage", selection["min_date"])):
            value = coverage_percent(row, key)
            if threshold > 0 and (value is None or value < threshold):
                return False
        return True
    eligible = [row for row in rows if qualifies(row)]
    limit = selection["max_fields"]
    if selection["random_sample"] and len(eligible) > limit:
        # Sort IDs so identical catalogue contents and seed give the same sample.
        return random.Random(selection["seed"]).sample(sorted(eligible, key=lambda r: r["id"]), limit)
    return eligible[:limit]


def fetch_pages(session, path, params, checkpoint=None, selection=None, progress_file=None, progress_context=None):
    rows, offset, expected, seen = [], 0, None, set()
    if checkpoint and checkpoint.exists():
        lines = checkpoint.read_text(encoding="utf-8").splitlines()
        valid = []
        for index, line in enumerate(lines):
            try:
                saved = json.loads(line)
            except json.JSONDecodeError:
                if index != len(lines) - 1:
                    raise ValueError("Catalogue checkpoint is damaged; no partial snapshot was published.")
                break  # A crash can leave the final append incomplete.
            if saved["offset"] != offset or (expected is not None and saved["count"] != expected):
                raise ValueError("Catalogue checkpoint offsets are inconsistent.")
            expected = saved["count"]
            for item in saved["results"]:
                if item["id"] in seen:
                    raise ValueError("Catalogue checkpoint contains repeated identifiers.")
                seen.add(item["id"])
                rows.append(item)
            offset = len(rows)
            valid.append(line)
        if len(valid) != len(lines):
            checkpoint.write_text("\n".join(valid) + ("\n" if valid else ""), encoding="utf-8")
        if expected is not None and offset == expected:
            return select_fields(rows, selection)
    if selection and not selection["random_sample"] and len(select_fields(rows, selection)) >= selection["max_fields"]:
        return select_fields(rows, selection)
    while True:
        page = read_json(session, path, {**params, "limit": 50, "offset": offset})
        results = page.get("results")
        if not isinstance(results, list) or not isinstance(page.get("count"), int):
            raise ValueError("Catalogue response lacks results or a verified total; sync is incomplete.")
        if expected is None:
            expected = page["count"]
        if expected != page["count"]:
            if checkpoint:
                checkpoint.unlink(missing_ok=True)
            raise ValueError("Catalogue changed during sync. Retry for a consistent snapshot.")
        if not results and offset < expected:
            raise ValueError("Catalogue pagination ended early; previous complete snapshot retained.")
        if params.get("dataset.id"):
            for item in results:
                dataset = item.get("dataset") if isinstance(item, dict) else None
                dataset_id = dataset.get("id") if isinstance(dataset, dict) else dataset
                if dataset_id != params["dataset.id"]:
                    raise ValueError("BRAIN returned fields outside the requested dataset. Sync stopped.")
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
                raise ValueError("Catalogue pagination contains invalid or repeated identifiers.")
            seen.add(item["id"])
            rows.append(item)
        if checkpoint:
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            with checkpoint.open("a", encoding="utf-8") as file:
                file.write(json.dumps({"offset": offset, "count": expected, "results": results}) + "\n")
            progress = progress_file or checkpoint.with_suffix(".progress.json")
            summary = {"downloaded": len(rows), "total": expected, **(progress_context or {})}
            if selection:
                summary["selected"] = min(summary.get("target", selection["max_fields"]), summary.get("selected_before", 0) + len(select_fields(rows, selection)))
                summary["target"] = summary.get("target", selection["max_fields"])
            publish_progress(progress, summary)
        offset += len(results)
        if selection and not selection["random_sample"] and len(select_fields(rows, selection)) >= selection["max_fields"]:
            return select_fields(rows, selection)
        if offset >= expected:
            if len(rows) != expected:
                raise ValueError("Catalogue result count does not match its reported total.")
            return select_fields(rows, selection)
        if offset > 1000000:
            raise ValueError("Catalogue exceeds the sync size limit.")


def discover_scopes(session):
    """Use ACE's OPTIONS interpretation with bounded network requests."""
    class BoundedOptions:
        def options(self, url):
            response = catalog_request(session, "options", "/simulations")
            if response.status_code != 200:
                if response.status_code == 401:
                    raise ValueError("BRAIN has not accepted the saved session. Finish verification in the BRAIN account panel before discovering settings.")
                raise ValueError(f"Could not discover BRAIN settings (HTTP {response.status_code}).")
            return response
    frame = ace.get_instrument_type_region_delay(BoundedOptions())
    scopes = []
    for row in frame.to_dict("records"):
        if row["InstrumentType"] != "EQUITY":
            continue
        for universe in row["Universe"]:
            settings = {"instrumentType": row["InstrumentType"], "region": row["Region"],
                        "delay": int(row["Delay"]), "universe": universe}
            scopes.append({"id": scope_key(settings), "settings": settings,
                           "neutralizations": row["Neutralization"]})
    if not scopes:
        raise ValueError("BRAIN did not return supported equity settings.")
    return scopes


def scope_key(settings):
    return "/".join(str(settings[k]) for k in ("instrumentType", "region", "delay", "universe"))


def matching_datasets(rows, name):
    query = (name or "").strip().casefold()
    if not query:
        return rows
    matches = [row for row in rows if query in str(row.get("name", "")).casefold()]
    if not matches:
        raise ValueError(f'No dataset name matches "{name}" in this scope. Previous snapshot retained.')
    return matches


def fetch_scope_fields(session, settings, checkpoint, selection):
    dataset_ids = selection.get("dataset_ids") if selection else None
    if not dataset_ids:
        return fetch_pages(session, "/data-fields", settings, checkpoint, selection)
    fields = []
    for dataset_id in dataset_ids:
        remaining = 1000000 if selection["random_sample"] else selection["max_fields"] - len(fields)
        if remaining <= 0:
            break
        dataset_checkpoint = checkpoint.with_name(checkpoint.stem + "-" + hashlib.sha256(dataset_id.encode()).hexdigest()[:16] + ".jsonl")
        fields.extend(fetch_pages(session, "/data-fields", {**settings, "dataset.id": dataset_id},
                                  dataset_checkpoint, {**selection, "max_fields": remaining},
                                  progress_file=checkpoint.with_suffix(".progress.json"),
                                  progress_context={"dataset": dataset_id, "selected_before": len(fields),
                                                    "target": selection["max_fields"]}))
    return select_fields(fields, selection)


def sync_scope(scope):
    key = hashlib.sha256(("dataset-scoped-v2" + scope["id"] + json.dumps(scope.get("selection"), sort_keys=True)).encode()).hexdigest()
    datasets_checkpoint = CHECKPOINTS / (key + "-datasets.jsonl")
    fields_checkpoint = CHECKPOINTS / (key + "-fields.jsonl")
    with ResearchSession() as session:
        datasets = fetch_pages(session, "/data-sets", scope["settings"], datasets_checkpoint)
        selection = scope.get("selection")
        if selection and selection.get("dataset_name"):
            datasets = matching_datasets(datasets, selection["dataset_name"])
            selection = {**selection, "dataset_ids": [row["id"] for row in datasets]}
        fields = fetch_scope_fields(session, scope["settings"], fields_checkpoint, selection)
        operators = read_json(session, "/operators")
        if not isinstance(operators, list) or not operators:
            raise ValueError("BRAIN did not return the available operators.")
    with SessionLocal() as db:
        record = db.get(CatalogScope, scope["id"])
        record.datasets, record.fields = datasets, fields
        record.capabilities = {"neutralizations": scope["neutralizations"], "operators": operators, "selection": scope.get("selection")}
        record.status, record.error = "COMPLETE", None
        record.synced_at = datetime.now(timezone.utc)
        db.commit()
    paths = [datasets_checkpoint, fields_checkpoint, *CHECKPOINTS.glob(key + "-fields-*.jsonl"),
             datasets_checkpoint.with_suffix(".progress.json"), fields_checkpoint.with_suffix(".progress.json"),
             datasets_checkpoint.with_suffix(".progress.tmp"), fields_checkpoint.with_suffix(".progress.tmp")]
    with PROGRESS_LOCK:
        for checkpoint in paths:
            PROGRESS_CACHE.pop(str(checkpoint), None)
            try:
                checkpoint.unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.warning("Completed catalogue checkpoint cleanup deferred (%s, errno=%s).",
                               type(exc).__name__, exc.errno)


def sync_pending():
    global RUNNING
    with SCHEDULE_LOCK:
        if RUNNING:
            return
        RUNNING = True
    try:
        _sync_pending()
    finally:
        with SCHEDULE_LOCK:
            RUNNING = False


def schedule_resume(retry_at):
    global RESUME_TIMER
    with SCHEDULE_LOCK:
        if RESUME_TIMER and RESUME_TIMER.is_alive():
            return
        def resume():
            global RESUME_TIMER
            with SCHEDULE_LOCK:
                RESUME_TIMER = None
            SYNC_WORKER.submit(sync_pending)
        RESUME_TIMER = threading.Timer(max(1, (retry_at - datetime.now(timezone.utc)).total_seconds()), resume)
        RESUME_TIMER.daemon = True
        RESUME_TIMER.start()


def _sync_pending():
    with SessionLocal() as db:
        scopes = [{"id": r.scope_id, "settings": r.settings,
                   "neutralizations": r.capabilities.get("neutralizations", []),
                   "selection": r.capabilities.get("selection")}
                  for r in db.query(CatalogScope).filter(CatalogScope.status.in_(["QUEUED", "PAUSED"])).order_by(CatalogScope.scope_id).all()]
    for scope in scopes:
        try:
            with SessionLocal() as db:
                db.get(CatalogScope, scope["id"]).status = "SYNCING"
                db.commit()
            sync_scope(scope)
        except CatalogRateLimited as exc:
            with SessionLocal() as db:
                for record in db.query(CatalogScope).filter(CatalogScope.status.in_(["SYNCING", "QUEUED", "PAUSED"])).all():
                    record.status, record.error = "PAUSED", str(exc)
                db.commit()
            schedule_resume(exc.retry_at)
            return  # Never move to another scope to get around the limit.
        except Exception as exc:
            with SessionLocal() as db:
                record = db.get(CatalogScope, scope["id"])
                record.status = "ERROR"
                record.error = sync_error_message(exc)
                db.commit()


def queue_sync(scope_ids=None, selection=None):
    if selection:
        selection = {**selection, "seed": secrets.randbits(32)}
    with ResearchSession() as session:
        available = discover_scopes(session)
    chosen = available if scope_ids is None else [s for s in available if s["id"] in scope_ids]
    if not chosen or (scope_ids is not None and len(chosen) != len(set(scope_ids))):
        raise ValueError("Select settings returned by BRAIN discovery.")
    with SessionLocal() as db:
        # Serialize dispatch across application processes.
        from sqlalchemy import text
        db.execute(text("SELECT pg_advisory_xact_lock(736281902)"))
        if db.query(CatalogScope).filter(CatalogScope.status.in_(["QUEUED", "SYNCING", "PAUSED"])).first():
            raise ValueError("A catalogue sync is already in progress.")
        for scope in chosen:
            record = db.get(CatalogScope, scope["id"])
            chosen_selection = retry_selection(selection, record)
            if record is None:
                record = CatalogScope(scope_id=scope["id"], settings=scope["settings"], datasets=[], fields=[])
                db.add(record)
            record.status, record.error = "QUEUED", None
            record.capabilities = {**(record.capabilities or {}), "neutralizations": scope["neutralizations"], "selection": chosen_selection}
        db.commit()
    SYNC_WORKER.submit(sync_pending)
    return {"queued": len(chosen)}


def list_scopes():
    with SessionLocal() as db:
        if not RUNNING and db.query(CatalogScope).filter_by(status="PAUSED").first():
            schedule_resume(cooldown() or datetime.now(timezone.utc))
        def progress(record):
            key = hashlib.sha256(("dataset-scoped-v2" + record.scope_id + json.dumps(record.capabilities.get("selection"), sort_keys=True)).encode()).hexdigest()
            result = {}
            for kind in ("datasets", "fields"):
                file = CHECKPOINTS / (key + "-" + kind + ".progress.json")
                with PROGRESS_LOCK:
                    if str(file) in PROGRESS_CACHE:
                        result[kind] = PROGRESS_CACHE[str(file)]
                    elif file.exists():
                        try:
                            result[kind] = json.loads(file.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            pass
            return result
        dataset_counts = dict(db.query(ScopedDataset.scope_id,func.count()).group_by(ScopedDataset.scope_id).all())
        field_counts = dict(db.query(ScopedField.scope_id,func.count()).group_by(ScopedField.scope_id).all())
        return [{"id": r.scope_id, "settings": r.settings, "status": r.status, "error": r.error,
                 "datasets": dataset_counts.get(r.scope_id,0), "fields": field_counts.get(r.scope_id,0),
                 "progress": progress(r), "selection": r.capabilities.get("selection"),
                 "neutralizations": r.capabilities.get("neutralizations", []),
                 "synced_at": r.synced_at.isoformat() if r.synced_at else None}
                for r in db.query(CatalogScope).order_by(CatalogScope.scope_id).all()]


def browse(scope_id, kind, search="", offset=0, limit=50, category_id="", dataset_id="",
           min_instrument_coverage=0, min_date_coverage=0, alpha_count_above=None, alpha_count_below=None):
    with SessionLocal() as db:
        scope = db.get(CatalogScope, scope_id)
        if scope is None:
            raise ValueError("Catalogue scope not found. Sync with BRAIN first.")
        model = ScopedDataset if kind == "datasets" else ScopedField
        query = db.query(model).filter(model.scope_id == scope_id)
        if category_id:
            query = query.filter(model.category_id.is_(None) if category_id == "__unknown__" else model.category_id == category_id)
        if dataset_id:
            query = query.filter(ScopedDataset.dataset_id == dataset_id if kind == "datasets" else ScopedField.dataset_id == dataset_id)
        if search.strip():
            query = query.filter(model.search_text.contains(search.strip().lower(),autoescape=True))
        if min_instrument_coverage:
            query = query.filter(model.instrument_coverage >= min_instrument_coverage)
        if min_date_coverage:
            query = query.filter(model.date_coverage >= min_date_coverage)
        if kind == "fields" and (alpha_count_above is not None or alpha_count_below is not None):
            # Missing counts stay unknown; only valid nonnegative integer metadata qualifies.
            raw_count = ScopedField.payload["alphaCount"].astext
            alpha_count = case((raw_count.op("~")(r"^[0-9]{1,18}$"), cast(raw_count, BigInteger)), else_=None)
            if alpha_count_above is not None:
                query = query.filter(alpha_count > alpha_count_above)
            if alpha_count_below is not None:
                query = query.filter(alpha_count < alpha_count_below)
        count = query.count()
        order = model.name if kind == "datasets" else model.field_id
        query = query.order_by(order, model.dataset_id if kind == "datasets" else model.field_id).offset(offset)
        rows = query.limit(min(limit,100)).all() if limit is not None else query.all()
        categories = db.query(ScopedDataset.category_id,ScopedDataset.category_name).filter(ScopedDataset.scope_id == scope_id).distinct().order_by(ScopedDataset.category_name).all()
        selected = db.get(ScopedDataset,(scope_id,dataset_id)) if dataset_id else None
        counts = dict(db.query(ScopedField.dataset_id,func.count()).filter(ScopedField.scope_id == scope_id).group_by(ScopedField.dataset_id).all()) if kind == "datasets" else {}
        results = [{**r.payload, "catalog_dataset_name":getattr(r,"dataset_name",None),
                    "catalog_category":r.category_name, "instrument_coverage_pct":float(r.instrument_coverage) if r.instrument_coverage is not None else None,
                    "date_coverage_pct":float(r.date_coverage) if r.date_coverage is not None else None,
                    **({"synced_field_count":counts.get(r.dataset_id,0)} if kind == "datasets" else {})} for r in rows]
        return {"results":results,"count":count,"status":scope.status,"scope_settings":scope.settings,
                "categories":[{"id":cid or "__unknown__","name":name or "Unclassified"} for cid,name in categories],
                "dataset":selected.payload if selected else None,
                "synced_at":scope.synced_at.isoformat() if scope.synced_at else None}


def export_dataset(scope_id, dataset_id, search="", min_instrument_coverage=0,
                   min_date_coverage=0, alpha_count_above=None, alpha_count_below=None):
    data = browse(scope_id, "fields", search=search, limit=None, dataset_id=dataset_id,
                  min_instrument_coverage=min_instrument_coverage, min_date_coverage=min_date_coverage,
                  alpha_count_above=alpha_count_above, alpha_count_below=alpha_count_below)
    if data["dataset"] is None:
        raise ValueError("Dataset not found in this saved scope.")
    return {"scope_id":scope_id, "settings":data["scope_settings"], "dataset":data["dataset"],
            "synced_at":data["synced_at"], "source":"saved_catalogue_metadata",
            "filters":{"search":search, "min_instrument_coverage":min_instrument_coverage,
                       "min_date_coverage":min_date_coverage, "alpha_count_above":alpha_count_above,
                       "alpha_count_below":alpha_count_below},
            "field_count":data["count"], "fields":data["results"]}

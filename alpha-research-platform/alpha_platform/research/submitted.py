"""Read-only submitted-alpha imports. Staged pages never become partial snapshots."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import math
import threading
import uuid

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert

from alpha_platform.db.models import SubmittedAlpha, SubmittedImport, SubmittedPage, WorkspacePreference
from alpha_platform.db.session import SessionLocal
from alpha_platform.research import catalog

WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="submitted-import")
ACTIVE = ("QUEUED", "RUNNING", "STOPPING")
LOCK = threading.Lock()
TIMERS = {}
PAGE_SIZE = 50


class HistoryChanged(ValueError):
    pass


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def json_safe(value):
    if isinstance(value, dict):
        return {k:json_safe(v) for k,v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def read(session, path, params=None):
    response = catalog.catalog_request(session, "get", path, params)
    if response.status_code in (401, 403):
        raise ValueError("BRAIN authentication or alpha access needs attention. Sign in again in Sync with BRAIN.")
    if response.status_code != 200:
        raise ValueError(f"BRAIN submitted-history request failed (HTTP {response.status_code}). Saved history remains available.")
    try:
        return response.json()
    except ValueError:
        raise ValueError("BRAIN returned an unreadable submitted-history response. Retry later.") from None


def account(session):
    profile = read(session, "/users/self")
    value = profile.get("id") if isinstance(profile, dict) else None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("BRAIN did not provide a verified account identifier; history was not imported.")
    return value


def is_submitted(item):
    return bool(item.get("dateSubmitted")) or item.get("status") in {"ACTIVE", "DECOMMISSIONED", "SUBMITTED"}


def validate_page(page, expected, seen):
    if not isinstance(page, dict) or type(page.get("count")) is not int or page["count"] < 0 or not isinstance(page.get("results"), list):
        raise ValueError("Submitted-history response lacks records or a verified total.")
    if expected is not None and page["count"] != expected:
        raise HistoryChanged("Submitted history changed during download. Restart the full sync for a consistent selection.")
    identifiers = set()
    for item in page["results"]:
        identifier = item.get("id") if isinstance(item, dict) else None
        if not isinstance(identifier, str) or not identifier or len(identifier) > 200:
            raise ValueError("BRAIN returned an invalid alpha identifier; the previous complete history was retained.")
        if identifier in seen or identifier in identifiers:
            raise HistoryChanged("BRAIN repeated an alpha across pages. Restart the full sync; saved history was retained.")
        identifiers.add(identifier)
    if len(seen) + len(identifiers) > page["count"] or (not identifiers and len(seen) < page["count"]):
        raise HistoryChanged("BRAIN ended submitted-history pagination early or returned an inconsistent total. Restart the full sync.")
    return identifiers


def alpha_record(payload, account_id, import_id):
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    regular = payload.get("regular")
    expression = regular.get("code") if isinstance(regular, dict) else regular if isinstance(regular, str) else None
    submitted_at = None
    try:
        submitted_at = datetime.fromisoformat(payload["dateSubmitted"].replace("Z", "+00:00"))
        if submitted_at.tzinfo is None:
            submitted_at = None
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    metrics = {key:payload.get(key) if isinstance(payload.get(key), dict) else {} for key in ("is", "os")}
    return {"account_id":account_id,"alpha_id":payload["id"],"import_id":import_id,
            "name":payload.get("name") if isinstance(payload.get("name"), str) else None,
            "alpha_type":payload.get("type"),"status":payload.get("status"),"expression":expression,
            "region":settings.get("region"),"universe":settings.get("universe"),
            "delay":settings.get("delay") if type(settings.get("delay")) is int and settings["delay"] in (0,1) else None,
            "date_submitted":submitted_at,"score":number(payload.get("score")),"metrics":json_safe(metrics),
            "search_text":" ".join(str(v or "") for v in (payload["id"],payload.get("name"),expression)).lower(),
            "payload":json_safe(payload)}


def selected_account(db):
    selected = db.get(WorkspacePreference, "submitted-account")
    return selected.value.get("id") if selected else None


def status():
    with SessionLocal() as db:
        owner = selected_account(db)
        state = db.get(SubmittedImport, owner) if owner else None
        saved = db.query(func.count(SubmittedAlpha.alpha_id)).filter_by(account_id=owner).scalar() if owner else 0
        if state is None:
            return {"status":"NOT_SYNCED","downloaded":0,"total":None,"saved":0,"progress_pct":None,"error":None}
        return {"status":state.status,"downloaded":state.downloaded,"total":state.total,"saved":saved,
                "progress_pct":round(100 * state.downloaded / state.total,1) if state.total else 100 if state.status == "COMPLETE" else None,
                "error":state.error,"restart_required":state.restart_required,
                "retry_at":state.retry_at.isoformat() if state.retry_at else None,
                "completed_at":state.completed_at.isoformat() if state.completed_at else None,
                "updated_at":state.updated_at.isoformat() if state.updated_at else None}


def queue_sync(restart=False):
    with catalog.ResearchSession() as session:
        owner = account(session)
    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(736281904)"))
        if db.query(SubmittedImport).filter(SubmittedImport.status.in_(ACTIVE)).first():
            raise ValueError("A submitted-history sync is already running.")
        state = db.get(SubmittedImport, owner)
        if state is None:
            state = SubmittedImport(account_id=owner)
            db.add(state)
        if restart or state.status == "COMPLETE" or state.restart_required:
            db.query(SubmittedPage).filter_by(account_id=owner).delete(synchronize_session=False)
            state.import_id, state.downloaded, state.total = uuid.uuid4(), 0, None
        state.status, state.error, state.cancel_requested, state.restart_required = "QUEUED", None, False, False
        state.retry_at, state.updated_at = None, datetime.now(timezone.utc)
        pref = db.get(WorkspacePreference, "submitted-account")
        if pref:
            pref.value = {"id":owner}
        else:
            db.add(WorkspacePreference(key="submitted-account",value={"id":owner}))
        db.commit()
    WORKER.submit(run_sync, owner)
    return status()


def stop():
    with SessionLocal() as db:
        owner = selected_account(db)
        state = db.get(SubmittedImport, owner) if owner else None
        if state and state.status in (*ACTIVE,"PAUSED"):
            state.cancel_requested = True
            state.status = "STOPPED" if state.status == "PAUSED" else "STOPPING"
            db.commit()
    return status()


def schedule_resume(owner, retry_at):
    with LOCK:
        if owner in TIMERS and TIMERS[owner].is_alive():
            return
        def resume():
            with LOCK:
                TIMERS.pop(owner, None)
            with SessionLocal() as db:
                db.execute(text("SELECT pg_advisory_xact_lock(736281904)"))
                state = db.get(SubmittedImport, owner)
                if state is None or state.status != "PAUSED" or state.cancel_requested:
                    return
                if db.query(SubmittedImport).filter(SubmittedImport.status.in_(ACTIVE)).first():
                    return
                state.status = "QUEUED"
                db.commit()
            WORKER.submit(run_sync, owner)
        timer = threading.Timer(max(1,(retry_at-datetime.now(timezone.utc)).total_seconds()),resume)
        timer.daemon = True
        TIMERS[owner] = timer
        timer.start()


def publish(db, state):
    pages = db.query(SubmittedPage).filter_by(import_id=state.import_id).order_by(SubmittedPage.offset).all()
    if sum(len(p.payload) for p in pages) != state.total:
        raise ValueError("Saved submitted-history pages are incomplete; the previous published history was retained.")
    batch = []
    def write(rows):
        statement = insert(SubmittedAlpha).values(rows)
        db.execute(statement.on_conflict_do_update(index_elements=["account_id","alpha_id"],
                   set_={key:getattr(statement.excluded,key) for key in rows[0] if key not in {"account_id","alpha_id"}}))
    for page in pages:
        for payload in page.payload:
            if not is_submitted(payload):
                continue
            batch.append(alpha_record(payload,state.account_id,state.import_id))
            if len(batch) == 200:
                write(batch)
                batch = []
    if batch:
        write(batch)
    state.completed_import_id, state.completed_at = state.import_id, datetime.now(timezone.utc)
    state.status, state.error, state.retry_at = "COMPLETE", None, None
    state.updated_at = state.completed_at
    db.query(SubmittedPage).filter_by(import_id=state.import_id).delete(synchronize_session=False)
    db.commit()  # Records and the complete marker publish together. Old absent records are retained.


def run_sync(owner):
    try:
        with catalog.ResearchSession() as session:
            if account(session) != owner:
                raise ValueError("The BRAIN account changed. Start a new sync for the current account; saved histories remain separate.")
            with SessionLocal() as db:
                state = db.get(SubmittedImport, owner)
                if state is None or state.status != "QUEUED":
                    return
                state.status = "RUNNING"
                db.commit()
                seen = {p["id"] for page in db.query(SubmittedPage).filter_by(import_id=state.import_id) for p in page.payload}
                if len(seen) != state.downloaded:
                    raise HistoryChanged("Saved page counts disagree. Restart the full sync; the previous history was retained.")
            while True:
                with SessionLocal() as db:
                    state = db.get(SubmittedImport, owner)
                    if state.cancel_requested:
                        state.status = "STOPPED"
                        db.commit()
                        return
                    offset, expected = state.downloaded, state.total
                    if expected is not None and offset == expected:
                        publish(db,state)
                        return
                page = read(session,"/users/self/alphas",{"stage":"OS","limit":PAGE_SIZE,"offset":offset,"order":"-dateSubmitted"})
                identifiers = validate_page(page,expected,seen)
                with SessionLocal() as db:
                    state = db.get(SubmittedImport, owner)
                    db.add(SubmittedPage(import_id=state.import_id,offset=offset,account_id=owner,payload=json_safe(page["results"])))
                    state.total, state.downloaded = page["count"], offset+len(identifiers)
                    state.updated_at, state.error, state.retry_at = datetime.now(timezone.utc), None, None
                    db.commit()
                seen.update(identifiers)
    except Exception as exc:
        retry_at = exc.retry_at if isinstance(exc,catalog.CatalogRateLimited) else None
        message = str(exc) if isinstance(exc,ValueError) else f"Submitted-history sync failed ({type(exc).__name__}). Saved pages and published records are retained."
        with SessionLocal() as db:
            state = db.get(SubmittedImport, owner)
            if state:
                state.status, state.error, state.retry_at = "PAUSED" if retry_at else "ERROR", message, retry_at
                state.restart_required = isinstance(exc,HistoryChanged)
                db.commit()
        if retry_at:
            schedule_resume(owner,retry_at)


def browse(search="",region="",delay=None,offset=0,limit=50):
    with SessionLocal() as db:
        owner = selected_account(db)
        state = db.get(SubmittedImport, owner) if owner else None
        query = db.query(SubmittedAlpha).filter_by(account_id=owner) if owner else db.query(SubmittedAlpha).filter(False)
        if search.strip():
            query = query.filter(SubmittedAlpha.search_text.contains(search.strip().lower(),autoescape=True))
        if region:
            query = query.filter_by(region=region)
        if delay is not None:
            query = query.filter_by(delay=delay)
        count = query.count()
        query = query.order_by(SubmittedAlpha.date_submitted.desc().nullslast(),SubmittedAlpha.alpha_id).offset(offset)
        rows = query.limit(limit).all() if limit is not None else query.all()
        results = []
        for row in rows:
            results.append({"id":row.alpha_id,"name":row.name,"type":row.alpha_type,"expression":row.expression,
                "status":row.status,"region":row.region,"universe":row.universe,"delay":row.delay,
                "date_submitted":row.date_submitted.isoformat() if row.date_submitted else None,
                "score":float(row.score) if row.score is not None else None,
                "metrics":{stage:{key:number(values.get(key)) for key in ("sharpe","fitness","turnover")} for stage,values in row.metrics.items()},
                "in_latest_sync":bool(state and row.import_id == state.completed_import_id)})
        regions = [r[0] for r in db.query(SubmittedAlpha.region).filter_by(account_id=owner).filter(SubmittedAlpha.region.isnot(None)).distinct().order_by(SubmittedAlpha.region)] if owner else []
        return {"results":results,"count":count,"regions":regions}


def detail(alpha_id, db=None):
    if db is None:
        with SessionLocal() as session:
            return detail(alpha_id,session)
    owner = selected_account(db)
    row = db.get(SubmittedAlpha,(owner,alpha_id)) if owner else None
    if row is None:
        raise ValueError("Submitted alpha not found in the current saved account history.")
    return row.payload


def export():
    with SessionLocal() as db:
        owner = selected_account(db)
        payloads = [r[0] for r in db.query(SubmittedAlpha.payload).filter_by(account_id=owner).order_by(SubmittedAlpha.alpha_id)] if owner else []
        return {"source":"saved_submitted_alpha_history","count":len(payloads),"alphas":payloads}


def parent_reference(db, alpha_id, expression, chosen_settings):
    from alpha_platform.structure.parser import parse
    from alpha_platform.structure.serializer import serialize
    payload = detail(alpha_id,db)
    source = payload.get("regular")
    code = source.get("code") if isinstance(source,dict) else source
    if payload.get("type") != "REGULAR" or not isinstance(code,str) or serialize(parse(code)) != expression:
        raise ValueError("Redevelopment must retain the selected submitted alpha's original regular expression.")
    settings = payload.get("settings",{})
    if settings.get("instrumentType") != "EQUITY" or settings.get("language") != "FASTEXPR":
        raise ValueError("This submitted alpha uses instrument or language settings unsupported by redevelopment.")
    for key in ("region","universe","delay","neutralization","decay","truncation"):
        if key not in settings or settings[key] != chosen_settings[key]:
            raise ValueError("Retain the imported parent's market, delay, universe, neutralization, decay and truncation for comparison.")
    for api_key, local_key in {"pasteurization":"pasteurization","testPeriod":"test_period","unitHandling":"unit_handling",
                               "nanHandling":"nan_handling","maxTrade":"max_trade","visualization":"visualization",
                               "simulationMode":"simulation_mode"}.items():
        if api_key in settings:
            chosen_settings[local_key] = settings[api_key]
    metrics = payload.get("is") if isinstance(payload.get("is"),dict) else {}
    return {"run_id":None,"alpha_id":alpha_id,"source":"submitted_history","settings_match":True,
            "metrics":{**{k:number(metrics.get(k)) for k in ("sharpe","fitness","turnover")},
                       "self_correlation":None,"production_correlation":None},
            "note":"Imported in-sample metrics under matching saved settings. Historical sample alignment and parent correlation are unverified; no parent simulation was started."}


def recover_interrupted():
    with SessionLocal() as db:
        for state in db.query(SubmittedImport).filter(SubmittedImport.status.in_((*ACTIVE,"PAUSED"))):
            state.status = "INTERRUPTED"
            state.error = "Server restarted. Resume this sync to reuse saved pages; no alpha was simulated or submitted."
        db.commit()

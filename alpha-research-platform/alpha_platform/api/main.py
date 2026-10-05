"""Loopback-only research dashboard. Every state-changing request needs a local token."""
from pathlib import Path
from datetime import date
import secrets
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from alpha_platform.db.session import SessionLocal
from alpha_platform.generation.gp.population import DEFAULT_SEEDS
from alpha_platform.generation.gp.trees import DEFAULT_FIELDS
from alpha_platform.pipeline import orchestrator
from alpha_platform.pipeline.safety import quota_snapshot
from alpha_platform.structure.parser import FastExprSyntaxError
from alpha_platform.research import campaigns, catalog, providers, submitted
from alpha_platform.structure.features import extract_features
from alpha_platform.config.operators import load_catalog
from fastapi import Query
from alpha_platform.brain_client import browser_auth
from alpha_platform.brain_client.session_cache import _load_session_cookies
from pydantic import SecretStr
from fastapi.exceptions import RequestValidationError

@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(startup_recovery)
    yield


app = FastAPI(title="Alpha Research", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
LOCAL_TOKEN = secrets.token_urlsafe(32)
ASSETS = Path(__file__).resolve().parents[2] / "dashboard"


@app.middleware("http")
async def local_guard(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not secrets.compare_digest(request.headers.get("x-local-token", ""), LOCAL_TOKEN):
            return JSONResponse({"detail": "Refresh the local dashboard before continuing"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response


@app.exception_handler(ValueError)
@app.exception_handler(FastExprSyntaxError)
async def invalid_request(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(RequestValidationError)
async def input_validation_error(request, exc):
    # Validation responses must not echo credentials from the request body.
    return JSONResponse({"detail": [{k: e[k] for k in ("loc", "msg", "type") if k in e}
                                    for e in exc.errors()]}, status_code=422)


@app.exception_handler(RecursionError)
async def expression_too_deep(request, exc):
    return JSONResponse({"detail": "Expression nesting is too deep. Use a simpler seed."}, status_code=400)


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    return JSONResponse({"detail": "Database unavailable or schema not ready. Run the doctor command in the terminal."}, status_code=503)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SimulationSettings(InputModel):
    region: str = Field(default="USA", pattern=r"^[A-Z0-9_]{2,20}$")
    universe: str = Field(default="TOP3000", pattern=r"^[A-Z0-9_]{2,30}$")
    delay: int = Field(default=1, ge=0, le=1)
    decay: int = Field(default=15, ge=0, le=512)
    neutralization: str = Field(default="SUBINDUSTRY", pattern=r"^[A-Z_]{2,30}$")
    truncation: float = Field(default=0.08, gt=0, le=1)


class GenerateInput(InputModel):
    seeds: list[str] = Field(default=list(DEFAULT_SEEDS), min_length=1, max_length=100)
    fields: list[str] = Field(default=list(DEFAULT_FIELDS), min_length=1, max_length=200)
    count: int = Field(default=20, ge=1, le=100)
    random_seed: int = 42
    settings: SimulationSettings = Field(default_factory=SimulationSettings)


class ReviewInput(InputModel):
    batch_id: str = Field(max_length=64)
    selected: list[str] = Field(min_length=1, max_length=100)


class ConfirmInput(InputModel):
    token: str = Field(max_length=128)
    phrase: str = Field(max_length=32)


@app.get("/")
def index():
    return FileResponse(ASSETS / "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    return {"token": LOCAL_TOKEN, "seeds": DEFAULT_SEEDS, "fields": DEFAULT_FIELDS}


@app.get("/api/status")
def status():
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
        version = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        return {"database": "connected", "schema": version, "quota": quota_snapshot(db),
                "brain_cookie_cache": bool(_load_session_cookies())}


@app.post("/api/generate")
def generate(body: GenerateInput):
    import re
    if any(len(s) > 2000 for s in body.seeds):
        raise ValueError("Seed expressions must be at most 2000 characters")
    if any(not re.fullmatch(r"[A-Za-z_]\w*", f) for f in body.fields):
        raise ValueError("Field names must be identifiers")
    return orchestrator.generate_batch(body.seeds, body.fields, body.count, body.random_seed,
                                       body.settings.model_dump())


@app.post("/api/review")
def review(body: ReviewInput):
    return orchestrator.review_batch(body.batch_id, body.selected)


@app.get("/api/batches/{batch_id}")
def batch(batch_id: str):
    return orchestrator.get_batch(batch_id)


@app.post("/api/confirm")
def confirm(body: ConfirmInput):
    return orchestrator.confirm_review(body.token, body.phrase)


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    return orchestrator.get_job(job_id)


@app.post("/api/jobs/{job_id}/stop")
def stop(job_id: str):
    return orchestrator.stop_job(job_id)


@app.get("/api/results")
def results():
    return orchestrator.recent_results()


class SubmittedSyncInput(InputModel):
    restart: bool = False


@app.get("/api/submitted/status")
def submitted_status():
    return submitted.status()


@app.post("/api/submitted/sync")
def submitted_sync(body: SubmittedSyncInput):
    return submitted.queue_sync(body.restart)


@app.post("/api/submitted/stop")
def submitted_stop():
    return submitted.stop()


@app.get("/api/submitted")
def submitted_list(search: str = Query(default="",max_length=200),
                   region: str = Query(default="",max_length=30),delay: int | None = Query(default=None,ge=0,le=1),
                   offset: int = Query(default=0,ge=0),limit: int = Query(default=50,ge=1,le=100),
                   date_from: date | None = None,date_to: date | None = None):
    return submitted.browse(search,region,delay,offset,limit,date_from,date_to)


@app.get("/api/submitted/export")
def submitted_export(search: str = Query(default="",max_length=200),
                     region: str = Query(default="",max_length=30),delay: int | None = Query(default=None,ge=0,le=1),
                     date_from: date | None = None,date_to: date | None = None):
    return JSONResponse(submitted.export(search,region,delay,date_from,date_to),
                        headers={"Content-Disposition":'attachment; filename="submitted-alphas.json"'})


@app.get("/api/submitted/{alpha_id}")
def submitted_detail(alpha_id: str):
    return submitted.detail(alpha_id)


app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")


class BrainLoginInput(InputModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: SecretStr = Field(min_length=1, max_length=1000)


class BrainVerificationInput(InputModel):
    attempt_id: str = Field(min_length=20, max_length=128)


@app.get("/api/brain/auth")
def brain_auth_state():
    return browser_auth.state()


@app.post("/api/brain/login")
def brain_login(body: BrainLoginInput):
    return browser_auth.login(body.email, body.password.get_secret_value())


@app.post("/api/brain/verify")
def brain_verify(body: BrainVerificationInput):
    return browser_auth.check(body.attempt_id)


@app.post("/api/brain/cancel")
def brain_cancel(body: BrainVerificationInput):
    return browser_auth.cancel(body.attempt_id)


@app.get("/api/research/campaigns")
def campaigns_list():
    return campaigns.list_campaigns()


@app.post("/api/research/campaigns")
def campaigns_create(body: campaigns.CampaignInput):
    return campaigns.create_campaign(body)


@app.get("/api/research/campaigns/{campaign_id}")
def campaigns_detail(campaign_id: str):
    return campaigns.get_campaign(campaign_id)


@app.post("/api/research/campaigns/{campaign_id}/start")
def campaigns_start(campaign_id: str):
    return campaigns.launch(campaign_id)


class ResearchConnection(InputModel):
    provider: str = Field(pattern=r"^(openai|gemini|anthropic|groq)$")
    model: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_./:-]+$")


@app.post("/api/research/campaigns/{campaign_id}/retry")
def campaigns_retry(campaign_id: str, body: ResearchConnection):
    return campaigns.launch(campaign_id, retry=True, connection=body)


@app.post("/api/research/campaigns/{campaign_id}/stop")
def campaigns_stop(campaign_id: str):
    return campaigns.stop(campaign_id)


@app.post("/api/research/campaigns/{campaign_id}/resume")
def campaigns_resume(campaign_id: str):
    return campaigns.resume_campaign(campaign_id)


@app.post("/api/research/candidates/{candidate_id}/cancel")
def candidate_cancel(candidate_id: str):
    return campaigns.cancel_candidate(candidate_id)


class CandidateValidation(InputModel):
    decision: str = Field(pattern=r"^(accepted|rejected)$")
    feedback: str = Field(default="", max_length=3000)


@app.post("/api/research/candidates/{candidate_id}/validate")
def candidate_validate(candidate_id: str, body: CandidateValidation):
    return campaigns.validate_candidate(candidate_id, body.decision, body.feedback)


@app.get("/api/catalog/scopes")
def catalog_scopes():
    return catalog.list_scopes()


@app.get("/api/catalog/discover")
def catalog_discover():
    with catalog.ResearchSession() as session:
        return catalog.discover_scopes(session)


class SyncSelection(InputModel):
    dataset_name: str = Field(default="", max_length=200)
    min_instrument: float = Field(default=80, ge=0, le=100)
    min_date: float = Field(default=80, ge=0, le=100)
    max_fields: int = Field(default=500, ge=1, le=100000)
    random_sample: bool = False


class SyncInput(InputModel):
    scope_ids: list[str] | None = Field(default=None, max_length=500)
    selection: SyncSelection | None = None


@app.post("/api/catalog/sync")
def catalog_sync(body: SyncInput):
    return catalog.queue_sync(body.scope_ids, body.selection.model_dump() if body.selection else None)


@app.get("/api/catalog/browse")
def catalog_browse(scope_id: str, kind: str = Query(default="fields", pattern="^(fields|datasets)$"),
                   search: str = Query(default="", max_length=200), offset: int = Query(default=0, ge=0),
                   limit: int = Query(default=50, ge=1, le=100), category_id: str = Query(default="",max_length=200),
                   dataset_id: str = Query(default="",max_length=200),
                   min_instrument_coverage: float = Query(default=0,ge=0,le=100),
                   min_date_coverage: float = Query(default=0,ge=0,le=100),
                   alpha_count_above: int | None = Query(default=None,ge=0,le=1000000000),
                   alpha_count_below: int | None = Query(default=None,ge=0,le=1000000000)):
    return catalog.browse(scope_id, kind, search, offset, limit, category_id, dataset_id, min_instrument_coverage, min_date_coverage, alpha_count_above, alpha_count_below)


@app.get("/api/catalog/export")
def catalog_export(scope_id: str, dataset_id: str = Query(min_length=1,max_length=200),
                   search: str = Query(default="",max_length=200),
                   min_instrument_coverage: float = Query(default=0,ge=0,le=100),
                   min_date_coverage: float = Query(default=0,ge=0,le=100),
                   alpha_count_above: int | None = Query(default=None,ge=0,le=1000000000),
                   alpha_count_below: int | None = Query(default=None,ge=0,le=1000000000)):
    data = catalog.export_dataset(scope_id, dataset_id, search, min_instrument_coverage,
                                  min_date_coverage, alpha_count_above, alpha_count_below)
    import re
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{scope_id}_{dataset_id}")[:180]+".json"
    return JSONResponse(data, headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.get("/api/providers")
def provider_profiles():
    return providers.profiles()


class ProviderInput(InputModel):
    provider: str = Field(pattern=r"^(openai|gemini|anthropic|groq)$")
    model: str = Field(min_length=1, max_length=100)
    api_key: str | None = Field(default=None, max_length=1000)


@app.post("/api/providers")
def provider_save(body: ProviderInput):
    return providers.save_profile(body.provider, body.model, body.api_key)


@app.get("/api/learning")
def learning():
    return campaigns.learning_summary()


@app.get("/api/operators")
def operators():
    return [{"name": spec.name, "category": spec.category, "signature": spec.signature,
             "description": spec.description} for spec in load_catalog().values()]


class InspectInput(InputModel):
    expression: str = Field(min_length=1, max_length=2000)


@app.post("/api/inspect")
def inspect_expression(body: InspectInput):
    features = extract_features(body.expression)
    return {"operators": features.operators, "fields": features.data_fields,
            "depth": features.expression_depth, "nodes": features.expression_node_count,
            "ast_hash": features.ast_hash, "complexity": float(features.complexity_score)}


def startup_recovery():
    try:
        campaigns.recover_interrupted()
        submitted.recover_interrupted()
    except SQLAlchemyError:
        # The app remains available to show schema/database setup errors.
        pass

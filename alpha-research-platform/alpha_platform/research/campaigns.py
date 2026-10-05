"""Durable bounded campaigns using the existing quota-reserving simulation adapter."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import hashlib
import math
import re
import threading
import uuid

from sqlalchemy import text
from pydantic import BaseModel, ConfigDict, Field, model_validator
from alpha_platform.brain_client.ace_lib_adapter import get_or_create_config, simulate_confirmed, MonitoringCancelled, response_error
from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.db.models import (ResearchCampaign, ResearchCandidate, CatalogScope, RlDecision,
                                      AlphaPerformance, AlphaConfig, AlphaStructure, SimulationRun)
from alpha_platform.db.session import SessionLocal
from alpha_platform.dedupe.hashing import hash_alpha
from alpha_platform.generation.bandit.policy import choose_arm
from alpha_platform.generation.bandit.reward import reward
from alpha_platform.pipeline.safety import SubmissionBlocked
from alpha_platform.research.catalog import ResearchSession
from alpha_platform.research.evaluation import Criteria, evaluate, finite_number
from alpha_platform.research.planning import research_plan, field_dataset
from alpha_platform.research.providers import api_key, PROVIDERS
from alpha_platform.structure.parser import parse, Identifier, FunctionCall, KeywordArg, NumberLiteral, FastExprSyntaxError
from alpha_platform.generation.gp.trees import validate_tree, walk, SCALAR_OPERATORS
from alpha_platform.structure.serializer import serialize

POLICY_VERSION = "research-ucb-v1"
ACTIVE = ("QUEUED", "RESEARCHING", "RUNNING", "STOPPING")
WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research-campaign")
RUN_LOCK = threading.Lock()


class ManualPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_id: str = Field(min_length=1, max_length=200)
    expressions: list[str] = Field(min_length=1, max_length=200)
    neutralization: str = Field(min_length=1, max_length=100)
    decay: int = Field(default=0, ge=0, le=512)
    truncation: float = Field(default=.08, gt=0, le=1)
    variant_count: int = Field(default=0, ge=0, le=5000)
    variant_seed: int = Field(default=42, ge=0, le=2147483647)


class CampaignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=120)
    objective: str = Field(min_length=10, max_length=5000)
    mode: str = Field(default="category", pattern=r"^(category|idea|existing|manual|redevelop)$")
    category: str = Field(default="fundamental", min_length=2, max_length=100)
    source_expression: str | None = Field(default=None, max_length=2000)
    max_attempts: int = Field(default=25, ge=1, le=5000)
    concurrency: int = Field(default=1, ge=1, le=200)
    criteria: Criteria = Field(default_factory=Criteria)
    provider: str = Field(pattern=r"^(openai|gemini|anthropic|groq|manual)$")
    model: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_./:-]+$")

    parent_run_id: uuid.UUID | None = None
    parent_submitted_alpha_id: str | None = Field(default=None,min_length=1,max_length=200)
    redevelopment_windows: list[int] = Field(default_factory=lambda: [40, 20, 60, 126], min_length=2, max_length=8)
    manual_plan: ManualPlanInput | None = None

    @model_validator(mode="after")
    def source_required(self):
        if self.parent_submitted_alpha_id and (self.mode != "redevelop" or self.parent_run_id):
            raise ValueError("Choose one submitted or local parent for redevelopment.")
        if self.mode in {"manual", "redevelop"}:
            if self.provider != "manual" or self.manual_plan is None:
                raise ValueError("Manual research requires expressions and settings, with provider manual.")
            if len(self.manual_plan.expressions) > self.max_attempts:
                raise ValueError("The expression count exceeds the maximum simulation attempts.")
            if self.manual_plan.variant_count:
                if len(self.manual_plan.expressions) != 1:
                    raise ValueError("Field variants require exactly one complete template expression.")
                if self.manual_plan.variant_count > self.max_attempts:
                    raise ValueError("Requested variants exceed the maximum simulation attempts.")
        elif self.manual_plan is not None or self.provider == "manual":
            raise ValueError("Manual expressions belong to manual research mode.")
        if self.mode in {"existing", "redevelop"}:
            if not self.source_expression or not self.source_expression.strip():
                raise ValueError("An existing-alpha campaign requires its original expression.")
            if self.mode == "redevelop":
                self.source_expression = re.sub(r"/\*.*?\*/|//[^\n]*", "", self.source_expression, flags=re.S).strip()
                if len(self.manual_plan.expressions) != 1 or self.manual_plan.variant_count:
                    raise ValueError("Redevelopment requires one parent expression; field substitutions are a separate research mode.")
                self.manual_plan.expressions = [self.source_expression]
                if any(w < 2 or w > 512 for w in self.redevelopment_windows) or len(set(self.redevelopment_windows)) != len(self.redevelopment_windows):
                    raise ValueError("Use distinct redevelopment windows between 2 and 512 days.")
            parse(self.source_expression)
        elif self.source_expression:
            raise ValueError("Source expressions belong to existing-alpha campaigns.")
        return self


def campaign_json(c, candidates=None):
    result = {"id": str(c.campaign_id), "name": c.name, "objective": c.objective,
              "mode": c.mode, "category": c.category, "source_expression": c.source_expression,
              "max_attempts": c.max_attempts, "concurrency": c.concurrency,
              "criteria": c.criteria, "provider": c.provider, "model": c.model,
              "status": c.status, "error": c.error, "brief": c.brief,
              "stop_requested": c.stop_requested, "created_at": c.created_at.isoformat(),
              "updated_at": c.updated_at.isoformat(), "target": 4}
    if candidates is not None:
        result["candidates"] = [{"id": str(r.candidate_id), "expression": r.expression,
                                  "ordinal": r.ordinal, "template": r.template, "rationale": r.rationale,
                                  "status": r.status, "evaluation": r.evaluation, "decision": r.decision,
                                  "telemetry": r.telemetry or {},
                                  "feedback": r.feedback, "run_id": str(r.run_id) if r.run_id else None}
                                 for r in candidates]
        result["attempts"] = sum(r.status not in {"PLANNED", "SKIPPED", "CANCELLED"} or r.run_id is not None for r in candidates)
        result["qualified"] = sum(bool(r.evaluation and r.evaluation.get("qualifies")) for r in candidates)
    return result


def list_campaigns():
    with SessionLocal() as db:
        campaigns = db.query(ResearchCampaign).order_by(ResearchCampaign.created_at.desc()).limit(100).all()
        return [campaign_json(c, db.query(ResearchCandidate).filter_by(campaign_id=c.campaign_id).all()) for c in campaigns]


def get_campaign(campaign_id):
    with SessionLocal() as db:
        c = db.get(ResearchCampaign, uuid.UUID(str(campaign_id)))
        if c is None:
            raise ValueError("Research campaign not found.")
        rows = db.query(ResearchCandidate).filter_by(campaign_id=c.campaign_id).order_by(ResearchCandidate.ordinal).all()
        return campaign_json(c, rows)


def manual_brief(body, scope):
    if scope is None or scope.status != "COMPLETE":
        raise ValueError("Select a synced catalogue scope for manual research.")
    settings = body.manual_plan
    if settings.neutralization not in scope.capabilities.get("neutralizations", []):
        raise ValueError("Select a neutralization available in this catalogue scope.")
    fields = {f["id"]: f for f in scope.fields if str(f.get("type", "")).upper() == "MATRIX" and len(f.get("description") or "") >= 10}
    available_ops = {op.get("name") for op in scope.capabilities.get("operators", [])}
    planned, used, seen = [], set(), set()
    for index, expression in enumerate(settings.expressions, 1):
        if len(expression) > 2000:
            raise ValueError(f"Expression {index} exceeds 2,000 characters.")
        if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=(?!=)", expression):
            raise ValueError(
                f"Expression {index}: variable assignments are not supported in manual research. "
                "Enter a complete expression on each line, without a variable name and '='. "
                "For dependent variables, substitute their expressions into the final expression."
            )
        try:
            tree = parse(expression)
            for _, node in walk(tree):
                if isinstance(node, FunctionCall) and node.operator == "ts_backfill":
                    positional = [a for a in node.args if not isinstance(a, KeywordArg)]
                    if node.args == positional and len(positional) in {2, 3}:
                        node.args = [positional[0], KeywordArg("lookback", positional[1])]
                        if len(positional) == 3:
                            node.args.append(KeywordArg("k", positional[2]))
                    for option in node.args:
                        if isinstance(option, KeywordArg) and option.name in {"lookback", "k"}:
                            value = option.value
                            if not isinstance(value, NumberLiteral) or not math.isfinite(value.value) or not (value.value >= 1 and value.value == int(value.value)):
                                raise ValueError("ts_backfill lookback/k must be a positive integer")
            tree = validate_tree(tree, fields=fields,
                                 operators=SCALAR_OPERATORS | {"group_zscore", "group_neutralize", "ts_backfill", "ts_step"},
                                 groups={"industry", "sector", "subindustry"})
        except (FastExprSyntaxError, ValueError) as error:
            raise ValueError(f"Expression {index}: {error}") from error
        operators = {node.operator for _, node in walk(tree) if isinstance(node, FunctionCall)}
        if not operators.issubset(available_ops):
            raise ValueError(f"Expression {index} uses an operator unavailable in the selected scope.")
        identifiers = {node.name for _, node in walk(tree) if isinstance(node, Identifier) and node.name in fields}
        if not identifiers:
            raise ValueError(f"Expression {index} must use a documented field from the selected scope.")
        canonical = serialize(tree)
        if canonical in seen:
            raise ValueError(f"Expression {index} duplicates another expression.")
        seen.add(canonical)
        used.update(identifiers)
        planned.append({"expression": canonical, "template": f"Manual expression {index}", "rationale": body.objective})
    variant_analysis = None
    if settings.variant_count:
        from alpha_platform.research.variants import field_variants
        planned, variant_analysis = field_variants(planned[0]["expression"], fields, scope.datasets,
                                                  settings.variant_count, settings.variant_seed)
        used.update(name for row in planned for name in row["substitutions"].values())
    templates = [{"name":row["template"],"expression":row["expression"],"hypothesis":body.objective,"slots":[],"windows":[]} for row in planned]
    if variant_analysis:
        templates = [{"name":"Field substitution template", "expression":variant_analysis["source_expression"],
                      "hypothesis":body.objective, "windows":[],
                      "slots":[{"name":slot["original_field"], "role":slot["description"],
                                "original_field":slot["original_field"], "choices":slot["choices"]}
                               for slot in variant_analysis["slots"]]}]
    return {"hypothesis":body.objective,"expected_behaviour":"User-defined hypothesis; simulation results will test it.",
            "selection_rationale":"User selected documented fields and expressions from this synced catalogue.",
            "settings_rationale":"User selected the saved scope and available neutralization.",
            "scope_id":scope.scope_id,"catalogue_synced_at":scope.synced_at.isoformat() if scope.synced_at else None,
            "settings":{**{k:scope.settings[k] for k in ("region","universe","delay")},
                        "neutralization":settings.neutralization,"decay":settings.decay,"truncation":settings.truncation},
            "templates":templates,
            "manual_expressions":planned,"field_evidence":[fields[name] for name in sorted(used)],
            "variant_analysis":variant_analysis,
            "fields_considered":len(used),"available_documented_fields":len(fields),
            "risks":["A valid expression and documented fields do not establish predictive performance.",
                     "Category and description similarity do not establish economic equivalence; review substitutions before starting."],
            "unknowns":["Raw observations and publication timing have not been inspected."],
            "evidence_level":"catalogue_metadata","provider":"manual","model":"manual","usage":[],"version":1}


def create_campaign(body):
    with SessionLocal() as db:
        values = body.model_dump()
        values.pop("manual_plan", None)
        values.pop("redevelopment_windows", None)
        values.pop("parent_run_id", None)
        values.pop("parent_submitted_alpha_id", None)
        if body.mode in {"manual", "redevelop"}:
            scope = db.query(CatalogScope).filter_by(scope_id=body.manual_plan.scope_id).with_for_update().one_or_none()
            if body.manual_plan.variant_count and scope is not None and scope.status == "COMPLETE":
                from alpha_platform.research.discovery import resolve_template_scope
                from alpha_platform.research.catalog import CatalogRateLimited
                try:
                    discovered, provenance = resolve_template_scope(scope, body.manual_plan.expressions[0])
                except CatalogRateLimited as error:
                    raise ValueError(f"Template discovery paused by BRAIN rate limits. Retry saving after {error.retry_at.isoformat()}. "
                                     "Incomplete pages are retained; no simulations were started.") from error
                brief = manual_brief(body, discovered)
                brief["variant_analysis"]["discovery"] = provenance
                keep = {field["id"] for field in scope.fields} | {
                    slot["original_field"] for slot in brief["variant_analysis"]["slots"]} | {
                    choice["field_id"] for slot in brief["variant_analysis"]["slots"] for choice in slot["choices"]}
                scope.fields = [field for field in discovered.fields if field["id"] in keep]
                scope.datasets = discovered.datasets
                scope.synced_at = datetime.now(timezone.utc)
                brief["catalogue_synced_at"] = scope.synced_at.isoformat()
                values["brief"] = brief
            else:
                if scope is not None and scope.status == "COMPLETE":
                    from alpha_platform.research.discovery import resolve_template_scope, template_fields
                    from alpha_platform.research.catalog import CatalogRateLimited
                    discovered, discoveries = scope, []
                    for expression in body.manual_plan.expressions:
                        known = {field["id"] for field in discovered.fields}
                        if set(template_fields(expression)) - known:
                            try:
                                discovered, provenance = resolve_template_scope(discovered, expression, include_replacements=False)
                            except CatalogRateLimited as error:
                                raise ValueError(f"Field discovery paused by BRAIN rate limits. Retry saving after {error.retry_at.isoformat()}. "
                                                 "Incomplete pages are retained; no simulations were started.") from error
                            discoveries.append(provenance)
                    values["brief"] = manual_brief(body, discovered)
                    if discoveries:
                        scope.fields, scope.datasets = discovered.fields, discovered.datasets
                        scope.synced_at = datetime.now(timezone.utc)
                        values["brief"]["catalogue_synced_at"] = scope.synced_at.isoformat()
                        values["brief"]["field_discovery"] = discoveries
                else:
                    values["brief"] = manual_brief(body, scope)
        if body.mode == "redevelop":
            from alpha_platform.research.redevelopment import redevelopment_plan
            from copy import deepcopy
            prepared = values["brief"]
            fields = prepared["field_evidence"]
            branches, analysis = redevelopment_plan(body.source_expression, fields, body.redevelopment_windows, body.max_attempts,
                                                   {op['name'] for op in discovered.capabilities.get('operators', [])})
            checked_body = deepcopy(body)
            checked_body.manual_plan.expressions = [r["expression"] for r in branches]
            checked_brief = manual_brief(checked_body, discovered)
            for row, checked in zip(branches, checked_brief["manual_expressions"]):
                row["expression"] = checked["expression"]
            checked_brief["manual_expressions"] = branches
            checked_brief["templates"] = [{"name":r["template"], "expression":r["expression"], "hypothesis":r["rationale"], "slots":[], "windows":[]} for r in branches]
            selected_run = db.get(SimulationRun, body.parent_run_id) if body.parent_run_id else None
            submitted_parent = None
            if body.parent_submitted_alpha_id:
                from alpha_platform.research.submitted import parent_reference
                submitted_parent = parent_reference(db,body.parent_submitted_alpha_id,analysis["parent_expression"],checked_brief["settings"])
            if body.parent_run_id:
                if selected_run is None or selected_run.status != "COMPLETE":
                    raise ValueError("Select a completed local parent run.")
                saved_parent = db.get(AlphaConfig, selected_run.config_id)
                if saved_parent.sim_type != "REGULAR" or serialize(parse(saved_parent.regular_code)) != analysis["parent_expression"]:
                    raise ValueError("The expression must match the selected parent run. Start a new redevelopment project to use another parent.")
                for k in ("region","universe","delay","neutralization","decay","truncation"):
                    if getattr(saved_parent,k) != checked_brief["settings"][k] and not (k == "truncation" and float(getattr(saved_parent,k)) == checked_brief["settings"][k]):
                        raise ValueError("Keep the selected parent's scope, neutralization, decay and truncation for a controlled comparison.")
                for k in ("pasteurization","test_period","unit_handling","nan_handling","max_trade","visualization","simulation_mode"):
                    if getattr(saved_parent,k) is not None:
                        checked_brief["settings"][k] = getattr(saved_parent,k)
            parent = get_or_create_config(db, ace.generate_alpha(regular=analysis["parent_expression"], **checked_brief["settings"]), origin="seed")
            run = db.query(SimulationRun).filter_by(config_id=parent.config_id, status="COMPLETE").order_by(SimulationRun.submitted_at.desc()).first()
            if selected_run: run = selected_run
            perf = db.get(AlphaPerformance, run.run_id) if run else None
            analysis["parent"] = {"config_id":str(parent.config_id), "run_id":str(run.run_id) if run else None,
                                  "metrics":{k:finite_number(getattr(perf,k)) if perf else None for k in ("sharpe","fitness","turnover","self_correlation","production_correlation")},
                                  "alpha_id":run.alpha_id if run else None, "settings_match":True, "note":"Saved local baseline under the exact same settings; no parent simulation is submitted. Missing history remains unverified."}
            if submitted_parent:
                analysis["parent"] = {**submitted_parent,"config_id":str(parent.config_id)}
            checked_brief["redevelopment"] = analysis
            checked_brief["unknowns"] += analysis["unavailable"]
            checked_brief["settings_rationale"] = "User selected the parent market scope and settings. Baseline comparisons use exact matching simulation settings."
            values["brief"] = checked_brief
        c = ResearchCampaign(**values)
        db.add(c)
        db.commit()
        return campaign_json(c, [])


def launch(campaign_id, retry=False, connection=None):
    campaign_id = uuid.UUID(str(campaign_id))
    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(736281903)"))
        c = db.get(ResearchCampaign, campaign_id)
        allowed = {"ERROR", "INTERRUPTED"} if retry else {"DRAFT"}
        if c is None or c.status not in allowed:
            raise ValueError("This project cannot be launched in its current state. Duplicate launches are blocked.")
        if retry:
            if db.query(ResearchCandidate).filter_by(campaign_id=campaign_id).first():
                raise ValueError("This project has saved simulation candidates. Inspect its runs before retrying; automatic replay is blocked.")
            if connection:
                c.provider, c.model = connection.provider, connection.model
        if db.query(ResearchCampaign).filter(ResearchCampaign.status.in_(ACTIVE)).first():
            raise ValueError("Another research campaign is active. Wait for it or stop it first.")
        if c.provider != "manual" and not api_key(c.provider):
            raise ValueError(f"Configure {PROVIDERS[c.provider]} credentials in LLM Integration first.")
        if not db.query(CatalogScope).filter_by(status="COMPLETE").first():
            raise ValueError("Sync at least one complete BRAIN catalogue before starting research.")
        c.status, c.error, c.stop_requested = "QUEUED", None, False
        c.updated_at = datetime.now(timezone.utc)
        db.commit()
    WORKER.submit(run_campaign, campaign_id)
    return get_campaign(campaign_id)


def stop(campaign_id):
    with SessionLocal() as db:
        c = db.get(ResearchCampaign, uuid.UUID(str(campaign_id)))
        if c is None:
            raise ValueError("Campaign not found.")
        if c.status in ACTIVE:
            c.stop_requested = True
            c.status = "STOPPING"
            c.updated_at = datetime.now(timezone.utc)
            db.commit()
    return get_campaign(campaign_id)


def set_status(campaign_id, status, error=None):
    with SessionLocal() as db:
        c = db.get(ResearchCampaign, campaign_id)
        c.status, c.error = status, error
        c.updated_at = datetime.now(timezone.utc)
        db.commit()


def history(db):
    values = defaultdict(list)
    for decision in db.query(RlDecision).filter(RlDecision.policy_version == POLICY_VERSION,
                                               RlDecision.observed_reward.isnot(None)).all():
        values[decision.action_taken["arm"]].append(float(decision.observed_reward))
    return values


def arm_key(category, template):
    return category.lower() + ":" + hashlib.sha256(template.encode()).hexdigest()[:16]


def run_campaign(campaign_id):
    with RUN_LOCK:
        try:
            set_status(campaign_id, "RESEARCHING")
            with SessionLocal() as db:
                c = db.get(ResearchCampaign, campaign_id)
                scopes = db.query(CatalogScope).filter_by(status="COMPLETE").all()
                memories = [{"objective": p.objective, "hypothesis": p.brief.get("hypothesis"),
                             "unknowns": p.brief.get("unknowns"), "settings": p.brief.get("settings")}
                            for p in db.query(ResearchCampaign).filter(ResearchCampaign.brief.isnot(None)).order_by(
                                ResearchCampaign.created_at.desc()).limit(12).all()]
                if c.mode in {"manual", "redevelop"}:
                    brief = c.brief
                    planned = brief["manual_expressions"]
                else:
                    brief, planned = research_plan(c, scopes, memories)
                c.brief = brief
                if c.stop_requested:
                    c.status = "STOPPED"
                    db.commit()
                    return
                # Persist the whole plan before any simulation or quota reservation.
                db.execute(text("SELECT pg_advisory_xact_lock(736281901)"))
                source_expression = c.source_expression or (brief.get("variant_analysis") or {}).get("source_expression")
                parent = get_or_create_config(db, ace.generate_alpha(regular=source_expression, **brief["settings"]),
                                              origin="seed") if source_expression else None
                for ordinal, row in enumerate(planned, 1):
                    payload = ace.generate_alpha(regular=row["expression"], **brief["settings"])
                    config = get_or_create_config(db, payload, origin="manual" if c.mode in {"manual", "redevelop"} else "llm_hypothesis")
                    structure = db.get(AlphaStructure, config.config_id)
                    selected_scope = next(s for s in scopes if s.scope_id == brief["scope_id"])
                    if structure:
                        datasets = sorted({field_dataset(f) for f in selected_scope.fields
                                           if f["id"] in structure.data_fields and field_dataset(f)})
                        structure.dataset_ids, structure.dataset_count = datasets, len(datasets)
                    if parent and parent.config_id != config.config_id and config.parent_config_id is None:
                        config.parent_config_id = parent.config_id
                        config.generation = (parent.generation or 0) + 1
                    db.add(ResearchCandidate(campaign_id=campaign_id, ordinal=ordinal, expression=row["expression"],
                                             template=row["template"], rationale=row["rationale"], payload=payload,
                                             config_id=config.config_id))
                c.status = "RUNNING"
                c.updated_at = datetime.now(timezone.utc)
                db.commit()
            execute_plan(campaign_id)
        except Exception as exc:
            set_status(campaign_id, "ERROR", str(exc) if isinstance(exc, (ValueError, SubmissionBlocked)) else
                       "Campaign interrupted by a connection or processing error. Saved runs must be inspected before starting another campaign.")


def execute_plan(campaign_id):
    with SessionLocal() as db:
        c = db.get(ResearchCampaign, campaign_id)
        concurrency = c.concurrency
    futures = {}
    fatal = None
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="research-simulation") as executor:
        while True:
            with SessionLocal() as db:
                c = db.get(ResearchCampaign, campaign_id)
                rows = db.query(ResearchCandidate).filter_by(campaign_id=campaign_id).order_by(ResearchCandidate.ordinal).all()
                qualified = sum(bool(r.evaluation and r.evaluation.get("qualifies")) for r in rows)
                planned = [r for r in rows if r.status == "PLANNED"]
                if not c.stop_requested and not fatal and qualified < 4:
                    # Stop dispatching at four qualifying results; already accepted work finishes.
                    capacity = concurrency - len(futures)
                    if c.mode == "redevelop" and planned:
                        metadata = {r["template"]:r for r in c.brief["redevelopment"]["branches"]}
                        active_rows = [r for r in rows if r.status == "RUNNING"]
                        phase = min(metadata[r.template]["phase"] for r in planned + active_rows)
                        planned = [r for r in planned if metadata[r.template]["phase"] == phase]
                        if phase == 2:
                            useful = {metadata[r.template]["family"] for r in rows if metadata[r.template]["phase"] == 1
                                      and r.evaluation and (r.evaluation.get("metrics",{}).get("sharpe") or 0) > 0
                                      and (r.evaluation.get("metrics",{}).get("fitness") or 0) > 0
                                      and r.evaluation.get("metrics",{}).get("turnover") is not None
                                      and r.evaluation["metrics"]["turnover"] <= c.criteria["max_turnover"]}
                            for r in planned:
                                if metadata[r.template]["family"] not in useful:
                                    r.status = "SKIPPED"
                                    r.telemetry = {"stage":"Not selected", "error":"Base branch did not meet the positive Sharpe, fitness, and turnover screening rule."}
                            planned = [r for r in planned if r.status == "PLANNED"]
                    past = history(db)
                    for _ in range(max(0, capacity)):
                        if not planned:
                            break
                        templates = {t["name"]: t["expression"] for t in c.brief["templates"]}
                        arms = list(dict.fromkeys(arm_key(c.category, templates[r.template]) for r in planned))
                        arm = choose_arm(past, arms)
                        row = next(r for r in planned if arm_key(c.category, templates[r.template]) == arm)
                        planned.remove(row)
                        row.status = "RUNNING"
                        decision = RlDecision(config_id=row.config_id, policy_version=POLICY_VERSION,
                            state_snapshot={"campaign_id": str(campaign_id), "category": c.category,
                                            "scope_id": c.brief["scope_id"], "criteria": c.criteria,
                                            "observations": len(past.get(arm, []))},
                            action_taken={"arm": arm, "template": row.template, "candidate_id": str(row.candidate_id)})
                        db.add(decision)
                        db.commit()
                        futures[executor.submit(run_candidate, row.candidate_id, decision.decision_id)] = row.candidate_id
                        # Explore another untried arm while the selected result is pending.
                        past[arm].append(0)
                if not futures and not fatal and not c.stop_requested and qualified < 4 and any(r.status == "PLANNED" for r in rows):
                    db.commit()
                    continue
                if not futures:
                    attempts = sum(r.run_id is not None for r in rows)
                    terminal = "ERROR" if fatal else "STOPPED" if c.stop_requested else "AWAITING_VALIDATION" if qualified >= 4 else "BUDGET_COMPLETE" if attempts >= c.max_attempts else "PLAN_COMPLETE"
                    c.status, c.error = terminal, fatal
                    c.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    return
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                futures.pop(future)
                try:
                    future.result()
                except MonitoringCancelled:
                    pass  # cancel_candidate already requested the campaign to stop dispatching.
                except Exception as error:
                    detail = str(error) if isinstance(error, (ValueError, TimeoutError)) else type(error).__name__
                    fatal = f"Simulation or checks could not be confirmed: {detail[:500]}. Inspect the alpha's saved error before continuing."


def run_candidate(candidate_id, decision_id):
    with SessionLocal() as db:
        row = db.get(ResearchCandidate, candidate_id)
        campaign = db.get(ResearchCampaign, row.campaign_id)
        decision = db.get(RlDecision, decision_id)
        def cancelled():
            with SessionLocal() as current:
                candidate = current.get(ResearchCandidate, candidate_id)
                return bool((candidate.telemetry or {}).get("cancel_requested"))
        def linked(run):
            db.refresh(row, ["telemetry"])
            row.run_id, decision.run_id = run.run_id, run.run_id
            row.telemetry = {**(row.telemetry or {}), "stage":"Submitting to BRAIN", "updated_at":datetime.now(timezone.utc).isoformat()}
            db.commit()
        def observed(url, response):
            if not (url.startswith(ace.brain_api_url + "/simulations") or url.startswith(ace.brain_api_url + "/alphas/")):
                return
            db.refresh(row, ["telemetry"])
            telemetry = {**(row.telemetry or {}), "http_status":response.status_code,
                         "updated_at":datetime.now(timezone.utc).isoformat(),
                         "stage":"Simulation processing" if "/simulations/" in url else "Fetching results and checks"}
            try:
                data = response.json()
                progress = finite_number(data.get("progress"))
                if progress is not None and 0 <= progress <= 1:
                    telemetry["progress"] = round(progress * 100, 2)
                if data.get("status") == "ERROR" or response.status_code >= 400:
                    telemetry["error"] = f"BRAIN HTTP {response.status_code}: {response_error(response)}"
            except (ValueError, AttributeError):
                pass
            row.telemetry = telemetry
            db.commit()
        try:
            with ResearchSession() as session:
                result = simulate_confirmed(db, session, row.payload, confirmed_hash=hash_alpha(row.payload),
                                            origin="manual" if campaign.mode in {"manual", "redevelop"} else "llm_hypothesis", triggered_by="bandit_policy", check_submission=True,
                                            on_run=linked, on_response=observed, cancel_requested=cancelled)
            reused = False
            if result["skipped"] and campaign.mode == "redevelop":
                saved = db.query(SimulationRun).filter_by(config_id=result["config"].config_id, status="COMPLETE").order_by(SimulationRun.submitted_at.desc()).first()
                if saved is not None:
                    result["run"], result["skipped"], reused = saved, False, True
            if result["skipped"]:
                row.status = "SKIPPED"
                db.commit()
                return
            run = result["run"]
            row.run_id = run.run_id
            decision.run_id = run.run_id
            perf = db.get(AlphaPerformance, run.run_id)
            metrics = {k: finite_number(getattr(perf, k)) if perf else None
                       for k in ("sharpe", "fitness", "turnover", "self_correlation", "production_correlation")}
            checks = (perf.ladder_metrics or {}).get("checks", []) if perf else []
            row.evaluation = evaluate(metrics, checks, campaign.criteria)
            if campaign.mode == "redevelop":
                from alpha_platform.research.redevelopment import parent_comparison
                metadata = next(r for r in campaign.brief["redevelopment"]["branches"] if r["template"] == row.template)
                row.evaluation = {**row.evaluation, "comparison":parent_comparison(metrics, campaign.brief["redevelopment"]["parent"]), "phase":metadata["phase"]}
                if metadata["phase"] == 0:
                    row.evaluation = {**row.evaluation, "qualifies":False, "diagnostic":True}
            row.status = "QUALIFIED" if run.status == "COMPLETE" and row.evaluation["qualifies"] else "COMPLETE" if run.status == "COMPLETE" else "ERROR"
            db.refresh(row, ["telemetry"])
            row.telemetry = {**(row.telemetry or {}), "progress":100 if run.status == "COMPLETE" else (row.telemetry or {}).get("progress"),
                             "stage":"Saved result reused" if reused else "Complete" if run.status == "COMPLETE" else "Completion unconfirmed",
                             "error":run.error_message, "updated_at":datetime.now(timezone.utc).isoformat()}
            scored = reward(metrics)
            decision.observed_reward = None if reused else scored["score"] if run.status == "COMPLETE" else -1
            decision.reward_components = {**scored, "qualification": row.evaluation["qualifies"], "baseline": scored["score"], "reused_saved_result":reused}
            decision.rewarded_at = datetime.now(timezone.utc)
            db.commit()
            if run.status != "COMPLETE":
                raise ValueError(run.error_message or "Simulation did not complete.")
        except Exception as error:
            db.rollback()
            row.status = "CANCELLED" if isinstance(error, MonitoringCancelled) else "ERROR"
            db.refresh(row, ["telemetry"])
            detail = str(error) if isinstance(error, (ValueError, TimeoutError)) else f"{type(error).__name__} while processing simulation results"
            row.telemetry = {**(row.telemetry or {}), "stage":"Tracking stopped" if isinstance(error, MonitoringCancelled) else "Error",
                             "error": detail[:700], "updated_at":datetime.now(timezone.utc).isoformat()}
            row.evaluation = {"qualifies": False, "reasons": [], "unverified": ["simulation outcome"], "metrics": {}, "checks": []}
            db.commit()
            raise


def validate_candidate(candidate_id, decision, feedback):
    if decision not in {"accepted", "rejected"}:
        raise ValueError("Choose accepted or rejected.")
    if decision == "rejected" and len(feedback.strip()) < 5:
        raise ValueError("Provide a reason for rejection so the learning history is useful.")
    with SessionLocal() as db:
        row = db.query(ResearchCandidate).filter_by(candidate_id=uuid.UUID(str(candidate_id))).with_for_update().one_or_none()
        if row is None or row.status != "QUALIFIED":
            raise ValueError("Only verified qualifying candidates can be validated.")
        if row.decision:
            raise ValueError("This candidate has already been validated.")
        row.decision, row.feedback = decision, feedback
        row.decided_at = datetime.now(timezone.utc)
        event = db.query(RlDecision).filter(RlDecision.action_taken["candidate_id"].astext == str(row.candidate_id),
                                          RlDecision.policy_version == POLICY_VERSION).first()
        if event:
            adjustment = 0.25 if decision == "accepted" else -0.5
            event.observed_reward = float(event.observed_reward or 0) + adjustment
            event.reward_components = {**(event.reward_components or {}), "user_validation": adjustment,
                                       "feedback": feedback}
        db.commit()
        campaign_id = row.campaign_id
        c = db.get(ResearchCampaign, campaign_id)
        qualified = db.query(ResearchCandidate).filter_by(campaign_id=campaign_id, status="QUALIFIED").all()
        if qualified and all(q.decision for q in qualified) and c.status not in ACTIVE:
            c.status = "VALIDATED"
            c.updated_at = datetime.now(timezone.utc)
            db.commit()
    return get_campaign(campaign_id)


def cancel_candidate(candidate_id):
    with SessionLocal() as db:
        row = db.query(ResearchCandidate).filter_by(candidate_id=uuid.UUID(str(candidate_id))).with_for_update().one_or_none()
        if row is None or row.status not in {"PLANNED", "RUNNING"}:
            raise ValueError("Only queued or running alphas can be stopped.")
        running = row.status == "RUNNING"
        row.telemetry = {**(row.telemetry or {}), "cancel_requested":True,
                         "updated_at":datetime.now(timezone.utc).isoformat(),
                         "stage":"Stop requested" if running else "Cancelled before submission"}
        if running:
            campaign = db.get(ResearchCampaign, row.campaign_id)
            campaign.stop_requested = True
            campaign.status = "STOPPING"
        else:
            row.status = "CANCELLED"
        db.commit()
        campaign_id = row.campaign_id
    return get_campaign(campaign_id)


def resume_campaign(campaign_id):
    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(736281903)"))
        c = db.get(ResearchCampaign, uuid.UUID(str(campaign_id)))
        if c is None or c.status not in {"ERROR", "STOPPED", "INTERRUPTED"}:
            raise ValueError("This campaign cannot be continued in its current state.")
        if db.query(ResearchCampaign).filter(ResearchCampaign.status.in_(ACTIVE)).first():
            raise ValueError("Another campaign is active.")
        rows = db.query(ResearchCandidate).filter_by(campaign_id=c.campaign_id).all()
        if not any(row.status == "PLANNED" for row in rows) or any(row.status == "RUNNING" for row in rows):
            raise ValueError("No safely resumable queued experiments are available.")
        from alpha_platform.db.models import SimulationRun
        for row in rows:
            runs = db.query(SimulationRun).filter_by(config_id=row.config_id).all()
            if any(run.status in {"WAITING", "SIMULATING", "TIMEOUT"} for run in runs):
                raise ValueError("A saved run has an uncertain outcome. Inspect BRAIN before continuing; it will not be resubmitted.")
        if sum(bool(row.evaluation and row.evaluation.get("qualifies")) for row in rows) >= 4:
            raise ValueError("Four candidates are awaiting validation.")
        c.stop_requested, c.error, c.status = False, None, "RUNNING"
        db.commit()
        identity = c.campaign_id
    WORKER.submit(execute_plan, identity)
    return get_campaign(identity)


def learning_summary():
    with SessionLocal() as db:
        past = history(db)
        events = db.query(RlDecision).filter(RlDecision.policy_version == POLICY_VERSION).order_by(RlDecision.decided_at.desc()).limit(100).all()
        return {"policy": POLICY_VERSION, "observations": sum(len(v) for v in past.values()),
                "arms": [{"arm": k, "observations": len(v), "mean_reward": sum(v) / len(v)} for k, v in past.items()],
                "events": [{"id": str(e.decision_id), "state": e.state_snapshot, "action": e.action_taken,
                            "reward": float(e.observed_reward) if e.observed_reward is not None else None,
                            "components": e.reward_components, "at": e.decided_at.isoformat()} for e in events]}


def recover_interrupted():
    """On startup, surface interrupted work. Never replay an uncertain submission."""
    with SessionLocal() as db:
        for c in db.query(ResearchCampaign).filter(ResearchCampaign.status.in_(ACTIVE)).all():
            c.status = "INTERRUPTED"
            c.error = "Server restarted during this campaign. Inspect saved runs; no simulation was resubmitted."
        for s in db.query(CatalogScope).filter(CatalogScope.status.in_(["SYNCING", "QUEUED"])).all():
            s.status, s.error = "ERROR", "Sync interrupted by restart. Retry to complete the catalogue."
        db.commit()

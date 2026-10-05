"""Disposable Postgres schema; research/provider/simulation network calls are mocked."""
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch
import test_local_workflow as local_workflow
from alpha_platform.db.models import (ResearchCampaign, ResearchCandidate, CatalogScope,
                                      SimulationRun, AlphaPerformance, RlDecision)
from alpha_platform.research import campaigns
from alpha_platform.research.campaigns import CampaignInput
from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.dedupe.hashing import hash_alpha
from alpha_platform.brain_client.ace_lib_adapter import get_or_create_config
from alpha_platform.research import catalog
from datetime import timedelta


class CampaignIntegrationTests(unittest.TestCase):
    setUp = local_workflow.PostgresWorkflowTests.setUp
    tearDown = local_workflow.PostgresWorkflowTests.tearDown

    def test_numpy_correlation_metrics_save_as_native_numbers(self):
        import pandas as pd
        import numpy as np
        from alpha_platform.brain_client.ace_lib_adapter import persist_simulation_result
        with self.sessions() as db:
            config=get_or_create_config(db,ace.generate_alpha(regular="rank(close)"),origin="manual")
            run=SimulationRun(config_id=config.config_id,status="TIMEOUT",triggered_by="manual",error_message="Previous result save failed")
            db.add(run);db.commit()
            result={"alpha_id":"saved-alpha","is_stats":pd.DataFrame([{"sharpe":np.float64(1.99),"fitness":.77,"turnover":.4}]),
                "is_tests":pd.DataFrame([{"name":"SELF_CORRELATION","result":"PASS","value":np.float64(.3895)},
                                         {"name":"PROD_CORRELATION","result":"PASS","value":np.float64(.8446)}])}
            persist_simulation_result(db,config,result,reserved_run=run)
            db.commit()
            perf=db.get(AlphaPerformance,run.run_id)
            self.assertAlmostEqual(float(perf.self_correlation),.3895)
            self.assertAlmostEqual(float(perf.production_correlation),.8446)
            self.assertEqual(run.status,"COMPLETE")
            self.assertIsNone(run.error_message)

    def test_redevelopment_saves_review_plan_and_screens_followup_windows(self):
        from alpha_platform.research.campaigns import ManualPlanInput
        from alpha_platform.config.operators import load_catalog
        with self.sessions() as db:
            db.add(CatalogScope(scope_id="redevelop-test", settings={"instrumentType":"EQUITY","region":"USA","delay":1,"universe":"TOP3000"},
                fields=[{"id":"close","type":"MATRIX","description":"The closing share price on each trading day."},
                        {"id":"volume","type":"MATRIX","description":"The traded number of shares on each trading day."}],
                datasets=[],capabilities={"neutralizations":["SUBINDUSTRY"],"operators":[{"name":n} for n in load_catalog()]},
                status="COMPLETE",synced_at=datetime.now(timezone.utc)))
            db.commit()
        body=CampaignInput(name="Redevelop parent",objective="Test structural changes with verified field documentation", mode="redevelop",
            provider="manual",model="manual",source_expression="group_neutralize(rank(close)+rank(volume),subindustry)",
            criteria={"min_sharpe":5,"min_fitness":3,"max_turnover":.3},max_attempts=50,concurrency=8,
            manual_plan=ManualPlanInput(scope_id="redevelop-test",expressions=["parent"],neutralization="SUBINDUSTRY"))
        with self.sessions() as db:
            parent=get_or_create_config(db,ace.generate_alpha(regular=body.source_expression,neutralization="SUBINDUSTRY",nan_handling="ON"),origin="manual")
            run=SimulationRun(config_id=parent.config_id,status="COMPLETE",triggered_by="manual",alpha_id="parent-alpha")
            db.add(run);db.flush()
            db.add(AlphaPerformance(run_id=run.run_id,config_id=parent.config_id,sharpe=6,fitness=4,turnover=.2))
            db.commit();body.parent_run_id=run.run_id
        calls=[]
        def fake_simulate(db, session, payload, **kwargs):
            self.assertEqual(payload["settings"]["nanHandling"],"ON")
            candidate=db.query(ResearchCandidate).filter_by(expression=payload["regular"]).first()
            c=db.get(ResearchCampaign,candidate.campaign_id)
            meta=next(r for r in c.brief["redevelopment"]["branches"] if r["template"]==candidate.template)
            phase=meta["phase"]
            if phase==1:
                self.assertFalse(db.query(ResearchCandidate).filter_by(campaign_id=c.campaign_id,status="RUNNING").filter(ResearchCandidate.template.like("Component%" )).count())
            self.assertNotEqual(phase,2)
            config=get_or_create_config(db,payload,origin="manual")
            run=SimulationRun(config_id=config.config_id,status="COMPLETE",triggered_by="bandit_policy",alpha_id="mock")
            db.add(run); db.flush()
            db.add(AlphaPerformance(run_id=run.run_id,config_id=config.config_id,sharpe=10 if phase==0 else -1,
                fitness=5 if phase==0 else -1,turnover=.1,ladder_metrics={"checks":[{"name":"CHECK","result":"PASS"}]}))
            db.commit();calls.append(phase)
            return {"config":config,"run":None if phase==0 else run,"skipped":phase==0}
        with patch.object(campaigns,"SessionLocal",self.sessions), patch.object(campaigns,"ResearchSession",MagicMock()), patch.object(campaigns,"simulate_confirmed",side_effect=fake_simulate), patch.object(campaigns.WORKER,"submit") as worker:
            result=campaigns.create_campaign(body)
            worker.assert_not_called()
            self.assertEqual(result["status"],"DRAFT")
            self.assertTrue(result["brief"]["redevelopment"]["branches"])
            self.assertEqual(result["brief"]["redevelopment"]["parent"]["run_id"],str(body.parent_run_id))
            self.assertEqual(result["brief"]["redevelopment"]["parent"]["metrics"]["sharpe"],6)
            self.assertEqual(result["candidates"],[])
            identity=__import__("uuid").UUID(result["id"])
            campaigns.run_campaign(identity)
            result=campaigns.get_campaign(identity)
            self.assertEqual(result["qualified"],0)
            self.assertEqual(calls,sorted(calls))
            diagnostics=[r for r in result["candidates"] if r["template"].startswith("Component")]
            self.assertTrue(all(r["telemetry"]["stage"]=="Saved result reused" for r in diagnostics))
            with self.sessions() as db:
                self.assertTrue(all(d.observed_reward is None for d in db.query(RlDecision).filter(RlDecision.run_id.in_([__import__("uuid").UUID(r["run_id"]) for r in diagnostics])).all()))
            self.assertTrue(any(r["status"]=="SKIPPED" for r in result["candidates"]))
            self.assertTrue(all(r["evaluation"]["comparison"]["parent_correlation"] is None for r in result["candidates"] if r["evaluation"]))

    def test_resume_keeps_completed_runs_and_blocks_uncertain_outcomes(self):
        with self.sessions() as db:
            c=ResearchCampaign(name="Resume test",objective="Continue only experiments not already dispatched.",mode="manual",
                category="other",provider="manual",model="manual",criteria={},max_attempts=2,concurrency=1,status="STOPPED",brief={})
            db.add(c);db.flush()
            config=get_or_create_config(db,ace.generate_alpha(regular="rank(close)"),origin="manual")
            run=SimulationRun(config_id=config.config_id,status="TIMEOUT",triggered_by="bandit_policy")
            db.add(run);db.flush()
            db.add_all([ResearchCandidate(campaign_id=c.campaign_id,config_id=config.config_id,run_id=run.run_id,
                ordinal=1,expression="rank(close)",template="test",rationale="test",payload={},status="COMPLETE"),
                ResearchCandidate(campaign_id=c.campaign_id,ordinal=2,expression="rank(open)",template="test",rationale="test",payload={},status="PLANNED")])
            db.commit();cid,rid=c.campaign_id,run.run_id
        with patch.object(campaigns,"SessionLocal",self.sessions),patch.object(campaigns.WORKER,"submit") as submit:
            with self.assertRaisesRegex(ValueError,"uncertain outcome"):
                campaigns.resume_campaign(cid)
            submit.assert_not_called()
            with self.sessions() as db:
                db.get(SimulationRun,rid).status="COMPLETE";db.commit()
            result=campaigns.resume_campaign(cid)
            self.assertEqual(result["status"],"RUNNING")
            self.assertEqual(result["candidates"][0]["run_id"],str(rid))
            self.assertEqual(result["candidates"][1]["status"],"PLANNED")
            submit.assert_called_once_with(campaigns.execute_plan,cid)

    def test_progress_run_link_and_nan_metrics_are_persisted_without_failure(self):
        import uuid
        from decimal import Decimal
        campaign=ResearchCampaign(name="Progress test",objective="Observe progress and missing metric handling.",
            mode="manual",category="other",provider="manual",model="manual",criteria={},max_attempts=1,
            concurrency=1,status="RUNNING",brief={})
        with self.sessions() as db:
            db.add(campaign);db.flush()
            config=get_or_create_config(db,ace.generate_alpha(regular="rank(close)"),origin="manual")
            row=ResearchCandidate(campaign_id=campaign.campaign_id,config_id=config.config_id,ordinal=1,
                expression="rank(close)",template="test",rationale="test",payload=ace.generate_alpha(regular="rank(close)"),status="RUNNING")
            decision=RlDecision(config_id=config.config_id,policy_version=campaigns.POLICY_VERSION,
                state_snapshot={},action_taken={"arm":"test"})
            db.add_all([row,decision]);db.commit()
            row_id,decision_id,campaign_id,config_id=row.candidate_id,decision.decision_id,campaign.campaign_id,config.config_id
        def fake_simulate(db,session,payload,**kwargs):
            run=SimulationRun(config_id=config_id,status="SIMULATING",triggered_by="bandit_policy")
            db.add(run);db.commit()
            kwargs["on_run"](run)
            response=MagicMock(status_code=200);response.json.return_value={"progress":.35}
            kwargs["on_response"]("https://api.worldquantbrain.com/simulations/test",response)
            with self.sessions() as current:
                saved=current.get(ResearchCandidate,row_id)
                self.assertEqual(saved.telemetry["progress"],35)
                self.assertEqual(saved.run_id,run.run_id)
            run.status="COMPLETE"
            db.add(AlphaPerformance(run_id=run.run_id,config_id=run.config_id,sharpe=2,fitness=2,turnover=.1,
                self_correlation=Decimal("NaN"),production_correlation=Decimal("NaN"),
                ladder_metrics={"checks":[{"name":"CHECK","result":"PASS"}]}))
            db.commit()
            return {"run":run,"skipped":False}
        with patch.object(campaigns,"SessionLocal",self.sessions),patch.object(campaigns,"ResearchSession"),patch.object(
                campaigns,"simulate_confirmed",side_effect=fake_simulate):
            campaigns.run_candidate(row_id,decision_id)
            saved=campaigns.get_campaign(campaign_id)["candidates"][0]
            self.assertEqual(saved["telemetry"]["progress"],100)
            self.assertIsNone(saved["evaluation"]["metrics"]["self_correlation"])
            self.assertEqual(saved["status"],"QUALIFIED")

    def test_queued_cancel_and_running_stop_are_distinct(self):
        with self.sessions() as db:
            c=ResearchCampaign(name="Cancel test",objective="Test explicit local cancellation handling.",mode="manual",
                category="other",provider="manual",model="manual",criteria={},max_attempts=2,concurrency=1,status="RUNNING",brief={})
            db.add(c);db.flush()
            rows=[ResearchCandidate(campaign_id=c.campaign_id,ordinal=i,expression="rank(close)",template="test",rationale="test",payload={},status=status)
                  for i,status in ((1,"PLANNED"),(2,"RUNNING"))]
            db.add_all(rows);db.commit()
            first,second,cid=rows[0].candidate_id,rows[1].candidate_id,c.campaign_id
        with patch.object(campaigns,"SessionLocal",self.sessions):
            result=campaigns.cancel_candidate(first)
            self.assertEqual(result["status"],"RUNNING")
            self.assertEqual(result["candidates"][0]["status"],"CANCELLED")
            result=campaigns.cancel_candidate(second)
            self.assertEqual(result["status"],"STOPPING")
            self.assertTrue(result["candidates"][1]["telemetry"]["cancel_requested"])

    def test_manual_missing_field_discovery_runs_when_variant_count_is_zero(self):
        from types import SimpleNamespace
        scope_id="EQUITY/USA/1/TOP3000"
        settings={"instrumentType":"EQUITY","region":"USA","delay":1,"universe":"TOP3000"}
        capabilities={"neutralizations":["SUBINDUSTRY"],"operators":[{"name":"rank"}]}
        source={"id":"missing_margin","type":"MATRIX","description":"Operating margin for each company", "dataset":{"id":"fund"}}
        datasets=[{"id":"fund","category":{"id":"fundamental"}}]
        with self.sessions() as db:
            db.add(CatalogScope(scope_id=scope_id,settings=settings,capabilities=capabilities,
                               datasets=[],fields=[],status="COMPLETE"))
            db.commit()
        discovered=SimpleNamespace(scope_id=scope_id,settings=settings,capabilities=capabilities,
            synced_at=None,status="COMPLETE",datasets=datasets,fields=[source])
        body=CampaignInput(name="Missing manual field",objective="Test the missing documented profitability field.",
            mode="manual",provider="manual",model="manual",manual_plan={"scope_id":scope_id,
                "expressions":["rank(missing_margin)"],"neutralization":"SUBINDUSTRY"})
        with patch.object(campaigns,"SessionLocal",self.sessions),patch(
                "alpha_platform.research.discovery.resolve_template_scope",return_value=(discovered,{})) as resolve:
            created=campaigns.create_campaign(body)
            self.assertEqual(created["status"],"DRAFT")
            self.assertEqual(created["candidates"],[])
            self.assertEqual(resolve.call_args.kwargs,{"include_replacements":False})
            with self.sessions() as db:
                self.assertEqual(db.get(CatalogScope,scope_id).fields,[source])

    def test_template_discovery_merges_top_fifty_and_saves_draft_before_runs(self):
        from types import SimpleNamespace
        scope_id="EQUITY/USA/1/TOP3000"
        settings={"instrumentType":"EQUITY","region":"USA","delay":1,"universe":"TOP3000"}
        capabilities={"neutralizations":["SUBINDUSTRY"],"operators":[{"name":"rank"}]}
        old={"id":"close","type":"MATRIX","description":"Closing equity price for each instrument.","dataset":{"id":"price"}}
        source={"id":"source_margin","type":"MATRIX","description":"Operating profitability margin capital return",
                "dataset":{"id":"fund"},"coverage":.5,"dateCoverage":1}
        extra=[{**source,"id":f"replacement_{i}","coverage":i/100} for i in range(1,71)]
        datasets=[{"id":"price","category":{"id":"price-volume"}}, {"id":"fund","category":{"id":"fundamental"}}]
        with self.sessions() as db:
            db.add(CatalogScope(scope_id=scope_id,settings=settings,capabilities=capabilities,
                               datasets=datasets[:1],fields=[old],status="COMPLETE"))
            db.commit()
        discovered=SimpleNamespace(scope_id=scope_id,settings=settings,capabilities=capabilities,
            synced_at=None,status="COMPLETE",datasets=datasets,fields=[old,source,*extra])
        body=CampaignInput(name="Automatic discovery",objective="Test compatible profitability margin replacements.",
            mode="manual",provider="manual",model="manual",max_attempts=100,
            manual_plan={"scope_id":scope_id,"expressions":["rank(source_margin)"],
                         "neutralization":"SUBINDUSTRY","variant_count":100})
        with patch.object(campaigns,"SessionLocal",self.sessions),patch(
                "alpha_platform.research.discovery.resolve_template_scope",return_value=(discovered,{})),patch.object(campaigns,"execute_plan") as execute:
            created=campaigns.create_campaign(body)
            self.assertEqual(created["status"],"DRAFT")
            self.assertEqual(created["candidates"],[])
            self.assertEqual(created["brief"]["variant_analysis"]["generated"],50)
            execute.assert_not_called()
            with self.sessions() as db:
                saved=db.get(CatalogScope,scope_id)
                self.assertEqual(len(saved.fields),52)
                self.assertIn("close",{f["id"] for f in saved.fields})
                self.assertNotIn("replacement_1",{f["id"] for f in saved.fields})
            campaign_id=__import__("uuid").UUID(created["id"])
            campaigns.run_campaign(campaign_id)
            self.assertEqual(len(campaigns.get_campaign(campaign_id)["candidates"]),50)
            execute.assert_called_once_with(campaign_id)

    def test_four_qualifying_candidates_pause_and_feedback_is_persistent(self):
        with self.sessions() as db:
            scope=CatalogScope(scope_id="EQUITY/USA/1/TOP3000",
                settings={"instrumentType":"EQUITY","region":"USA","delay":1,"universe":"TOP3000"},
                datasets=[],fields=[],capabilities={},status="COMPLETE",synced_at=datetime.now(timezone.utc))
            db.add(scope)
            db.commit()
        brief={"scope_id":"EQUITY/USA/1/TOP3000","templates":[{"name":"test","expression":"rank(slot_field)"}],
               "settings":{"region":"USA","universe":"TOP3000","delay":1},"hypothesis":"Test hypothesis",
               "unknowns":["Mock data"],"version":1}
        planned=[{"expression":f"rank(close + {i})","template":"test","rationale":"Test substitution"} for i in range(8)]
        calls=[]
        def fake_simulate(db, session, payload, **kwargs):
            self.assertEqual(kwargs["confirmed_hash"],hash_alpha(payload))
            self.assertTrue(kwargs["check_submission"])
            config=get_or_create_config(db,payload,origin="llm_hypothesis")
            run=SimulationRun(config_id=config.config_id,status="COMPLETE",triggered_by="bandit_policy",alpha_id="mock")
            db.add(run)
            db.flush()
            db.add(AlphaPerformance(run_id=run.run_id,config_id=config.config_id,sharpe=2,fitness=2,turnover=.1,
                   ladder_metrics={"checks":[{"name":"LOW_SHARPE","result":"PASS"}]}))
            db.commit()
            calls.append(payload["regular"])
            return {"config":config,"run":run,"skipped":False}
        with patch.object(campaigns,"SessionLocal",self.sessions),patch.object(campaigns,"ResearchSession",MagicMock()),patch.object(
                campaigns,"research_plan",return_value=(brief,planned)),patch.object(campaigns,"simulate_confirmed",side_effect=fake_simulate):
            created=campaigns.create_campaign(CampaignInput(name="Test research",objective="Test a documented hypothesis",provider="openai",model="mock",concurrency=3,max_attempts=8))
            campaign_id=__import__("uuid").UUID(created["id"])
            campaigns.run_campaign(campaign_id)
            result=campaigns.get_campaign(campaign_id)
            self.assertEqual(result["status"],"AWAITING_VALIDATION")
            self.assertGreaterEqual(result["qualified"],4)
            self.assertLessEqual(result["qualified"],6)
            self.assertEqual(len(calls),result["qualified"])
            selected=next(r for r in result["candidates"] if r["status"]=="QUALIFIED")
            campaigns.validate_candidate(selected["id"],"rejected","The economic rationale needs more evidence.")
            self.assertEqual(campaigns.learning_summary()["observations"],len(calls))
            with self.assertRaises(ValueError):
                campaigns.validate_candidate(selected["id"],"accepted","")
            with self.sessions() as db:
                self.assertEqual(db.query(SimulationRun).count(),len(calls))
                self.assertEqual(db.query(RlDecision).count(),len(calls))

    def test_retry_preserves_project_and_blocks_saved_candidates(self):
        from types import SimpleNamespace
        with self.sessions() as db:
            db.add(CatalogScope(scope_id="scope",settings={},datasets=[],fields=[],capabilities={},status="COMPLETE"))
            db.commit()
        with patch.object(campaigns,"SessionLocal",self.sessions),patch.object(campaigns,"api_key",return_value="key"),patch.object(campaigns.WORKER,"submit") as submit:
            c=campaigns.create_campaign(CampaignInput(name="Retry project",objective="Research accounting profitability",provider="openai",model="mock"))
            campaign_id=__import__("uuid").UUID(c["id"])
            campaigns.set_status(campaign_id,"ERROR","No credits")
            result=campaigns.launch(c["id"],retry=True,connection=SimpleNamespace(provider="groq",model="openai/gpt-oss-120b"))
            self.assertEqual(result["id"],c["id"])
            self.assertEqual(result["provider"],"groq")
            self.assertEqual(result["status"],"QUEUED")
            self.assertIsNone(result["error"])
            submit.assert_called_once()
            campaigns.set_status(campaign_id,"ERROR","simulation interruption")
            with self.sessions() as db:
                config=get_or_create_config(db,__import__("alpha_platform.brain_client.vendor.ace_lib",fromlist=["generate_alpha"]).generate_alpha(regular="rank(close)"),origin="llm_hypothesis")
                db.add(ResearchCandidate(campaign_id=campaign_id,ordinal=1,expression="rank(close)",template="test",rationale="test",payload={},config_id=config.config_id))
                db.commit()
            with self.assertRaises(ValueError):
                campaigns.launch(c["id"],retry=True)
            self.assertEqual(submit.call_count,1)

    def test_manual_research_validates_and_runs_without_provider(self):
        with self.sessions() as db:
            db.add(CatalogScope(scope_id="EQUITY/USA/1/TOP3000",settings={"region":"USA","delay":1,"universe":"TOP3000"},
                datasets=[],fields=[{"id":"close","type":"MATRIX","description":"Documented daily closing price."}],
                capabilities={"neutralizations":["SUBINDUSTRY"],"operators":[{"name":"rank"}]},status="COMPLETE",synced_at=datetime.now(timezone.utc)))
            db.commit()
        body=CampaignInput(name="Manual profitability",objective="Test this user-specified hypothesis.",mode="manual",provider="manual",model="manual",max_attempts=2,
             manual_plan={"scope_id":"EQUITY/USA/1/TOP3000","expressions":["rank(close)","rank(-close)"],"neutralization":"SUBINDUSTRY"})
        with patch.object(campaigns,"SessionLocal",self.sessions),patch.object(campaigns,"research_plan",side_effect=AssertionError("LLM must not be called")),patch.object(campaigns,"execute_plan") as execute:
            created=campaigns.create_campaign(body)
            self.assertEqual(created["status"],"DRAFT")
            self.assertEqual(created["candidates"],[])
            campaign_id=__import__("uuid").UUID(created["id"])
            campaigns.run_campaign(campaign_id)
            result=campaigns.get_campaign(campaign_id)
            self.assertEqual(len(result["candidates"]),2)
            execute.assert_called_once_with(campaign_id)
            body.manual_plan.expressions=["rank(invented_field)"]
            with patch("alpha_platform.research.discovery.resolve_template_scope",side_effect=ValueError("Exact field not found")),self.assertRaises(ValueError):
                campaigns.create_campaign(body)

    def test_duplicate_launch_and_missing_credentials_are_blocked(self):
        with patch.object(campaigns,"SessionLocal",self.sessions),patch.object(campaigns,"api_key",return_value=""):
            c=campaigns.create_campaign(CampaignInput(name="Test draft",objective="Research accounting profitability",provider="openai",model="mock"))
            with self.assertRaises(ValueError):
                campaigns.launch(c["id"])
            self.assertEqual(campaigns.get_campaign(c["id"])["status"],"DRAFT")

    def test_rate_limit_pauses_entire_catalogue_queue(self):
        with self.sessions() as db:
            for name in ("scope-a","scope-b"):
                db.add(CatalogScope(scope_id=name,settings={},datasets=[],fields=[],capabilities={},status="QUEUED"))
            db.commit()
        retry=datetime.now(timezone.utc)+timedelta(seconds=60)
        with patch.object(catalog,"SessionLocal",self.sessions),patch.object(catalog,"sync_scope",side_effect=catalog.CatalogRateLimited(retry)) as sync,patch.object(catalog,"schedule_resume") as resume:
            catalog.sync_pending()
            sync.assert_called_once()
            resume.assert_called_once_with(retry)
            with self.sessions() as db:
                self.assertEqual([s.status for s in db.query(CatalogScope).all()],["PAUSED","PAUSED"])


if __name__=="__main__":
    unittest.main()

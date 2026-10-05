import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from alpha_platform.research.catalog import fetch_pages, CatalogRateLimited
from alpha_platform.research.evaluation import evaluate
from alpha_platform.research.planning import Plan, materialize
from alpha_platform.research.providers import generate_json


class ResearchTests(unittest.TestCase):
    def test_manual_group_zscore_validates_group_roles_and_live_operators(self):
        from types import SimpleNamespace
        from alpha_platform.research.campaigns import CampaignInput, manual_brief
        field = "tm_7_r1_r5_r10_rh_ah3_d1_r5_q3_p2"
        signal = f"ts_mean(group_zscore(ts_backfill(ts_rank({field},90),60),industry),5)"
        expression = f"trade_when(abs({signal}) > 0.70,{signal},-1)"
        scope = SimpleNamespace(status="COMPLETE", scope_id="test", synced_at=None,
            settings={"region":"USA","universe":"TOP3000","delay":1},
            fields=[{"id":field,"type":"MATRIX","description":"Documented test field for validation only."}],
            capabilities={"neutralizations":["SUBINDUSTRY"],"operators":[{"name":name} for name in
                ("ts_mean","group_zscore","ts_backfill","ts_rank","trade_when","abs")]})
        body = CampaignInput(name="Group test", objective="Test a user supplied grouped signal.",
            mode="manual", provider="manual", model="manual", manual_plan={
                "scope_id":"test","expressions":[expression],"neutralization":"SUBINDUSTRY"})
        self.assertEqual(len(manual_brief(body,scope)["manual_expressions"]),1)
        self.assertIn("lookback=60",manual_brief(body,scope)["manual_expressions"][0]["expression"])
        for invalid in (expression.replace(",industry)",",unknown_group)"),
                        expression.replace(",industry)",f",{field})"),
                        f"group_zscore(industry,industry)",
                        f"ts_backfill({field},0)", f"ts_backfill({field},lookback=1e999)"):
            body.manual_plan.expressions=[invalid]
            with self.subTest(expression=invalid), self.assertRaises(ValueError):
                manual_brief(body,scope)
        body.manual_plan.expressions=[expression]
        scope.capabilities["operators"]=[op for op in scope.capabilities["operators"] if op["name"]!="group_zscore"]
        with self.assertRaisesRegex(ValueError,"unavailable"):
            manual_brief(body,scope)

    def test_groq_context_budget_preserves_objective_and_evidence(self):
        import json
        from alpha_platform.research.providers import fit_groq_context,GROQ_INPUT_BYTES
        from alpha_platform.research.planning import GROQ_SYSTEM,Plan
        fields=[{"id":str(i),"type":"MATRIX","description":"Reported accounting profitability for each company."*3,"extra":"x"*1000} for i in range(300)]
        context={"objective":"Research profitability without changing my hypothesis.","fields":fields,
                 "output_schema":Plan.model_json_schema(),"required_field_ids":["299"]}
        result=fit_groq_context(GROQ_SYSTEM,context)
        self.assertLess(len(result["fields"]),300)
        self.assertEqual(result["objective"],context["objective"])
        self.assertIn("299",[f["id"] for f in result["fields"]])
        for f in result["fields"]:
            self.assertEqual(f["description"],fields[int(f["id"])]["description"])
        size=len(GROQ_SYSTEM.encode())+len(json.dumps(result,ensure_ascii=False,separators=(",",":")).encode())+64
        self.assertLessEqual(size,GROQ_INPUT_BYTES)
        self.assertEqual(len(context["fields"]),300)

    def test_oversized_groq_objective_is_blocked_before_api_call(self):
        with patch("alpha_platform.research.providers.api_key",return_value="key"),patch("alpha_platform.research.providers.requests.post") as post:
            with self.assertRaises(ValueError):
                generate_json("groq","openai/gpt-oss-120b","Return JSON",{"objective":"x"*10000})
        post.assert_not_called()

    def test_groq_chat_adapter_and_slash_model_identifier(self):
        response=MagicMock(status_code=200)
        response.json.return_value={"choices":[{"message":{"content":"{\"ok\":true}"}}],"usage":{"total_tokens":12}}
        with patch("alpha_platform.research.providers.api_key",return_value="key"),patch(
                "alpha_platform.research.providers.requests.post",return_value=response) as post:
            result,usage=generate_json("groq","openai/gpt-oss-120b","Return JSON",{})
        self.assertTrue(result["ok"])
        self.assertEqual(usage["total_tokens"],12)
        self.assertEqual(post.call_args.args[0],"https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["response_format"],{"type":"json_object"})

    def test_provider_timeout_and_bad_json_have_distinct_diagnostics(self):
        import requests
        with patch("alpha_platform.research.providers.api_key", return_value="SECRET KEY"):
            with patch("alpha_platform.research.providers.requests.post", side_effect=requests.exceptions.ReadTimeout("SECRET KEY")):
                with self.assertRaises(ValueError) as error:
                    generate_json("gemini", "model", "system", {})
                self.assertIn("180 seconds", str(error.exception))
                self.assertNotIn("SECRET KEY", str(error.exception))
            response = MagicMock(status_code=200)
            response.json.side_effect = requests.exceptions.JSONDecodeError("invalid", "not json", 0)
            with patch("alpha_platform.research.providers.requests.post", return_value=response):
                with self.assertRaises(ValueError) as error:
                    generate_json("gemini", "model", "system", {})
                self.assertIn("invalid JSON", str(error.exception))
                self.assertNotIn("connection failed", str(error.exception))

    def test_openai_json_mode_input_explicitly_requests_json(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"output": [{"content": [{"type": "output_text", "text": "{}"}]}]}
        with patch("alpha_platform.research.providers.api_key", return_value="SECRET KEY"), patch(
                "alpha_platform.research.providers.requests.post", return_value=response) as post:
            generate_json("openai", "gpt-5.4-mini", "Return a structured answer", {"task": "select fields"})
        self.assertIn("JSON", post.call_args.kwargs["json"]["input"])

    def test_openai_error_preserves_diagnostic_and_redacts_credentials(self):
        response = MagicMock(status_code=400)
        response.json.return_value = {"error": {"message": "Invalid text.format for SECRET KEY sk-examplecredential researcher@example.com"}}
        with patch("alpha_platform.research.providers.api_key", return_value="SECRET KEY"), patch(
                "alpha_platform.research.providers.requests.post", return_value=response):
            with self.assertRaises(ValueError) as error:
                generate_json("openai", "gpt-5.4-mini", "system", {})
        message = str(error.exception)
        self.assertIn("Invalid text.format", message)
        self.assertNotIn("SECRET KEY", message)
        self.assertNotIn("sk-examplecredential", message)
        self.assertNotIn("researcher@example.com", message)

    def test_named_dataset_download_targets_only_that_dataset(self):
        from alpha_platform.research.catalog import fetch_scope_fields
        options = {"min_instrument": 50, "min_date": 80, "max_fields": 200,
                   "random_sample": False, "seed": 1, "dataset_ids": ["fundamental28"]}
        rows = [{"id": str(i), "dataset": {"id": "fundamental28"}, "coverage": .5, "dateCoverage": 1} for i in range(200)]
        pages = [{"count": 929, "results": rows[i:i+50]} for i in range(0, 200, 50)]
        with TemporaryDirectory() as directory:
            with patch("alpha_platform.research.catalog.read_json", side_effect=pages) as read:
                selected = fetch_scope_fields(None, {}, Path(directory)/"fields.jsonl", options)
        self.assertEqual(len(selected), 200)
        self.assertEqual(read.call_count, 4)
        for call in read.call_args_list:
            self.assertEqual(call.args[2]["dataset.id"], "fundamental28")

    def test_dataset_query_rejects_unrelated_fields(self):
        page = {"count": 1, "results": [{"id": "wrong", "dataset": {"id": "other"}}]}
        with patch("alpha_platform.research.catalog.read_json", return_value=page):
            with self.assertRaises(ValueError):
                fetch_pages(None, "/data-fields", {"dataset.id": "fundamental28"})

    def test_dataset_name_filter_resolves_ids_and_restricts_fields(self):
        from alpha_platform.research.catalog import matching_datasets, select_fields
        datasets = [{"id": "fundamental28", "name": "Global Fundamental Data"},
                    {"id": "other", "name": "Price Data"}]
        matched = matching_datasets(datasets, "global fundamental")
        self.assertEqual([d["id"] for d in matched], ["fundamental28"])
        with self.assertRaises(ValueError):
            matching_datasets(datasets, "unavailable dataset")
        options = {"min_instrument": 80, "min_date": 80, "max_fields": 500,
                   "random_sample": False, "seed": 1, "dataset_ids": ["fundamental28"]}
        rows = [{"id": "good", "dataset": {"id": "fundamental28"}, "coverage": .9, "dateCoverage": 1},
                {"id": "wrong", "dataset": {"id": "other"}, "coverage": 1, "dateCoverage": 1},
                {"id": "low", "dataset": "fundamental28", "coverage": .5, "dateCoverage": 1}]
        self.assertEqual([r["id"] for r in select_fields(rows, options)], ["good"])

    def test_coverage_filter_and_seeded_sample(self):
        from alpha_platform.research.catalog import select_fields
        rows = [{"id": str(i), "coverage": .9, "dateCoverage": .85} for i in range(20)]
        rows += [{"id": "missing"}, {"id": "low", "coverage": .7, "dateCoverage": 1}]
        options = {"min_instrument": 80, "min_date": 80, "max_fields": 5, "random_sample": True, "seed": 42}
        sample = select_fields(rows, options)
        self.assertEqual(len(sample), 5)
        self.assertEqual(sample, select_fields(list(reversed(rows)), options))
        self.assertFalse({"missing", "low"} & {r["id"] for r in sample})

    def test_standard_selection_stops_at_field_limit(self):
        options = {"min_instrument": 80, "min_date": 80, "max_fields": 1, "random_sample": False, "seed": 1}
        page = {"count": 10000, "results": [{"id": "a", "coverage": .9, "dateCoverage": .9}]}
        with patch("alpha_platform.research.catalog.read_json", return_value=page) as read:
            self.assertEqual(len(fetch_pages(None, "/data-fields", {}, selection=options)), 1)
            read.assert_called_once()

    def test_random_sampling_scans_remaining_pages(self):
        options = {"min_instrument": 80, "min_date": 80, "max_fields": 1, "random_sample": True, "seed": 1}
        pages = [{"count": 2, "results": [{"id": x, "coverage": .9, "dateCoverage": .9}]} for x in ("a", "b")]
        with patch("alpha_platform.research.catalog.read_json", side_effect=pages) as read:
            self.assertEqual(len(fetch_pages(None, "/data-fields", {}, selection=options)), 1)
            self.assertEqual(read.call_count, 2)

    def test_pagination_requires_every_result(self):
        pages = [{"count": 3, "results": [{"id": "a"}, {"id": "b"}]},
                 {"count": 3, "results": [{"id": "c"}]}]
        with patch("alpha_platform.research.catalog.read_json", side_effect=pages) as read:
            self.assertEqual(len(fetch_pages(None, "/data-fields", {})), 3)
            self.assertEqual(read.call_args.args[2]["offset"], 2)
        with patch("alpha_platform.research.catalog.read_json", side_effect=[pages[0], {"count":3,"results":[]} ]):
            with self.assertRaises(ValueError):
                fetch_pages(None, "/data-fields", {})

    def test_duplicate_page_is_not_a_complete_catalogue(self):
        page = {"count": 2, "results": [{"id":"a"}]}
        with patch("alpha_platform.research.catalog.read_json", side_effect=[page,page]):
            with self.assertRaises(ValueError):
                fetch_pages(None, "/data-fields", {})

    def test_rate_limited_download_resumes_after_last_saved_page(self):
        with TemporaryDirectory() as directory:
            checkpoint=Path(directory)/"fields.jsonl"
            first={"count":3,"results":[{"id":"a"},{"id":"b"}]}
            with patch("alpha_platform.research.catalog.read_json",side_effect=[first,CatalogRateLimited(datetime.now(timezone.utc)+timedelta(seconds=60))]):
                with self.assertRaises(CatalogRateLimited):
                    fetch_pages(None,"/data-fields",{},checkpoint)
            with patch("alpha_platform.research.catalog.read_json",return_value={"count":3,"results":[{"id":"c"}]}) as read:
                rows=fetch_pages(None,"/data-fields",{},checkpoint)
                self.assertEqual(read.call_args.args[2]["offset"],2)
                self.assertEqual([r["id"] for r in rows],["a","b","c"])

    def test_missing_checks_and_metrics_cannot_qualify(self):
        metrics={"sharpe":2,"fitness":2,"turnover":.1}
        self.assertFalse(evaluate(metrics, [], {})["qualifies"])
        checks=[{"name":"LOW_SHARPE","result":"PASS"}]
        self.assertTrue(evaluate(metrics, checks, {})["qualifies"])
        self.assertFalse(evaluate(metrics, checks, {"required_checks":["PROD_CORRELATION"]})["qualifies"])
        self.assertFalse(evaluate(metrics, checks, {"max_self_correlation":.7})["qualifies"])
        checks[0]["result"]="PENDING"
        self.assertFalse(evaluate(metrics, checks, {})["qualifies"])

    def plan(self, evidence="Reported accounting profitability"):
        return Plan.model_validate({"hypothesis":"Persistent accounting profitability may predict relative stock returns.",
            "expected_behaviour":"Higher profitability should rank higher under this untested hypothesis.",
            "risks":["Industry effects"],"unknowns":["Publication timing unavailable"],
            "settings_rationale":"Use the verified settings for this selected catalogue scope.",
            "decay":15,"truncation":.08,"neutralization":"SUBINDUSTRY",
            "templates":[{"name":"Profitability","expression":"rank(ts_mean(slot_profit, window))",
                "hypothesis":"Persistent profitability may indicate durable financial quality.",
                "explanation":"Average the documented profitability signal before cross-sectional ranking.",
                "slots":[{"name":"slot_profit","role":"Comparable accounting profitability measurement",
                    "original_field":"profit_a","choices":[{"field_id":"profit_b","evidence":evidence,
                        "rationale":"Uses another documented profitability measurement for the same role."}]}],"windows":[21,63]}]})

    def test_substitution_requires_real_field_evidence(self):
        fields=[{"id":"profit_b","type":"MATRIX","description":"Reported accounting profitability for each company."}]
        rows=materialize(self.plan(),fields,"rank(ts_mean(profit_a, 5))",10)
        self.assertEqual(rows[0]["expression"],"rank(ts_mean(profit_b, 21))")
        with self.assertRaises(ValueError):
            materialize(self.plan("A fabricated description"),fields,None,10)
        with self.assertRaises(ValueError):
            materialize(self.plan(),fields,"rank(ts_delta(profit_a, 5))",10)

    def test_provider_error_does_not_leak_key_or_response(self):
        response=MagicMock(status_code=401,text="SECRET RESPONSE")
        with patch("alpha_platform.research.providers.api_key",return_value="SECRET KEY"),patch(
                "alpha_platform.research.providers.requests.post",return_value=response):
            with self.assertRaises(ValueError) as error:
                generate_json("openai","model","system",{})
        self.assertNotIn("SECRET",str(error.exception))


if __name__ == "__main__":
    unittest.main()

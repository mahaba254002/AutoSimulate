import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from alpha_platform.research.discovery import resolve_template_scope


class DiscoveryTests(unittest.TestCase):
    def scope(self):
        return SimpleNamespace(scope_id="EQUITY/USA/1/TOP3000",status="COMPLETE",synced_at=None,
            settings={"instrumentType":"EQUITY","region":"USA","universe":"TOP3000","delay":1},
            fields=[],datasets=[],capabilities={})

    def test_exact_missing_field_resolved_without_prefix_inference(self):
        source={"id":"tm7_actual_field","type":"MATRIX","dataset":{"id":"verified_model"},
                "description":"Documented volatility risk prediction","coverage":.5,"dateCoverage":1}
        dataset={"id":"verified_model","name":"Verified Model Dataset","category":{"id":"model"}}
        calls=[]
        def pages(session,path,params,checkpoint):
            calls.append((path,params))
            if path=="/data-sets":return [dataset]
            if params.get("search"):return [source,{**source,"id":"similar_prefix_field"}]
            return [source,{**source,"id":"tm3_replacement"}]
        with patch("alpha_platform.research.discovery.ResearchSession",return_value=MagicMock()),patch(
                "alpha_platform.research.discovery.fetch_pages",side_effect=pages),patch(
                "alpha_platform.research.discovery.read_json",side_effect=lambda s,p,q:{"count":10000,"results":pages(s,p,q,None)}) as search:
            resolved,provenance=resolve_template_scope(self.scope(),"group_zscore(tm7_actual_field,industry)")
        search.assert_called_once()
        self.assertEqual(provenance["resolved_missing_fields"],["tm7_actual_field"])
        self.assertEqual(resolved.datasets,[dataset])
        self.assertEqual(calls[0][1]["search"],"tm7_actual_field")
        self.assertEqual(calls[-1][1]["dataset.id"],"verified_model")
        for _,params in calls:
            self.assertEqual(params["region"],"USA")
            self.assertEqual(params["universe"],"TOP3000")
            self.assertEqual(params["delay"],1)
        self.assertNotIn("industry",provenance["resolved_missing_fields"])

    def test_prefix_match_is_not_an_exact_source_field(self):
        with patch("alpha_platform.research.discovery.ResearchSession",return_value=MagicMock()),patch(
                "alpha_platform.research.discovery.read_json",return_value={"results":[{"id":"tm7_other"}]}):
            with self.assertRaisesRegex(ValueError,"exact field"):
                resolve_template_scope(self.scope(),"rank(tm7_missing)")

    def test_plain_manual_lookup_does_not_scan_replacement_dataset(self):
        source={"id":"missing_field","type":"MATRIX","dataset":{"id":"verified"},
                "description":"Documented operating margin field"}
        dataset={"id":"verified","category":{"id":"fundamental"}}
        with patch("alpha_platform.research.discovery.ResearchSession",return_value=MagicMock()),patch(
                "alpha_platform.research.discovery.fetch_pages",return_value=[dataset]) as pages,patch(
                "alpha_platform.research.discovery.read_json",return_value={"results":[source]}) as search:
            resolved,_=resolve_template_scope(self.scope(),"rank(missing_field)",include_replacements=False)
        self.assertEqual(pages.call_count,1)
        search.assert_called_once()
        self.assertEqual(resolved.fields,[source])
        self.assertFalse(any("dataset.id" in call.args[2] for call in pages.call_args_list))

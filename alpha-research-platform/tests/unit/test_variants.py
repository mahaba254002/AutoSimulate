import unittest
from types import SimpleNamespace

from alpha_platform.research.variants import field_variants
from alpha_platform.research.campaigns import CampaignInput, manual_brief
from alpha_platform.structure.parser import Identifier, parse
from alpha_platform.generation.gp.trees import walk


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.datasets = [{"id":category, "category":{"id":category,"name":category}}
                         for category in ("fundamental", "model", "analyst")]
        self.fields = {}
        for category, description in (("fundamental","Operating profitability margin return on invested capital"),
                                      ("model","Forecast volatility risk estimates for equity returns"),
                                      ("analyst","Consensus earnings forecast revisions expected growth")):
            for index in range(4):
                name = category + str(index)
                self.fields[name] = {"id":name,"dataset":{"id":category},"type":"MATRIX",
                                    "description":description,"units":"ratio","frequency":"daily","coverage":.8,"dateCoverage":1}

    def test_mixed_categories_consistent_replacements_and_structure(self):
        expression = "rank(fundamental0) + ts_mean(model0,21) + analyst0 / fundamental0"
        rows, analysis = field_variants(expression,self.fields,self.datasets,100,42)
        self.assertEqual(len(rows),27)
        self.assertEqual(len({r["expression"] for r in rows}),27)
        self.assertEqual(rows,field_variants(expression,self.fields,self.datasets,100,42)[0])
        self.assertEqual(len(analysis["slots"]),3)
        original_tree = parse(expression)
        for row in rows:
            mapping = row["substitutions"]
            for original, replacement in mapping.items():
                self.assertNotEqual(original,replacement)
                self.assertEqual(self.fields[original]["dataset"],self.fields[replacement]["dataset"])
            tree = parse(row["expression"])
            for _, node in walk(tree):
                if isinstance(node,Identifier):
                    node.name = next((key for key,value in mapping.items() if value==node.name),node.name)
            self.assertEqual(tree,original_tree)

    def test_metadata_constraints_no_invented_choices(self):
        self.fields["fundamental1"]["units"]="currency"
        self.fields["fundamental2"]["frequency"]="quarterly"
        self.fields["fundamental3"]["description"]="Unrelated revenue expense accounting debt payments"
        with self.assertRaisesRegex(ValueError,"No similar replacement"):
            field_variants("rank(fundamental0)",self.fields,self.datasets,100,42)
        with self.assertRaisesRegex(ValueError,"category"):
            field_variants("rank(model0)",self.fields,[],100,42)

    def test_fifty_choices_ranked_by_coverage_without_random_selection(self):
        from copy import deepcopy
        for index in range(4,75):
            name = f"fundamental{index}"
            self.fields[name] = {**deepcopy(self.fields["fundamental0"]), "id":name,
                                 "coverage":.5 if index < 40 else 1}
        self.fields["fundamental1"]["coverage"] = None
        self.fields["fundamental2"]["coverage"] = 0
        rows, analysis = field_variants("rank(fundamental0)",self.fields,self.datasets,100,42)
        choices = analysis["slots"][0]["choices"]
        self.assertEqual(len(choices),50)
        self.assertEqual(len(rows),50)
        coverage = [choice["instrument_coverage"] for choice in choices]
        self.assertEqual(coverage,sorted(coverage,reverse=True))
        self.assertEqual(coverage[0],100)
        self.assertFalse({"fundamental1","fundamental2"} & {choice["field_id"] for choice in choices})
        self.assertEqual(rows,field_variants("rank(fundamental0)",self.fields,self.datasets,100,999)[0])

    def test_manual_brief_variants_are_saved_for_review_without_simulation(self):
        scope = SimpleNamespace(status="COMPLETE",scope_id="test",synced_at=None,
            settings={"region":"USA","universe":"TOP3000","delay":1},
            fields=list(self.fields.values()),datasets=self.datasets,
            capabilities={"neutralizations":["SUBINDUSTRY"],"operators":[{"name":"rank"}]})
        body = CampaignInput(name="Mixed template",objective="Test documented cross category substitutions.",
            mode="manual",provider="manual",model="manual",max_attempts=100,
            manual_plan={"scope_id":"test","expressions":["rank(fundamental0 + model0 + analyst0)"],
                         "neutralization":"SUBINDUSTRY","variant_count":100})
        brief = manual_brief(body,scope)
        self.assertEqual(len(brief["manual_expressions"]),27)
        self.assertEqual(len(brief["templates"]),1)
        self.assertEqual(brief["variant_analysis"]["requested"],100)
        self.assertEqual(len(brief["field_evidence"]),12)
        with self.assertRaises(ValueError):
            CampaignInput(**{**body.model_dump(),"max_attempts":10})

import unittest
from alpha_platform.research.redevelopment import redevelopment_plan, parent_comparison
from alpha_platform.structure.parser import parse, FunctionCall, KeywordArg
from alpha_platform.generation.gp.trees import walk, validate_tree, SCALAR_OPERATORS


class RedevelopmentTests(unittest.TestCase):
    fields = [{'id':'forecast','description':'Documented model forecast for future returns.', 'type':'MATRIX'},
              {'id':'value','description':'Documented valuation measurement relative to peers.', 'type':'MATRIX'}]

    def test_preserves_groups_and_separates_slope_from_residual(self):
        rows, analysis = redevelopment_plan('group_neutralize(rank(forecast)+rank(value),subindustry)',
                                             self.fields,[40,20,60],100)
        self.assertEqual([r['phase'] for r in rows],sorted(r['phase'] for r in rows))
        self.assertEqual(len({r['expression'] for r in rows}),len(rows))
        self.assertEqual(analysis['field_evidence'],self.fields)
        self.assertTrue(any(r['template']=='Joint high ranks' for r in rows))
        for row in rows:
            tree=parse(row['expression'])
            validate_tree(tree,fields=['forecast','value'],operators=SCALAR_OPERATORS|{'group_neutralize','ts_step'},groups={'subindustry'})
            self.assertEqual(tree.operator,'group_neutralize')
            self.assertEqual(tree.args[1].name,'subindustry')
            regressions=[n for _,n in walk(tree) if isinstance(n,FunctionCall) and n.operator=='ts_regression']
            for n in regressions:
                ret=next(a.value.value for a in n.args if isinstance(a,KeywordArg) and a.name=='rettype')
                self.assertEqual(ret,2 if row['template'].startswith('Slope') else 0)
                self.assertIn('rettype='+str(int(ret)),row['rationale'])

    def test_comparison_never_invents_missing_metrics_or_correlation(self):
        result=parent_comparison({'sharpe':3,'fitness':None,'turnover':.2},
                                 {'run_id':'saved','metrics':{'sharpe':2,'fitness':1,'turnover':.3}})
        self.assertEqual(result['delta']['sharpe'],1)
        self.assertIsNone(result['delta']['fitness'])
        self.assertIsNone(result['parent_correlation'])

    def test_invalid_regression_options_are_rejected(self):
        for expression in ('ts_regression(forecast,ts_step(2),40,rettype=0)',
                           'ts_regression(forecast,ts_step(1),40,rettype=10)'):
            with self.assertRaises(ValueError):
                validate_tree(parse(expression),fields=['forecast'],operators=SCALAR_OPERATORS|{'ts_step'})

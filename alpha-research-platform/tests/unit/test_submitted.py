from datetime import datetime,timezone
import unittest
from unittest.mock import MagicMock,patch

from alpha_platform.research import submitted


class SubmittedTests(unittest.TestCase):
    def test_scans_unsubmitted_records_but_rejects_duplicate_and_changed_pages(self):
        good={'id':'alpha','stage':'OS','dateSubmitted':'2025-01-01T00:00:00Z'}
        self.assertEqual(submitted.validate_page({'count':1,'results':[good]},None,set()),{'alpha'})
        draft={'id':'draft','stage':'OS','status':'UNSUBMITTED'}
        self.assertEqual(submitted.validate_page({'count':1,'results':[draft]},None,set()),{'draft'})
        self.assertFalse(submitted.is_submitted(draft))
        self.assertTrue(submitted.is_submitted(good))
        for page,expected,seen in [({'count':1,'results':[good]},1,{'alpha'}),
                                   ({'count':2,'results':[good]},1,set()),
                                   ({'count':2,'results':[]},2,set()),
                                   ({'count':True,'results':[]},None,set())]:
            with self.assertRaises(ValueError):submitted.validate_page(page,expected,seen)

    def test_does_not_infer_scores_or_missing_metrics(self):
        payload={'id':'alpha','stage':'OS','regular':{'code':'rank(close)'},'is':{'sharpe':4},'os':None}
        row=submitted.alpha_record(payload,'owner','import')
        self.assertIsNone(row['score'])
        self.assertEqual(row['expression'],'rank(close)')
        self.assertEqual(row['metrics']['os'],{})
        self.assertIsNone(submitted.number(True))
        self.assertIsNone(submitted.number(float('nan')))
        self.assertIsNone(submitted.number('100'))
        self.assertEqual(submitted.number(0),0)

    def test_get_only_and_auth_failure_does_not_echo_response(self):
        response=MagicMock(status_code=401)
        response.json.return_value={'secret':'PRIVATE'}
        with patch.object(submitted.catalog,'catalog_request',return_value=response) as request:
            with self.assertRaises(ValueError) as failure:submitted.read(None,'/users/self/alphas')
            self.assertEqual(request.call_args.args[1],'get')
            self.assertNotIn('PRIVATE',str(failure.exception))

    def test_timezone_aware_submission_date_is_preserved(self):
        row=submitted.alpha_record({'id':'alpha','dateSubmitted':'2025-01-01T10:00:00Z'},'owner','import')
        self.assertEqual(row['date_submitted'],datetime(2025,1,1,10,tzinfo=timezone.utc))

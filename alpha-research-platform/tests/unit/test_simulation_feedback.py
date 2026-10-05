import json
from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from alpha_platform.brain_client.ace_lib_adapter import _BoundedSession, MonitoringCancelled
from alpha_platform.research.evaluation import evaluate, finite_number


class FeedbackTests(unittest.TestCase):
    def test_explicit_concurrency_rejection_preserves_daily_quota(self):
        from alpha_platform.pipeline.safety import capture_limits
        db, run = MagicMock(), MagicMock()
        ledger = MagicMock(last_known_remaining=4900)
        db.get.return_value = ledger
        capture_limits(db, run, {}, 429, rejection_code="CONCURRENT_SIMULATION_LIMIT_EXCEEDED")
        self.assertEqual(ledger.last_known_remaining,4900)
        capture_limits(db, run, {}, 429)
        self.assertEqual(ledger.last_known_remaining,0)

    def test_missing_correlations_are_json_safe_and_never_qualify_when_required(self):
        result=evaluate({"sharpe":2,"fitness":2,"turnover":.1,"self_correlation":Decimal("NaN")},
                        [{"name":"CHECK","result":"PASS","value":float("nan")}],{"max_self_correlation":.7})
        self.assertFalse(result["qualifies"])
        self.assertIn("self_correlation",result["unverified"])
        self.assertIsNone(result["metrics"]["self_correlation"])
        json.dumps(result,allow_nan=False)
        self.assertIsNone(finite_number(float("inf")))

    def test_stop_tracking_prevents_another_request(self):
        session=MagicMock()
        bounded=_BoundedSession(session,cancel_requested=lambda:True)
        with self.assertRaises(MonitoringCancelled):bounded.get("https://api.worldquantbrain.com/simulations/test")
        session.get.assert_not_called()

    def test_progress_response_is_forwarded_and_platform_error_kept(self):
        session=MagicMock()
        response=MagicMock(status_code=200,headers={"Retry-After":"5"})
        response.json.return_value={"progress":.35}
        session.get.return_value=response
        observe=MagicMock()
        bounded=_BoundedSession(session,on_response=observe)
        bounded.get("https://api.worldquantbrain.com/simulations/test")
        observe.assert_called_once()
        response.json.return_value={"status":"ERROR","message":"Invalid operator argument"}
        with self.assertRaisesRegex(ValueError,"Invalid operator argument"):
            bounded.get("https://api.worldquantbrain.com/simulations/test")

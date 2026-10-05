from copy import deepcopy
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import MagicMock, patch

from alpha_platform.brain_client.ace_lib_adapter import _BoundedSession, generate_and_simulate, simulate_confirmed
from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.dedupe.hashing import hash_alpha
from alpha_platform.pipeline import orchestrator
from alpha_platform.pipeline.safety import SubmissionBlocked, quota_snapshot, reserve_submission


class SafetyTests(unittest.TestCase):
    def test_ace_session_check_reuses_bounded_session_without_relogin(self):
        session = MagicMock()
        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {"token": {"expiry": 3000}}
        session.get.return_value = response
        bounded = _BoundedSession(session)
        with patch.object(ace, "start_session", side_effect=AssertionError("Interactive auth forbidden")):
            self.assertIs(ace.check_session_and_relogin(bounded), bounded)
        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.kwargs["timeout"], (10, 60))

    def test_expiring_session_does_not_trigger_interactive_auth(self):
        session = MagicMock()
        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {"token": {"expiry": 100}}
        session.get.return_value = response
        with patch.object(ace, "start_session", side_effect=AssertionError("Interactive auth forbidden")):
            with self.assertRaises(ValueError):
                ace.check_session_and_relogin(_BoundedSession(session))

    def test_adapter_requires_confirmation_before_db_or_network(self):
        db, session = MagicMock(), MagicMock()
        with self.assertRaises(SubmissionBlocked):
            generate_and_simulate(db, session, regular="close")
        db.execute.assert_not_called()
        session.post.assert_not_called()

    def test_modified_payload_is_not_approved(self):
        payload = ace.generate_alpha(regular="close")
        approval = hash_alpha(payload)
        payload["settings"]["decay"] = 123
        db = MagicMock()
        with self.assertRaises(SubmissionBlocked):
            reserve_submission(db, payload, origin="manual", triggered_by="manual", confirmed_hash=approval)
        db.execute.assert_not_called()

    def test_force_does_not_bypass_gate(self):
        with self.assertRaises(ValueError):
            generate_and_simulate(MagicMock(), MagicMock(), regular="close", force_resubmit=True)

    def test_unknown_post_outcome_keeps_run(self):
        db, session = MagicMock(), MagicMock()
        run = MagicMock()
        payload = ace.generate_alpha(regular="close")
        with patch("alpha_platform.pipeline.safety.reserve_submission", return_value=(MagicMock(), run)), \
             patch.object(ace, "start_simulation", side_effect=TimeoutError), \
             patch("alpha_platform.brain_client.ace_lib_adapter.logger"):
            with self.assertRaises(TimeoutError):
                simulate_confirmed(db, session, payload, confirmed_hash=hash_alpha(payload))
        self.assertEqual(run.status, "TIMEOUT")
        db.commit.assert_called_once()

    def test_quota_uses_largest_local_usage_and_smallest_remaining(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 6
        db.get.return_value = MagicMock(simulations_used=8, last_known_remaining=2)
        with patch("alpha_platform.pipeline.safety.get_settings", return_value=MagicMock(daily_simulation_budget=10)):
            self.assertEqual(quota_snapshot(db)["remaining"], 2)
        db.get.return_value.last_known_remaining = 0
        self.assertEqual(quota_snapshot(db)["remaining"], 0)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        orchestrator.REVIEWS.clear()
        orchestrator.JOBS.clear()
        orchestrator.REVIEWS["token"] = {"rows": [{"expression": "close"}], "expires": time.time()+60,
                                          "day": str(datetime.now(timezone.utc).date())}

    def test_wrong_phrase_does_not_start_worker(self):
        with patch.object(orchestrator.WORKER, "submit") as submit:
            with self.assertRaises(SubmissionBlocked):
                orchestrator.confirm_review("token", "yes")
            submit.assert_not_called()

    def test_confirmation_is_one_use(self):
        with patch.object(orchestrator.WORKER, "submit") as submit:
            job = orchestrator.confirm_review("token", "RUN 1")
            self.assertEqual(job["status"], "QUEUED")
            with self.assertRaises(SubmissionBlocked):
                orchestrator.confirm_review("token", "RUN 1")
            submit.assert_called_once()

    def test_expired_or_old_day_review_rejected(self):
        for key, value in (("expires", 0), ("day", "2000-01-01")):
            with self.subTest(key=key):
                self.setUp()
                orchestrator.REVIEWS["token"][key] = value
                with patch.object(orchestrator.WORKER, "submit") as submit:
                    with self.assertRaises(SubmissionBlocked):
                        orchestrator.confirm_review("token", "RUN 1")
                    submit.assert_not_called()

    def tearDown(self):
        orchestrator.REVIEWS.clear()
        orchestrator.JOBS.clear()


class APITests(unittest.TestCase):
    def test_csrf_and_offline_bootstrap(self):
        from fastapi.testclient import TestClient
        from alpha_platform.api.main import app
        with patch("alpha_platform.api.main.startup_recovery"), TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/api/bootstrap").status_code, 200)
            self.assertEqual(client.post("/api/confirm", json={"token":"no", "phrase":"RUN 1"}).status_code, 403)
            self.assertEqual(client.get("/", headers={"host":"untrusted.example"}).status_code, 400)

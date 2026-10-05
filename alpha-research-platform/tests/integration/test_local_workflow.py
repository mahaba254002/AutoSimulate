"""Postgres integration in disposable schemas; all simulation calls are mocked.

Run explicitly: python -m unittest discover -s tests/integration -p test_local_workflow.py -v
Requires CREATE SCHEMA permission. Never changes the research tables in public.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import sessionmaker

from alpha_platform.brain_client.ace_lib_adapter import simulate_confirmed
from alpha_platform.brain_client.vendor import ace_lib as ace
from alpha_platform.config.settings import get_settings
from alpha_platform.db.models import Base, GpPopulation, SimulationRun
from alpha_platform.dedupe.hashing import hash_alpha
from alpha_platform.pipeline import orchestrator
from alpha_platform.pipeline.safety import SubmissionBlocked, quota_snapshot, reserve_submission


class PostgresWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.schema = "alpha_test_" + uuid.uuid4().hex
        self.admin = create_engine(get_settings().database_url)
        with self.admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_engine(get_settings().database_url,
                                   connect_args={"options": f"-csearch_path={self.schema}"})
        with self.engine.begin() as conn:
            conn.execute(text("""CREATE FUNCTION immutable_date_utc(ts TIMESTAMPTZ) RETURNS DATE AS $$
                                 SELECT (ts AT TIME ZONE 'UTC')::date $$ LANGUAGE SQL IMMUTABLE"""))
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, autoflush=False)

    def tearDown(self):
        self.engine.dispose()
        assert self.schema.startswith("alpha_test_") and len(self.schema) == 43
        with self.admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()

    def reserve(self, expression):
        payload = ace.generate_alpha(regular=expression)
        with self.sessions() as db:
            _, run = reserve_submission(db, payload, origin="manual", triggered_by="manual",
                                        confirmed_hash=hash_alpha(payload))
            return run is not None

    def test_concurrent_reservations_cannot_exceed_budget(self):
        barrier = threading.Barrier(2)
        def attempt(expression):
            barrier.wait()
            try:
                return self.reserve(expression)
            except SubmissionBlocked:
                return False
        with patch("alpha_platform.pipeline.safety.get_settings", return_value=MagicMock(daily_simulation_budget=1)):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, ["rank(close)", "rank(open)"]))
            self.assertEqual(sum(results), 1)
            with self.sessions() as db:
                self.assertEqual(quota_snapshot(db)["remaining"], 0)
                self.assertEqual(db.query(func.count(SimulationRun.run_id)).scalar(), 1)

    def test_parameter_sweep_and_exact_duplicate_are_skipped(self):
        self.assertTrue(self.reserve("ts_mean(close, 5)"))
        self.assertFalse(self.reserve("ts_mean(close, 5)"))
        self.assertFalse(self.reserve("ts_mean(close, 21)"))
        with self.sessions() as db:
            self.assertEqual(quota_snapshot(db)["used"], 1)

    def test_complete_result_updates_reserved_run(self):
        payload = ace.generate_alpha(regular="rank(close)")
        response = MagicMock(status_code=201, headers={"Location": "https://example.invalid/mock", "X-Ratelimit-Remaining": "10"})
        with self.sessions() as db, patch.object(ace, "start_simulation", return_value=response) as post, \
             patch.object(ace, "simulation_progress", return_value={"completed": True, "result": {"id": "mock-alpha"}}), \
             patch.object(ace, "get_specified_alpha_stats", return_value={"alpha_id": "mock-alpha", "is_stats": None}):
            result = simulate_confirmed(db, MagicMock(), payload, confirmed_hash=hash_alpha(payload))
            self.assertEqual(result["run"].status, "COMPLETE")
            self.assertEqual(db.query(func.count(SimulationRun.run_id)).scalar(), 1)
            self.assertEqual(quota_snapshot(db)["used"], 1)
            post.assert_called_once()

    def test_rate_limit_stops_further_submissions(self):
        payload = ace.generate_alpha(regular="rank(close)")
        response = MagicMock(status_code=429, headers={"X-Ratelimit-Remaining": "4000", "Retry-After": "60"})
        with self.sessions() as db, patch.object(ace, "start_simulation", return_value=response) as post:
            result = simulate_confirmed(db, MagicMock(), payload, confirmed_hash=hash_alpha(payload))
            self.assertEqual(result["run"].status, "ERROR")
            post.assert_called_once()
        with self.assertRaises(SubmissionBlocked):
            self.reserve("rank(open)")

    def test_generate_and_review_persist_without_submission(self):
        with patch.object(orchestrator, "SessionLocal", self.sessions), \
             patch.object(ace, "start_simulation", side_effect=AssertionError("Unexpected submission")) as post:
            batch = orchestrator.generate_batch(["rank(close)", "rank(-returns)"],
                                                ["close", "open", "returns"], 10, 42, {})
            review = orchestrator.review_batch(batch["id"], [r["id"] for r in batch["rows"][:2]])
            self.assertEqual(review["phrase"], "RUN 2")
            orchestrator.BATCHES.clear()
            restored = orchestrator.get_batch(batch["id"])
            self.assertEqual({hash_alpha(row["payload"]) for row in restored["rows"]},
                             {hash_alpha(row["payload"]) for row in batch["rows"]})
            with self.sessions() as db:
                self.assertEqual(db.query(func.count(GpPopulation.population_id)).scalar(), 10)
                self.assertEqual(db.query(func.count(SimulationRun.run_id)).scalar(), 0)
            post.assert_not_called()
        orchestrator.REVIEWS.clear()
        orchestrator.BATCHES.clear()

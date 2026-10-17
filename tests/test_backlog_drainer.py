from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from career_agent.analysis_budget import AnalysisBudgetLimits, BacklogDrainPolicy
from career_agent.analysis_cache import analysis_identity, record_from_job
from career_agent.enrichment import RuleBasedAnalyzer
from career_agent.handlers import backlog_drainer, deep_analysis_finalize, deep_analysis_worker
from career_agent.models import AnalysisRun, AnalysisTask
from career_agent.profile import load_profile
from career_agent.repository import MemoryRepository
from tests.helpers import ROOT, fixture_jobs


class FakeSqs:
    def __init__(self):
        self.batches: list[dict] = []
        self.messages: list[dict] = []

    def send_message_batch(self, **kwargs):
        self.batches.append(kwargs)
        return {"Successful": [{"Id": item["Id"]} for item in kwargs["Entries"]]}

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": str(len(self.messages))}


class FakeBedrockAnalyzer:
    analysis_version = "bedrock-job-fit-v1"
    calls = 0

    def __init__(self, model_id):
        self.model_id = model_id
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def analyze(self, job, profile):
        type(self).calls += 1
        self.model_calls += 1
        self.input_tokens += 800
        self.output_tokens += 400
        return RuleBasedAnalyzer().analyze(job, profile)


def task(index: int, *, first_queued_at: str | None = None) -> AnalysisTask:
    job = fixture_jobs()[index % len(fixture_jobs())]
    snapshot = job.to_dict()
    snapshot["id"] = f"job-{index}"
    return AnalysisTask(
        customer_id="personal",
        task_id=f"task-{index}",
        cache_key=f"task-{index}",
        job_id=f"job-{index}",
        profile_version="profile-v1",
        job_fingerprint=f"job-fingerprint-{index}",
        profile_fingerprint="profile-fingerprint",
        analyzer_version="bedrock-job-fit-v1",
        model_id="model-1",
        job=snapshot,
        source="fixture",
        source_key=f"fixture:{index % 5}",
        company=f"Company {index % 9}",
        role_family=f"role-{index % 4}",
        priority_score=90 - index / 10,
        priority_components={
            "preliminary_fit": 90 - index / 10,
            "vacancy_urgency": 70,
            "backlog_wait_age": 10,
            "novelty": 90,
        },
        first_queued_at=first_queued_at
        or (datetime.now(UTC) - timedelta(days=5)).isoformat(),
        last_seen_at=datetime.now(UTC).isoformat(),
        estimated_input_tokens=1_000,
    )


class AnalysisBudgetTests(unittest.TestCase):
    def test_discovery_reservation_blocks_backlog_at_fifty_and_releases_unused_slots(self):
        repo = MemoryRepository()
        limits = AnalysisBudgetLimits()
        day = "2026-09-21"
        now = datetime.now(UTC).isoformat()
        self.assertTrue(repo.set_analysis_discovery_state("personal", day, "reserved", now,
                                                          maximum_backlog_calls=50))
        kwargs = dict(input_tokens=500, output_tokens=1000, cost_microusd=16_500,
                      maximum_calls=75, maximum_input_tokens=225_000,
                      maximum_output_tokens=90_000, maximum_cost_microusd=1_750_000)
        for _ in range(50):
            self.assertTrue(repo.reserve_analysis_capacity("personal", day, now,
                                                           run_type="backlog_drain", **kwargs))
        self.assertFalse(repo.reserve_analysis_capacity("personal", day, now,
                                                       run_type="backlog_drain", **kwargs))
        self.assertFalse(repo.reserve_analysis_capacity("personal", day, now,
                                                       run_type="manual_discovery", **kwargs))
        for _ in range(10):
            self.assertTrue(repo.reserve_analysis_capacity("personal", day, now,
                                                           run_type="pipeline", **kwargs))
        self.assertEqual(50, limits.backlog_call_ceiling(repo.get_analysis_budget_usage("personal", day)))
        self.assertTrue(repo.set_analysis_discovery_state("personal", day, "released", now,
                                                          maximum_backlog_calls=50))
        self.assertEqual(75, limits.backlog_call_ceiling(repo.get_analysis_budget_usage("personal", day)))
        for _ in range(15):
            self.assertTrue(repo.reserve_analysis_capacity("personal", day, now,
                                                           run_type="backlog_drain", **kwargs))
        self.assertFalse(repo.reserve_analysis_capacity("personal", day, now,
                                                       run_type="backlog_drain", **kwargs))

    def test_budget_reservation_is_atomic_and_reconciles_to_actual_usage(self):
        repo = MemoryRepository()
        limits = AnalysisBudgetLimits(maximum_calls=1)
        now = datetime.now(UTC).isoformat()

        self.assertTrue(
            repo.reserve_analysis_capacity(
                "personal",
                "2026-09-08",
                now,
                input_tokens=1_000,
                output_tokens=1_000,
                cost_microusd=limits.estimated_cost_microusd(1_000, 1_000),
                maximum_calls=1,
                maximum_input_tokens=10_000,
                maximum_output_tokens=10_000,
                maximum_cost_microusd=100_000,
            )
        )
        self.assertFalse(
            repo.reserve_analysis_capacity(
                "personal",
                "2026-09-08",
                now,
                input_tokens=1_000,
                output_tokens=1_000,
                cost_microusd=18_000,
                maximum_calls=1,
                maximum_input_tokens=10_000,
                maximum_output_tokens=10_000,
                maximum_cost_microusd=100_000,
            )
        )

        repo.reconcile_analysis_capacity(
            "personal",
            "2026-09-08",
            now,
            reserved_input_tokens=1_000,
            reserved_output_tokens=1_000,
            reserved_cost_microusd=18_000,
            actual_calls=1,
            actual_input_tokens=800,
            actual_output_tokens=400,
            actual_cost_microusd=8_400,
        )
        usage = repo.get_analysis_budget_usage("personal", "2026-09-08")
        self.assertEqual(1, usage.calls_committed)
        self.assertEqual(1, usage.calls_actual)
        self.assertEqual(800, usage.input_tokens_committed)
        self.assertEqual(400, usage.output_tokens_committed)
        self.assertEqual(8_400, usage.cost_microusd_committed)

    def test_adaptive_target_reaches_upper_bound_for_an_old_large_backlog(self):
        now = datetime.now(UTC)
        policy = BacklogDrainPolicy()
        tasks = [task(index) for index in range(120)]

        self.assertEqual(75, policy.daily_target(tasks, now))
        selected = policy.select(tasks, count=25, seed="run-1", now=now)
        self.assertEqual(25, len(selected))
        self.assertGreater(len({item.source_key for item in selected}), 1)


class BacklogDrainerTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()
        self.sqs = FakeSqs()
        self.repo.put_analysis_tasks([task(index) for index in range(120)])
        self.environment = {
            "CAREER_CUSTOMER_ID": "personal",
            "DEEP_ANALYSIS_QUEUE_URL": "https://sqs.test/deep-analysis",
            "ANALYSIS_FINALIZER_QUEUE_URL": "https://sqs.test/finalizer",
            "DEEP_ANALYSIS_DEADLINE_MINUTES": "15",
        }

    def test_scheduled_drain_dispatches_one_bounded_adaptive_batch(self):
        self.repo.set_analysis_discovery_state("personal", AnalysisBudgetLimits().budget_date(),
                                               "none", datetime.now(UTC).isoformat(),
                                               maximum_backlog_calls=50)
        with (
            patch.dict("os.environ", self.environment, clear=True),
            patch.object(backlog_drainer, "repository", return_value=self.repo),
            patch.object(backlog_drainer, "_sqs_client", return_value=self.sqs),
        ):
            result = backlog_drainer.handler(
                {"source": "eventbridge-scheduler", "run_id": "2026-09-08T12:00:00Z"},
                None,
            )

        self.assertEqual("waiting", result["status"])
        self.assertEqual(75, result["daily_target_calls"])
        self.assertEqual(25, result["selected_tasks"])
        self.assertEqual([10, 10, 5], [len(batch["Entries"]) for batch in self.sqs.batches])
        run = self.repo.get_analysis_run(
            "backlog-drain-2026-09-08T12:00:00Z"
        )
        self.assertEqual("backlog_drain", run.run_type)
        self.assertEqual(25, run.expected_tasks)
        self.assertTrue(
            all(
                item.status == "dispatched"
                for item in self.repo.get_analysis_tasks(
                    "personal", run.selected_task_ids
                ).values()
            )
        )

    def test_drainer_does_not_dispatch_into_discovery_reserve(self):
        limits = AnalysisBudgetLimits()
        day = limits.budget_date()
        now = datetime.now(UTC).isoformat()
        self.repo.set_analysis_discovery_state("personal", day, "reserved", now,
                                               maximum_backlog_calls=50)
        for _ in range(50):
            self.assertTrue(self.repo.reserve_analysis_capacity(
                "personal", day, now, run_type="backlog_drain",
                input_tokens=500, output_tokens=1_000, cost_microusd=16_500,
                maximum_calls=75, maximum_input_tokens=225_000,
                maximum_output_tokens=90_000, maximum_cost_microusd=1_750_000,
            ))
        with (
            patch.dict("os.environ", self.environment, clear=True),
            patch.object(backlog_drainer, "repository", return_value=self.repo),
            patch.object(backlog_drainer, "_sqs_client", return_value=self.sqs),
        ):
            result = backlog_drainer.handler({"run_id": "reserve-test"}, None)
        self.assertEqual(50, result["backlog_call_ceiling"])
        self.assertEqual(0, result["selected_tasks"])
        self.assertEqual([], self.sqs.batches)

    def test_worker_records_usage_and_defers_when_the_daily_cap_is_reached(self):
        profile = load_profile()
        job = fixture_jobs()[0]
        analyzer = FakeBedrockAnalyzer("model-1")
        identity = analysis_identity(job, profile, analyzer)
        analysis_task = AnalysisTask(
            customer_id="personal",
            task_id=identity.cache_key,
            cache_key=identity.cache_key,
            job_id=job.id,
            profile_version=profile.version,
            job_fingerprint=identity.job_fingerprint,
            profile_fingerprint=identity.profile_fingerprint,
            analyzer_version=identity.analyzer_version,
            model_id=identity.model_id,
            job=job.to_dict(),
            source="fixture",
            source_key="fixture",
            company=job.company,
            role_family="data scientist",
            priority_score=80,
            priority_components={"priority_score": 80},
            first_queued_at=datetime.now(UTC).isoformat(),
            last_seen_at=datetime.now(UTC).isoformat(),
            estimated_input_tokens=1_000,
            status="dispatched",
            selected_run_id="drain-worker",
        )
        run = AnalysisRun(
            run_id="drain-worker",
            customer_id="personal",
            profile_version=profile.version,
            started_at=datetime.now(UTC).isoformat(),
            prepared_at=datetime.now(UTC).isoformat(),
            deadline_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            candidate_artifact_key="",
            selected_task_ids=[identity.cache_key],
            expected_tasks=1,
            send_email=False,
            backlog_size=1,
            run_type="backlog_drain",
        )
        self.repo.put_analysis_tasks([analysis_task])
        self.repo.begin_analysis_run(run)
        worker_environment = {
            **self.environment,
            "PROFILE_PATH": str(ROOT / "data" / "candidate_profile.example.json"),
            "STRONG_MODEL_ID": "model-1",
            "ANALYSIS_DAILY_CALL_LIMIT": "1",
        }
        self.repo.set_analysis_discovery_state(
            "personal", AnalysisBudgetLimits().budget_date(), "none", datetime.now(UTC).isoformat(),
            maximum_backlog_calls=0,
        )
        message = {
            "schema_version": 1,
            "kind": "deep_analysis_task",
            "run_id": "drain-worker",
            "customer_id": "personal",
            "task_id": identity.cache_key,
        }
        FakeBedrockAnalyzer.calls = 0
        with (
            patch.dict("os.environ", worker_environment, clear=True),
            patch.object(deep_analysis_worker, "repository", return_value=self.repo),
            patch.object(deep_analysis_worker, "BedrockAnalyzer", FakeBedrockAnalyzer),
            patch.object(deep_analysis_worker, "_sqs_client", return_value=self.sqs),
        ):
            deep_analysis_worker._process_record(
                {"messageId": "worker-1", "body": json.dumps(message)}
            )

        budget_date = AnalysisBudgetLimits().budget_date()
        usage = self.repo.get_analysis_budget_usage("personal", budget_date)
        self.assertEqual(1, FakeBedrockAnalyzer.calls)
        self.assertEqual(1, usage.calls_actual)
        self.assertEqual(800, usage.input_tokens_actual)
        self.assertEqual(400, usage.output_tokens_actual)

        second_job = fixture_jobs()[1]
        second_identity = analysis_identity(second_job, profile, analyzer)
        second = AnalysisTask(
            customer_id="personal",
            task_id=second_identity.cache_key,
            cache_key=second_identity.cache_key,
            job_id=second_job.id,
            profile_version=profile.version,
            job_fingerprint=second_identity.job_fingerprint,
            profile_fingerprint=second_identity.profile_fingerprint,
            analyzer_version=second_identity.analyzer_version,
            model_id=second_identity.model_id,
            job=second_job.to_dict(),
            source="fixture",
            source_key="fixture",
            company=second_job.company,
            role_family="data scientist",
            priority_score=75,
            priority_components={"priority_score": 75},
            first_queued_at=datetime.now(UTC).isoformat(),
            last_seen_at=datetime.now(UTC).isoformat(),
            estimated_input_tokens=1_000,
            status="dispatched",
            selected_run_id="drain-worker-2",
        )
        self.repo.put_analysis_tasks([second])
        self.repo.begin_analysis_run(
            AnalysisRun(
                run_id="drain-worker-2",
                customer_id="personal",
                profile_version=second.profile_version,
                started_at=datetime.now(UTC).isoformat(),
                prepared_at=datetime.now(UTC).isoformat(),
                deadline_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                candidate_artifact_key="",
                selected_task_ids=[second.task_id],
                expected_tasks=1,
                send_email=False,
                backlog_size=1,
                run_type="backlog_drain",
            )
        )
        second_message = {
            **message,
            "run_id": "drain-worker-2",
            "task_id": second.task_id,
        }
        with (
            patch.dict("os.environ", worker_environment, clear=True),
            patch.object(deep_analysis_worker, "repository", return_value=self.repo),
            patch.object(deep_analysis_worker, "BedrockAnalyzer", FakeBedrockAnalyzer),
            patch.object(deep_analysis_worker, "_sqs_client", return_value=self.sqs),
        ):
            deep_analysis_worker._process_record(
                {"messageId": "worker-2", "body": json.dumps(second_message)}
            )

        deferred = self.repo.get_analysis_task("personal", second_identity.cache_key)
        self.assertEqual("timed_out", deferred.status)
        self.assertEqual("daily_analysis_budget_exhausted", deferred.last_error)
        self.assertEqual(1, FakeBedrockAnalyzer.calls)

    def test_worker_cache_hit_reconciles_zero_usage_without_unbound_counters(self):
        profile = load_profile()
        job = fixture_jobs()[0]
        identity = analysis_identity(job, profile, FakeBedrockAnalyzer("model-1"))
        now = datetime.now(UTC).isoformat()
        selected = AnalysisTask(
            customer_id="personal", task_id=identity.cache_key, cache_key=identity.cache_key,
            job_id=job.id, profile_version=profile.version,
            job_fingerprint=identity.job_fingerprint,
            profile_fingerprint=identity.profile_fingerprint,
            analyzer_version=identity.analyzer_version, model_id=identity.model_id,
            job=job.to_dict(), source="fixture", source_key="fixture", company=job.company,
            role_family="data scientist", priority_score=80,
            priority_components={"priority_score": 80}, first_queued_at=now, last_seen_at=now,
            status="dispatched", selected_run_id="backlog-drain-cache-hit",
        )
        self.repo.put_analysis_tasks([selected])
        self.repo.put_deep_analysis_record(record_from_job(job, identity, input_tokens=0, output_tokens=0))
        self.repo.begin_analysis_run(AnalysisRun(
            run_id="backlog-drain-cache-hit", customer_id="personal", profile_version=profile.version,
            started_at=now, prepared_at=now,
            deadline_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            candidate_artifact_key="", selected_task_ids=[identity.cache_key], expected_tasks=1,
            send_email=False, backlog_size=1, run_type="backlog_drain",
        ))
        with (
            patch.dict("os.environ", {**self.environment, "PROFILE_PATH": str(ROOT / "data" / "candidate_profile.example.json"),
                                       "STRONG_MODEL_ID": "model-1"}, clear=True),
            patch.object(deep_analysis_worker, "repository", return_value=self.repo),
            patch.object(deep_analysis_worker, "BedrockAnalyzer", FakeBedrockAnalyzer),
            patch.object(deep_analysis_worker, "_sqs_client", return_value=self.sqs),
        ):
            deep_analysis_worker._process_record({"messageId": "cache-hit", "body": json.dumps({
                "schema_version": 1, "kind": "deep_analysis_task", "run_id": "backlog-drain-cache-hit",
                "customer_id": "personal", "task_id": identity.cache_key,
            })})
        result = self.repo.get_analysis_task("personal", identity.cache_key)
        self.assertEqual("completed", result.status)
        self.assertEqual("cache", result.result_source)
        self.assertIsNone(self.repo.get_analysis_budget_usage("personal", AnalysisBudgetLimits().budget_date()))

    def test_drain_finalizer_requeues_deadline_timeouts_without_pipeline_ranking(self):
        selected = task(1)
        selected.status = "dispatched"
        selected.selected_run_id = "drain-timeout"
        self.repo.put_analysis_tasks([selected])
        self.repo.begin_analysis_run(
            AnalysisRun(
                run_id="drain-timeout",
                customer_id="personal",
                profile_version=selected.profile_version,
                started_at=(datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
                prepared_at=(datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
                deadline_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                candidate_artifact_key="",
                selected_task_ids=[selected.task_id],
                expected_tasks=1,
                send_email=False,
                backlog_size=1,
                run_type="backlog_drain",
            )
        )
        message = {
            "schema_version": 1,
            "kind": "finalize_deep_analysis",
            "run_id": "drain-timeout",
        }
        with patch.object(
            deep_analysis_finalize, "repository", return_value=self.repo
        ):
            result = deep_analysis_finalize._process_record(
                {"messageId": "finalizer-1", "body": json.dumps(message)}
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("partial", result["pipeline_status"])
        self.assertEqual(
            "timed_out",
            self.repo.get_analysis_task("personal", selected.task_id).status,
        )
        completed = self.repo.get_analysis_run("drain-timeout")
        self.assertEqual("completed", completed.status)
        self.assertEqual(1, completed.tasks_timed_out)


if __name__ == "__main__":
    unittest.main()

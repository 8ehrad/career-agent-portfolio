from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from career_agent.analysis_cache import analysis_identity
from career_agent.async_analysis import SqsAnalysisDispatcher
from career_agent.connectors import FixtureConnector
from career_agent.digest import MemoryEmailSender
from career_agent.enrichment import RuleBasedAnalyzer, SalaryEnricher
from career_agent.handlers import deep_analysis_finalize, deep_analysis_worker
from career_agent.ingestion import MemoryJobStagingStore
from career_agent.pipeline import CareerPipeline
from career_agent.models import Job, RunReport
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


def queue_record(body: dict, message_id: str = "message-1") -> dict:
    return {"messageId": message_id, "body": json.dumps(body), "attributes": {}}


class AsyncAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.staging = MemoryJobStagingStore()
        self.sqs = FakeSqs()
        self.profile = load_profile()
        self.sender = MemoryEmailSender()
        self.dispatcher = SqsAnalysisDispatcher(
            repository=self.repository,
            staging=self.staging,
            sqs=self.sqs,
            analysis_queue_url="https://sqs.test/deep-analysis",
            finalizer_queue_url="https://sqs.test/analysis-finalizer",
            customer_id="personal",
            deadline_minutes=15,
            maximum_concurrency=4,
        )
        self.pipeline = CareerPipeline(
            profile=self.profile,
            repository=self.repository,
            connectors=[FixtureConnector([job.to_dict() for job in fixture_jobs()])],
            fast_analyzer=RuleBasedAnalyzer(),
            deep_analyzer=RuleBasedAnalyzer(),
            salary_enricher=SalaryEnricher(self.repository),
            email_sender=self.sender,
            dashboard_url="https://career.example.com",
            deep_analysis_limit=3,
            analysis_dispatcher=self.dispatcher,
        )

    def test_completed_discovery_releases_unused_daily_calls(self):
        now = datetime.now(UTC)
        scheduled_run_id = now.isoformat()
        day = now.astimezone(ZoneInfo("Europe/London")).date().isoformat()
        self.repository.set_analysis_discovery_state(
            "personal", day, "reserved", now.isoformat(), maximum_backlog_calls=50,
        )
        self.pipeline._release_discovery_reservation(
            RunReport(run_id=scheduled_run_id, started_at=now.isoformat())
        )
        self.assertEqual("released", self.repository.get_analysis_budget_usage("personal", day).discovery_state)

    def test_pipeline_dispatches_durable_tasks_and_finalizes_from_worker_cache(self):
        report, recommendations = self.pipeline.run(run_id="async-run", send_email=False)

        self.assertEqual("analysis_pending", report.status)
        self.assertEqual([], recommendations)
        self.assertEqual(3, report.stage_counts["deep_analysis_dispatched"])
        self.assertEqual(0, report.stage_counts["deep_analysis_attempted"])
        self.assertEqual(5, report.stage_counts["analysis_backlog_total"])
        self.assertEqual(2, report.stage_counts["analysis_backlog_queued_after_dispatch"])
        self.assertEqual([3], [len(batch["Entries"]) for batch in self.sqs.batches])
        analysis_run = self.repository.get_analysis_run("async-run")
        self.assertEqual(3, analysis_run.expected_tasks)
        self.assertTrue(
            all(
                task.status == "dispatched"
                for task in self.repository.get_analysis_tasks(
                    "personal", analysis_run.selected_task_ids
                ).values()
            )
        )

        worker_messages = [
            json.loads(entry["MessageBody"])
            for batch in self.sqs.batches
            for entry in batch["Entries"]
        ]
        environment = {
            "PROFILE_PATH": str(ROOT / "data" / "candidate_profile.example.json"),
            "ANALYSIS_FINALIZER_QUEUE_URL": "https://sqs.test/analysis-finalizer",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(deep_analysis_worker, "repository", return_value=self.repository),
            patch.object(deep_analysis_worker, "_sqs_client", return_value=self.sqs),
        ):
            for index, message in enumerate(worker_messages):
                deep_analysis_worker._process_record(queue_record(message, f"worker-{index}"))
            deep_analysis_worker._process_record(queue_record(worker_messages[0], "duplicate"))

        tasks = self.repository.get_analysis_tasks("personal", analysis_run.selected_task_ids)
        self.assertTrue(all(task.status == "completed" for task in tasks.values()))
        self.assertTrue(all(task.attempts == 1 for task in tasks.values()))
        self.assertEqual(3, len(self.repository.deep_analysis_records))

        immediate_finalizer = next(
            json.loads(message["MessageBody"])
            for message in self.sqs.messages
            if message["DelaySeconds"] == 0
        )
        with (
            patch.dict("os.environ", {"ARTIFACT_BUCKET": "staging"}, clear=False),
            patch.object(deep_analysis_finalize, "repository", return_value=self.repository),
            patch.object(
                deep_analysis_finalize, "S3JobStagingStore", return_value=self.staging
            ),
            patch.object(deep_analysis_finalize, "pipeline", return_value=self.pipeline),
        ):
            result = deep_analysis_finalize._process_record(queue_record(immediate_finalizer))

        completed = self.repository.get_run("async-run")
        self.assertEqual("completed", result["status"])
        self.assertEqual("completed", completed.status)
        self.assertEqual(3, completed.stage_counts["deep_analysis_succeeded"])
        self.assertEqual(3, completed.stage_counts["qualified_shortlist"])
        self.assertEqual(2, completed.stage_counts["analysis_backlog_queued_after_completion"])
        self.assertEqual("completed", self.repository.get_analysis_run("async-run").status)

    def test_deadline_finalizes_only_cached_results_and_keeps_timeouts_in_backlog(self):
        report, _ = self.pipeline.run(run_id="async-timeout", send_email=False)
        self.assertEqual("analysis_pending", report.status)
        analysis_run = self.repository.get_analysis_run("async-timeout")
        self.repository.analysis_runs["async-timeout"].deadline_at = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        delayed_finalizer = json.loads(self.sqs.messages[0]["MessageBody"])

        with (
            patch.dict("os.environ", {"ARTIFACT_BUCKET": "staging"}, clear=False),
            patch.object(deep_analysis_finalize, "repository", return_value=self.repository),
            patch.object(
                deep_analysis_finalize, "S3JobStagingStore", return_value=self.staging
            ),
            patch.object(deep_analysis_finalize, "pipeline", return_value=self.pipeline),
        ):
            result = deep_analysis_finalize._process_record(queue_record(delayed_finalizer))

        completed = self.repository.get_run("async-timeout")
        tasks = self.repository.get_analysis_tasks(
            "personal", analysis_run.selected_task_ids
        )
        self.assertEqual("completed", result["status"])
        self.assertEqual(0, completed.stage_counts["deep_analysis_evaluated"])
        self.assertEqual(3, completed.stage_counts["deep_analysis_failed"])
        self.assertEqual(0, completed.stage_counts["qualified_shortlist"])
        self.assertEqual(5, completed.stage_counts["analysis_backlog_queued_after_completion"])
        self.assertTrue(all(task.status == "timed_out" for task in tasks.values()))

    def test_retry_resumes_existing_dispatch_without_rebuilding_the_run(self):
        first, _ = self.pipeline.run(run_id="async-resume", send_email=False)
        analysis_run = self.repository.get_analysis_run("async-resume")
        initial_batches = len(self.sqs.batches)
        self.repository.release_claim("run#async-resume")
        self.repository.runs["async-resume"].status = "failed"

        resumed, recommendations = self.pipeline.run(
            run_id="async-resume", send_email=False
        )

        self.assertEqual("analysis_pending", resumed.status)
        self.assertEqual([], recommendations)
        self.assertEqual(
            analysis_run.candidate_artifact_key,
            self.repository.get_analysis_run("async-resume").candidate_artifact_key,
        )
        self.assertEqual(initial_batches + 1, len(self.sqs.batches))
        self.assertEqual(first.stage_counts, resumed.stage_counts)

    def test_task_lease_prevents_duplicate_spend_and_error_reopens_retry(self):
        self.pipeline.run(run_id="async-lease", send_email=False)
        analysis_run = self.repository.get_analysis_run("async-lease")
        task_id = analysis_run.selected_task_ids[0]

        self.assertTrue(
            self.repository.start_analysis_task_attempt("personal", task_id, "async-lease")
        )
        self.assertFalse(
            self.repository.start_analysis_task_attempt("personal", task_id, "async-lease")
        )
        self.repository.record_analysis_task_error(
            "personal",
            task_id,
            "async-lease",
            "RuntimeError: malformed response",
            model_calls=1,
            input_tokens=800,
            output_tokens=40,
        )
        self.assertTrue(
            self.repository.start_analysis_task_attempt("personal", task_id, "async-lease")
        )
        self.repository.complete_analysis_task(
            "personal",
            task_id,
            "async-lease",
            datetime.now(UTC).isoformat(),
            result_source="fresh",
            model_calls=1,
            input_tokens=900,
            output_tokens=50,
        )
        task = self.repository.get_analysis_task("personal", task_id)
        self.assertEqual("completed", task.status)
        self.assertEqual(2, task.attempts)
        self.assertEqual(2, task.model_calls)
        self.assertEqual(1700, task.input_tokens)
        self.assertEqual(90, task.output_tokens)

        job = Job.from_dict(task.job)
        identity = analysis_identity(job, self.profile, RuleBasedAnalyzer())
        self.dispatcher.prepare(
            run_id="async-lease-retry",
            started_at=datetime.now(UTC).isoformat(),
            send_email=False,
            profile=self.profile,
            candidates=[job],
            cache_misses=[job],
            selected_jobs=[job],
            identities={job.id: identity},
            selection_reasons={job.id: "priority"},
            priorities={job.id: task.priority_components},
        )
        retried = self.repository.get_analysis_task("personal", task_id)
        self.assertEqual("dispatched", retried.status)
        self.assertEqual(0, retried.attempts)
        self.assertEqual(0, retried.model_calls)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from career_agent.enrichment import RuleBasedAnalyzer
from career_agent.handlers import api
from career_agent.models import (
    AnalysisRun,
    AnalysisTask,
    IngestionRun,
    IngestionTask,
    JobStatus,
    RunReport,
)
from career_agent.profile import load_profile
from career_agent.repository import MemoryRepository
from career_agent.scoring import score_job
from tests.helpers import fixture_jobs


def event(method: str, path: str, body=None, query=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
    }


class ApiTests(unittest.TestCase):
    def setUp(self):
        os.environ["DEV_AUTH_BYPASS"] = "true"
        self.profile = load_profile()
        self.repository = MemoryRepository()
        self.job = next(item for item in fixture_jobs() if item.source_id == "good-1")
        RuleBasedAnalyzer().analyze(self.job, self.profile)
        score_job(self.job, self.profile)
        self.repository.put_job(self.job)
        self.repo_patch = patch.object(api, "repository", return_value=self.repository)
        self.repo_patch.start()

    def tearDown(self):
        self.repo_patch.stop()
        os.environ.pop("DEV_AUTH_BYPASS", None)

    def test_list_and_get_job(self):
        response = api.handler(event("GET", "/jobs"), None)
        self.assertEqual(200, response["statusCode"])
        self.assertEqual(1, json.loads(response["body"])["count"])
        response = api.handler(event("GET", f"/jobs/{self.job.id}"), None)
        self.assertEqual(self.job.id, json.loads(response["body"])["id"])

    def test_incomplete_application_returns_actionable_retry_not_success(self):
        from career_agent.rich_application import ApplicationGenerationIncomplete
        with patch.object(api, 'application_service') as factory:
            factory.return_value.prepare.side_effect=ApplicationGenerationIncomplete('writer_unavailable')
            response=api.handler(event('POST',f'/jobs/{self.job.id}/prepare'),None)
        self.assertEqual(503,response['statusCode'])
        body=json.loads(response['body'])
        self.assertTrue(body['retryable'])
        self.assertEqual('incomplete',body['generation_state'])
        self.assertIn('retry',body['detail'])

    def test_metrics_are_authenticated_and_cached_by_the_handler(self):
        snapshot = {"range": "7d", "traffic": {"page_views": 12}}
        with patch.object(api, "_metrics_snapshot", return_value=snapshot) as metrics:
            response = api.handler(event("GET", "/metrics", query={"range": "7d"}), None)
        self.assertEqual(200, response["statusCode"])
        self.assertEqual(snapshot, json.loads(response["body"]))
        self.assertEqual("7d", metrics.call_args.args[0])

    def test_invalid_metrics_range_is_rejected(self):
        with patch.object(api, "_metrics_snapshot", side_effect=ValueError("range must be valid")):
            response = api.handler(event("GET", "/metrics", query={"range": "year"}), None)
        self.assertEqual(400, response["statusCode"])

    def test_pipeline_runs_exposes_persisted_funnel_and_definitions(self):
        self.repository.put_run(
            RunReport(
                run_id="baseline",
                started_at="2026-09-05T10:00:00+00:00",
                stage_counts={"canonical_unique": 120, "qualified_shortlist": 4},
                new_stage_counts={"canonical_unique": 80},
                source_metrics={"ashby": {"canonical_unique": 30, "qualified_shortlist": 2}},
            )
        )

        response = api.handler(event("GET", "/pipeline-runs", query={"limit": "10"}), None)
        payload = json.loads(response["body"])

        self.assertEqual(200, response["statusCode"])
        self.assertEqual("baseline", payload["items"][0]["run_id"])
        self.assertIn("qualified_shortlist", payload["stage_definitions"])
        self.assertIn("highest-priority source", payload["attribution"])

    def test_pipeline_runs_rejects_invalid_limit(self):
        response = api.handler(event("GET", "/pipeline-runs", query={"limit": "1000"}), None)
        self.assertEqual(400, response["statusCode"])

    def test_ingestion_runs_exposes_task_progress(self):
        self.repository.begin_ingestion_run(
            IngestionRun(
                run_id="fanout",
                started_at="2026-09-07T08:00:00+00:00",
                deadline_at="2026-09-07T08:45:00+00:00",
                expected_tasks=1,
                send_email=True,
            ),
            [IngestionTask("fanout", "workday:bank", "Bank", "workday", "bank")],
        )

        response = api.handler(event("GET", "/ingestion-runs", query={"limit": "10"}), None)
        payload = json.loads(response["body"])

        self.assertEqual(200, response["statusCode"])
        self.assertEqual({"queued": 1}, payload["items"][0]["task_status_counts"])
        self.assertEqual("workday:bank", payload["items"][0]["tasks"][0]["source_key"])

    def test_analysis_backlog_exposes_priority_without_full_description(self):
        self.repository.put_analysis_tasks(
            [
                AnalysisTask(
                    customer_id="personal",
                    task_id="task-1",
                    cache_key="task-1",
                    job_id=self.job.id,
                    profile_version=self.profile.version,
                    job_fingerprint="job",
                    profile_fingerprint="profile",
                    analyzer_version="fit-v1",
                    model_id="model-1",
                    job=self.job.to_dict(),
                    source=self.job.source,
                    source_key=self.job.source_key or self.job.source,
                    company=self.job.company,
                    role_family=self.job.role_family or "data scientist",
                    priority_score=83.5,
                    priority_components={"preliminary_fit": 90.0, "priority_score": 83.5},
                    first_queued_at="2026-09-08T08:00:00+00:00",
                    last_seen_at="2026-09-08T08:00:00+00:00",
                )
            ]
        )

        response = api.handler(
            event("GET", "/analysis-backlog", query={"status": "queued"}), None
        )
        payload = json.loads(response["body"])

        self.assertEqual(200, response["statusCode"])
        self.assertEqual({"queued": 1}, payload["status_counts"])
        self.assertEqual(83.5, payload["items"][0]["priority_score"])
        self.assertNotIn("description", payload["items"][0]["job"])

    def test_analysis_throughput_exposes_budget_slo_and_drain_runs(self):
        now = datetime.now(UTC)
        self.repository.begin_analysis_run(
            AnalysisRun(
                run_id="drain-1",
                customer_id="personal",
                profile_version=self.profile.version,
                started_at=now.isoformat(),
                prepared_at=now.isoformat(),
                deadline_at=(now + timedelta(minutes=15)).isoformat(),
                candidate_artifact_key="",
                selected_task_ids=[],
                expected_tasks=0,
                send_email=False,
                backlog_size=0,
                run_type="backlog_drain",
            )
        )

        response = api.handler(event("GET", "/analysis-throughput"), None)
        payload = json.loads(response["body"])

        self.assertEqual(200, response["statusCode"])
        self.assertEqual(75, payload["daily_limits"]["calls"])
        self.assertEqual(72, payload["policy"]["oldest_job_slo_hours"])
        self.assertEqual("drain-1", payload["runs"][0]["run_id"])


    def test_linkedin_coverage_sample_is_human_entered_and_never_fetched(self):
        response = api.handler(
            event(
                "POST",
                "/coverage-benchmark/samples",
                {
                    "title": "ML Scientist",
                    "company": "Example Bank",
                    "location": "London",
                    "reference_url": "https://www.linkedin.com/jobs/view/123",
                },
            ),
            None,
        )
        self.assertEqual(201, response["statusCode"])

        response = api.handler(event("GET", "/coverage-benchmark"), None)
        payload = json.loads(response["body"])
        self.assertEqual(1, payload["count"])
        self.assertIn("no LinkedIn page", payload["method"])

    def test_coverage_sample_rejects_non_linkedin_reference_url(self):
        response = api.handler(
            event(
                "POST",
                "/coverage-benchmark/samples",
                {
                    "title": "ML Scientist",
                    "company": "Example Bank",
                    "reference_url": "https://example.com/job/123",
                },
            ),
            None,
        )
        self.assertEqual(400, response["statusCode"])

    def test_viewing_coverage_benchmark_does_not_resolve_an_expired_sample(self):
        observed = datetime.now(UTC) - timedelta(hours=25)
        response = api.handler(
            event(
                "POST",
                "/coverage-benchmark/samples",
                {
                    "title": "ML Scientist",
                    "company": "Example Bank",
                    "observed_at": observed.isoformat(),
                },
            ),
            None,
        )
        self.assertEqual(201, response["statusCode"])

        response = api.handler(event("GET", "/coverage-benchmark"), None)

        self.assertEqual("pending", json.loads(response["body"])["items"][0]["status"])

    def test_preflight_does_not_require_authentication(self):
        os.environ.pop("DEV_AUTH_BYPASS", None)
        response = api.handler(event("OPTIONS", "/jobs"), None)
        self.assertEqual(204, response["statusCode"])

    def test_valid_tracking_transitions(self):
        response = api.handler(
            event("PATCH", f"/jobs/{self.job.id}", {"status": "shortlisted", "notes": "Strong fit"}), None
        )
        self.assertEqual(200, response["statusCode"])
        self.assertEqual(JobStatus.SHORTLISTED, self.repository.get_job(self.job.id).status)
        response = api.handler(event("PATCH", f"/jobs/{self.job.id}", {"status": "applied"}), None)
        self.assertEqual(200, response["statusCode"])
        stored = self.repository.get_job(self.job.id)
        self.assertEqual(JobStatus.APPLIED, stored.status)
        self.assertIn("applied_at", stored.application_tracking)

    def test_invalid_transition_is_rejected(self):
        response = api.handler(event("PATCH", f"/jobs/{self.job.id}", {"status": "offer"}), None)
        self.assertEqual(400, response["statusCode"])

    def test_dismiss_and_restore(self):
        response = api.handler(
            event("PATCH", f"/jobs/{self.job.id}", {"status": "dismissed", "reason": "Not the right domain"}), None
        )
        self.assertEqual(200, response["statusCode"])
        self.assertEqual("Not the right domain", self.repository.get_job(self.job.id).dismissal_reason)
        response = api.handler(event("PATCH", f"/jobs/{self.job.id}", {"status": "recommended"}), None)
        self.assertEqual(200, response["statusCode"])


if __name__ == "__main__":
    unittest.main()

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from career_agent.application_async import new_task, queue_task
from career_agent.handlers import api, application_prepare_worker as worker
from career_agent.models import ApplicationPack, Job, JobStatus
from career_agent.repository import MemoryRepository
from career_agent.services import ApplicationService
from career_agent.storage import MemoryArtifactStore


def job():
    return Job(id="async-job", source="greenhouse", source_id="one", title="Data Scientist",
        company="Example", location="UK", description="Python and ML.",
        source_url="https://example.test/one", apply_url="https://example.test/one",
        status=JobStatus.SHORTLISTED)


def event(method, path):
    return {"requestContext": {"http": {"method": method}}, "rawPath": path}


class ServiceStub:
    def __init__(self, repo, *, identity="current", fail=None):
        self.repository = repo
        self.profile = Mock()
        self.generator = Mock()
        self.generator.identity.return_value = identity
        self.fail = fail
        self.calls = 0

    def prepare(self, job_id, *, expected_identity):
        self.calls += 1
        if self.fail:
            raise self.fail
        pack = ApplicationPack(job_id=job_id, created_at=datetime.now(UTC).isoformat(),
            profile_version="fixture", cv_headline="Data Scientist", cv_summary="Summary",
            selected_evidence_ids=[], tailored_bullets=[], cover_letter="Letter",
            recruiter_message="Hello", interview_topics=[], gaps_to_address=[],
            generation_metadata={
            "identity": expected_identity, "generation_mode": "rich_model"})
        self.repository.put_application_pack(pack)
        self.repository.update_job_status(job_id, JobStatus.READY)
        return pack

    def download_urls(self, job_id):
        return {"cv": "https://example.test/private-cv"}


class ApplicationAsyncTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()
        self.repo.put_job(job())
        self.service = ServiceStub(self.repo)
        self.env = patch.dict(os.environ, {"DEV_AUTH_BYPASS": "true", "APPLICATION_PROFILE_MODE": "rich",
            "APPLICATION_PREPARE_QUEUE_URL": "https://sqs.example.test/queue", "CAREER_CUSTOMER_ID": "personal"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_task_claim_is_idempotent_and_terminal_task_can_be_replaced(self):
        task = new_task("personal", "async-job", "current", JobStatus.SHORTLISTED)
        first, created = self.repo.begin_application_task(task)
        self.assertTrue(created)
        same, created = self.repo.begin_application_task(new_task("personal", "async-job", "current", JobStatus.SHORTLISTED))
        self.assertFalse(created)
        self.assertEqual(first.task_id, same.task_id)
        with self.assertRaisesRegex(ValueError, "in progress"):
            self.repo.begin_application_task(new_task("personal", "async-job", "other", JobStatus.SHORTLISTED))
        claimed = self.repo.claim_application_task("personal", "async-job", task.task_id,
            "2026-10-05T10:00:00+00:00", "2026-10-05T10:05:00+00:00")
        self.assertEqual(1, claimed.attempts)
        self.assertIsNone(self.repo.claim_application_task("personal", "async-job", task.task_id,
            "2026-10-05T10:01:00+00:00", "2026-10-05T10:06:00+00:00"))
        self.assertTrue(self.repo.complete_application_task("personal", "async-job", task.task_id,
            "2026-10-05T10:02:00+00:00"))
        replacement, created = self.repo.begin_application_task(new_task("personal", "async-job", "other", JobStatus.SHORTLISTED))
        self.assertTrue(created)
        self.assertNotEqual(task.task_id, replacement.task_id)

    def test_api_queue_duplicate_click_worker_and_status(self):
        sent = []
        with patch.object(api, "application_service", return_value=self.service), \
             patch("career_agent.application_async.queue_task", side_effect=lambda task, **_: sent.append(task)):
            first = api.handler(event("POST", "/jobs/async-job/prepare"), None)
            second = api.handler(event("POST", "/jobs/async-job/prepare"), None)
            self.assertEqual(202, first["statusCode"])
            self.assertEqual(202, second["statusCode"])
            self.assertEqual(json.loads(first["body"])["task_id"], json.loads(second["body"])["task_id"])
            self.assertEqual(1, len(sent))
            self.assertEqual(JobStatus.PREPARING, self.repo.get_job("async-job").status)
            queued = api.handler(event("GET", "/jobs/async-job/prepare-status"), None)
            self.assertEqual("queued", json.loads(queued["body"])["status"])
        message = {"schema_version": 1, "kind": "application_prepare", "customer_id": "personal",
                   "job_id": "async-job", "task_id": sent[0].task_id}
        with patch.object(worker, "repository", return_value=self.repo), \
             patch.object(worker, "application_service", return_value=self.service):
            result = worker.handler({"Records": [{"messageId": "m1", "body": json.dumps(message)}]}, None)
        self.assertEqual([], result["batchItemFailures"])
        self.assertEqual(1, self.service.calls)
        self.assertEqual(JobStatus.READY, self.repo.get_job("async-job").status)
        self.assertEqual("completed", self.repo.get_application_task("personal", "async-job").status)
        with patch.object(api, "application_service", return_value=self.service):
            ready = api.handler(event("GET", "/jobs/async-job/prepare-status"), None)
        self.assertEqual("completed", json.loads(ready["body"])["status"])

    def test_terminal_writing_failure_restores_prior_status(self):
        from career_agent.rich_application import ApplicationGenerationIncomplete
        self.service.fail = ApplicationGenerationIncomplete("unsupported_prose")
        task, _ = self.repo.begin_application_task(new_task("personal", "async-job", "current", JobStatus.SHORTLISTED))
        self.repo.update_job_status("async-job", JobStatus.PREPARING)
        message = {"schema_version": 1, "kind": "application_prepare", "customer_id": "personal",
                   "job_id": "async-job", "task_id": task.task_id}
        with patch.object(worker, "repository", return_value=self.repo), \
             patch.object(worker, "application_service", return_value=self.service):
            result = worker.handler({"Records": [{"messageId": "m1", "body": json.dumps(message)}]}, None)
        self.assertEqual([], result["batchItemFailures"])
        self.assertEqual("failed", self.repo.get_application_task("personal", "async-job").status)
        self.assertEqual(JobStatus.SHORTLISTED, self.repo.get_job("async-job").status)

    def test_completed_task_does_not_present_a_stale_pack_as_current(self):
        task, _ = self.repo.begin_application_task(new_task("personal", "async-job", "old", JobStatus.SHORTLISTED))
        self.repo.claim_application_task("personal", "async-job", task.task_id,
            "2026-10-05T10:00:00+00:00", "2026-10-05T10:05:00+00:00")
        self.repo.complete_application_task("personal", "async-job", task.task_id, "2026-10-05T10:01:00+00:00")
        with patch.object(api, "application_service", return_value=self.service):
            response = api.handler(event("GET", "/jobs/async-job/prepare-status"), None)
        self.assertEqual("not_started", json.loads(response["body"])["status"])

    def test_queue_send_failure_is_visible_and_retryable_by_user(self):
        with patch.object(api, "application_service", return_value=self.service), \
             patch("career_agent.application_async.queue_task", side_effect=RuntimeError("network")):
            response = api.handler(event("POST", "/jobs/async-job/prepare"), None)
        self.assertEqual(503, response["statusCode"])
        self.assertEqual("failed", self.repo.get_application_task("personal", "async-job").status)
        self.assertEqual(JobStatus.SHORTLISTED, self.repo.get_job("async-job").status)

    def test_transport_message_contains_only_task_address(self):
        client = Mock()
        task = new_task("personal", "async-job", "current", JobStatus.SHORTLISTED)
        queue_task(task, client=client, queue_url="https://sqs.example.test/queue")
        body = json.loads(client.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual({"schema_version", "kind", "customer_id", "job_id", "task_id"}, set(body))
        self.assertNotIn("current", json.dumps(body))

    def test_changed_job_is_not_published_after_slow_generation(self):
        self.repo.update_job_status("async-job", JobStatus.PREPARING)
        store = MemoryArtifactStore()
        generator = Mock()
        generator.identity.side_effect = lambda item, _: item.description

        def change_job(*_):
            changed = self.repo.get_job("async-job")
            changed.description = "Advert changed while writing"
            self.repo.put_job(changed)
            return Mock()

        generator.generate.side_effect = change_job
        service = ApplicationService(self.repo, Mock(), generator, store)
        with self.assertRaisesRegex(ValueError, "Job changed"):
            service.prepare("async-job", expected_identity="Python and ML.")
        self.assertIsNone(self.repo.get_application_pack("async-job"))
        self.assertEqual({}, store.objects)

    def test_reconciler_recovers_lost_dispatch_and_stops_after_bounded_attempts(self):
        task, _ = self.repo.begin_application_task(new_task("personal", "async-job", "current", JobStatus.SHORTLISTED))
        self.repo.update_job_status("async-job", JobStatus.PREPARING)
        stored = self.repo.application_tasks[("personal", "async-job")]
        stored.updated_at = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        with patch.object(worker, "repository", return_value=self.repo), \
             patch.object(worker, "queue_task") as sender:
            result = worker.handler({"kind": "application_reconcile"}, None)
            self.assertEqual({"requeued": 1, "failed": 0}, result)
            sender.assert_called_once()
            self.assertEqual(1, self.repo.get_application_task("personal", "async-job").recovery_dispatches)
            stored.updated_at = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
            stored.recovery_dispatches = 4
            result = worker.handler({"kind": "application_reconcile"}, None)
        self.assertEqual({"requeued": 0, "failed": 1}, result)
        self.assertEqual("failed", self.repo.get_application_task("personal", "async-job").status)
        self.assertEqual(JobStatus.SHORTLISTED, self.repo.get_job("async-job").status)


if __name__ == "__main__":
    unittest.main()

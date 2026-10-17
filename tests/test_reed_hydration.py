from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from career_agent.connectors import ReedConnector
from career_agent.handlers import (
    backlog_drainer,
    deep_analysis_worker,
    reed_hydration_drainer,
    reed_hydration_ready,
    reed_hydration_worker,
)
from career_agent.listing_details import make_record
from career_agent.reed_hydration import (
    SqsReedHydrationDispatcher,
    hydrated_reed_analysis_task_ids,
    hydration_message,
)
from career_agent.repository import MemoryRepository


def summary(index: int = 1) -> dict:
    return {
        "jobId": index,
        "jobTitle": "Data Scientist",
        "employerName": "Example",
        "locationName": "Bristol, UK",
        "jobDescription": "Search excerpt",
        "minimumSalary": 60000,
        "maximumSalary": 80000,
        "date": (datetime.now(UTC) - timedelta(days=1)).date().isoformat(),
        "jobUrl": f"https://example.test/{index}",
    }


def details(index: int = 1) -> dict:
    return {
        **summary(index),
        "jobDescription": "Required: Python, SQL and machine learning production experience.",
        "yearlyMinimumSalary": 60000,
        "yearlyMaximumSalary": 80000,
        "expirationDate": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
    }


class FakeSqs:
    def __init__(self):
        self.messages: list[dict] = []

    def send_message_batch(self, *, QueueUrl, Entries):
        del QueueUrl
        self.messages.extend(json.loads(entry["MessageBody"]) for entry in Entries)
        return {"Successful": Entries}

    def send_message(self, *, QueueUrl, MessageBody):
        del QueueUrl
        self.messages.append(json.loads(MessageBody))
        return {"MessageId": "message"}


class ReedHydrationTests(unittest.TestCase):
    def test_dispatcher_persists_and_enqueues_every_listing_without_a_100_item_cap(self):
        repo = MemoryRepository()
        sqs = FakeSqs()
        dispatcher = SqsReedHydrationDispatcher(repo, sqs, "queue")

        sent = dispatcher.register([summary(index) for index in range(1, 251)])

        self.assertEqual(250, sent)
        self.assertEqual(250, len(repo.list_reed_hydration_tasks("personal")))
        self.assertEqual(250, len(sqs.messages))
        self.assertTrue(all(task.status == "dispatched" for task in repo.reed_hydration_tasks.values()))

    def test_completed_task_requeues_when_detail_cache_is_missing(self):
        repo = MemoryRepository()
        sqs = FakeSqs()
        dispatcher = SqsReedHydrationDispatcher(repo, sqs, "queue")
        dispatcher.register([summary()])
        self.assertTrue(repo.start_reed_hydration_attempt("personal", "1"))
        repo.complete_reed_hydration_task(
            "personal", "1", datetime.now(UTC).isoformat(),
            expected_fingerprint=repo.get_reed_hydration_task("personal", "1").summary_fingerprint,
        )
        self.assertEqual(1, dispatcher.register([summary()]))
        self.assertEqual(1, dispatcher.register([{**summary(), "minimumSalary": 65000}]))
        self.assertEqual("dispatched", repo.get_reed_hydration_task("personal", "1").status)

    def test_superseded_worker_and_ready_updates_cannot_finish_new_summary(self):
        repo = MemoryRepository()
        dispatcher = SqsReedHydrationDispatcher(repo, FakeSqs(), "queue")
        dispatcher.register([summary()])
        old_fingerprint = repo.get_reed_hydration_task("personal", "1").summary_fingerprint
        self.assertTrue(repo.start_reed_hydration_attempt("personal", "1"))
        dispatcher.register([{**summary(), "minimumSalary": 65000}])

        self.assertFalse(
            repo.complete_reed_hydration_task(
                "personal", "1", datetime.now(UTC).isoformat(),
                expected_fingerprint=old_fingerprint,
            )
        )
        repo.mark_reed_hydration_ready_processed(
            "personal", "1", datetime.now(UTC).isoformat(),
            outcome="rejected", expected_fingerprint=old_fingerprint,
        )
        current = repo.get_reed_hydration_task("personal", "1")
        self.assertEqual("dispatched", current.status)
        self.assertEqual("pending", current.ready_status)

    def test_reconciler_recovers_stale_dispatch_and_missing_ready_event(self):
        repo = MemoryRepository()
        sqs = FakeSqs()
        dispatcher = SqsReedHydrationDispatcher(repo, sqs, "hydration")
        dispatcher.register([summary(1), summary(2)])
        old = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        repo.reed_hydration_tasks[("personal", "1")].last_dispatched_at = old
        self.assertTrue(repo.start_reed_hydration_attempt("personal", "2"))
        repo.complete_reed_hydration_task(
            "personal", "2", old,
            expected_fingerprint=repo.get_reed_hydration_task("personal", "2").summary_fingerprint,
        )
        sqs.messages.clear()

        with (
            patch.object(reed_hydration_drainer, "repository", return_value=repo),
            patch.object(reed_hydration_drainer, "_sqs_client", return_value=sqs),
            patch.dict(
                os.environ,
                {
                    "REED_HYDRATION_QUEUE_URL": "hydration",
                    "REED_HYDRATION_READY_QUEUE_URL": "ready",
                },
                clear=False,
            ),
        ):
            result = reed_hydration_drainer.handler({}, None)

        self.assertEqual(1, result["hydration_dispatched"])
        self.assertEqual(1, result["ready_pending"])
        self.assertEqual(1, result["ready_dispatched"])
        self.assertEqual(
            {"reed_hydration_task", "reed_hydration_ready"},
            {message["kind"] for message in sqs.messages},
        )

    @patch("career_agent.connectors.get_json")
    def test_async_connector_returns_only_full_cache_hits_and_queues_all_misses(self, request):
        repo = MemoryRepository()
        repo.put_listing_details(
            make_record("1", summary(1), details(1), datetime.now(UTC))
        )
        sqs = FakeSqs()
        request.return_value = {"results": [summary(1), summary(2), summary(3)]}
        connector = ReedConnector(
            "key",
            ("data scientist",),
            detail_store=repo,
            hydration_dispatcher=SqsReedHydrationDispatcher(repo, sqs, "queue"),
        )

        jobs = connector.fetch()

        self.assertEqual(["1"], [job.source_id for job in jobs])
        self.assertEqual(2, connector.detail_metrics["hydration_registered"])
        self.assertEqual(2, connector.detail_metrics["deferred"])
        self.assertEqual(1, request.call_count)  # Search only; no inline detail requests.
        self.assertIsNone(connector.telemetry_configuration()["detail_results_limit"])

    @patch.object(reed_hydration_worker, "load_integration_environment")
    @patch.object(reed_hydration_worker, "repository")
    @patch("career_agent.handlers.reed_hydration_worker.get_json")
    @patch("career_agent.handlers.reed_hydration_worker.time.sleep")
    def test_worker_fetches_once_persists_full_advert_and_emits_ready(
        self, sleep, get_json, repository, load_environment
    ):
        del sleep, load_environment
        repo = MemoryRepository()
        sqs = FakeSqs()
        dispatcher = SqsReedHydrationDispatcher(repo, sqs, "hydration")
        dispatcher.register([summary()])
        get_json.return_value = details()
        repository.return_value = repo
        record = {
            "body": json.dumps(hydration_message("personal", "1")),
            "messageId": "one",
        }

        with patch.dict(
            os.environ,
            {"REED_API_KEY": "key", "REED_HYDRATION_READY_QUEUE_URL": "ready"},
            clear=False,
        ), patch.object(reed_hydration_worker, "_sqs_client", return_value=sqs):
            result = reed_hydration_worker._process_record(record)
            duplicate = reed_hydration_worker._process_record(record)

        self.assertEqual("completed", result["status"])
        self.assertEqual("ready_replayed", duplicate["status"])
        self.assertIn("machine learning", repo.get_listing_details("reed", ["1"])["1"]["details"]["jobDescription"])
        self.assertEqual(1, get_json.call_count)
        self.assertEqual(2, sum(message["kind"] == "reed_hydration_ready" for message in sqs.messages))

    @patch.object(reed_hydration_worker, "repository")
    @patch("career_agent.handlers.reed_hydration_worker.get_json")
    def test_daily_request_limit_defers_without_calling_reed(self, get_json, repository):
        repo = MemoryRepository()
        sqs = FakeSqs()
        SqsReedHydrationDispatcher(repo, sqs, "hydration").register([summary()])
        repository.return_value = repo
        message = {"body": json.dumps(hydration_message("personal", "1"))}

        with patch.dict(os.environ, {"REED_HYDRATION_DAILY_REQUEST_LIMIT": "0"}, clear=False):
            result = reed_hydration_worker._process_record(message)

        self.assertEqual("budget_deferred", result["status"])
        self.assertEqual("queued", repo.get_reed_hydration_task("personal", "1").status)
        self.assertIsNotNone(repo.get_reed_hydration_task("personal", "1").next_attempt_at)
        get_json.assert_not_called()

    @patch.object(reed_hydration_worker, "repository")
    @patch("career_agent.handlers.reed_hydration_worker.get_json")
    def test_worker_rechecks_cache_before_spending_api_request(self, get_json, repository):
        repo = MemoryRepository()
        sqs = FakeSqs()
        SqsReedHydrationDispatcher(repo, sqs, "hydration").register([summary()])
        repo.put_listing_details(
            make_record("1", summary(), details(), datetime.now(UTC))
        )
        repository.return_value = repo

        with (
            patch.object(reed_hydration_worker, "_sqs_client", return_value=sqs),
            patch.dict(os.environ, {"REED_HYDRATION_READY_QUEUE_URL": "ready"}, clear=False),
        ):
            result = reed_hydration_worker._process_record(
                {"body": json.dumps(hydration_message("personal", "1"))}
            )

        self.assertEqual("cache_hit", result["status"])
        self.assertEqual({}, repo.reed_hydration_budgets)
        get_json.assert_not_called()

    @patch.object(reed_hydration_worker, "load_integration_environment")
    @patch.object(reed_hydration_worker, "repository")
    @patch("career_agent.handlers.reed_hydration_worker.get_json")
    @patch("career_agent.handlers.reed_hydration_worker.time.sleep")
    def test_invalid_detail_is_retained_as_terminal_failed_task_after_limit(
        self, sleep, get_json, repository, load_environment
    ):
        del sleep, load_environment
        repo = MemoryRepository()
        sqs = FakeSqs()
        SqsReedHydrationDispatcher(repo, sqs, "hydration").register([summary()])
        repository.return_value = repo
        get_json.return_value = {"jobId": 1, "jobDescription": ""}
        message = {"body": json.dumps(hydration_message("personal", "1"))}

        with patch.dict(
            os.environ,
            {"REED_API_KEY": "key", "REED_HYDRATION_MAX_ATTEMPTS": "1"},
            clear=False,
        ):
            result = reed_hydration_worker._process_record(message)

        self.assertEqual("failed", result["status"])
        self.assertEqual("failed", repo.get_reed_hydration_task("personal", "1").status)
        self.assertEqual(1, repo.get_reed_hydration_task("personal", "1").attempts)
        self.assertEqual({}, repo.get_listing_details("reed", ["1"]))

    @patch.object(reed_hydration_ready, "repository")
    def test_ready_processor_queues_existing_analysis_task_contract_without_email(self, repository):
        repo = MemoryRepository()
        sqs = FakeSqs()
        SqsReedHydrationDispatcher(repo, sqs, "hydration").register([summary()])
        self.assertTrue(repo.start_reed_hydration_attempt("personal", "1"))
        repo.put_listing_details(
            make_record("1", summary(), details(), datetime.now(UTC))
        )
        repo.complete_reed_hydration_task(
            "personal", "1", datetime.now(UTC).isoformat(),
            expected_fingerprint=repo.get_reed_hydration_task("personal", "1").summary_fingerprint,
        )
        repository.return_value = repo
        record = {
            "body": json.dumps(
                {
                    "schema_version": 1,
                    "kind": "reed_hydration_ready",
                    "customer_id": "personal",
                    "source_id": "1",
                }
            ),
            "messageId": "ready",
        }

        with patch.dict(os.environ, {"STRONG_MODEL_ID": ""}, clear=False):
            result = reed_hydration_ready._process_record(record)
            duplicate = reed_hydration_ready._process_record(record)

        self.assertEqual("analysis_queued", result["status"])
        self.assertEqual("duplicate", duplicate["status"])
        tasks = repo.list_analysis_tasks("personal")
        self.assertEqual(1, len(tasks))
        self.assertEqual("queued", tasks[0].status)
        self.assertEqual("analysis_queued", repo.get_reed_hydration_task("personal", "1").ready_status)
        self.assertEqual({tasks[0].task_id}, hydrated_reed_analysis_task_ids(repo, tasks))

        stale = tasks[0]
        stale.job["description"] = "Search excerpt"
        self.assertEqual(set(), hydrated_reed_analysis_task_ids(repo, [stale]))

        repo.put_analysis_tasks([stale])
        sqs = FakeSqs()
        with (
            patch.object(backlog_drainer, "repository", return_value=repo),
            patch.object(backlog_drainer, "_sqs_client", return_value=sqs),
            patch.dict(
                os.environ,
                {
                    "DEEP_ANALYSIS_QUEUE_URL": "analysis",
                    "ANALYSIS_FINALIZER_QUEUE_URL": "finalizer",
                },
                clear=False,
            ),
        ):
            drain = backlog_drainer.handler({"run_id": "reed-snippet-exclusion"}, None)
        self.assertEqual(0, drain["selected_tasks"])

        stale.status = "dispatched"
        stale.selected_run_id = "legacy-reed"
        repo.put_analysis_tasks([stale])
        with (
            patch.object(deep_analysis_worker, "repository", return_value=repo),
            patch.dict(os.environ, {"STRONG_MODEL_ID": ""}, clear=False),
        ):
            deep_analysis_worker._process_record(
                {
                    "body": json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "deep_analysis_task",
                            "run_id": "legacy-reed",
                            "customer_id": "personal",
                            "task_id": stale.task_id,
                        }
                    )
                }
            )
        self.assertEqual("timed_out", repo.get_analysis_task("personal", stale.task_id).status)
        self.assertEqual({}, repo.analysis_budgets)


if __name__ == "__main__":
    unittest.main()

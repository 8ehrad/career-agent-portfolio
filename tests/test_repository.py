from __future__ import annotations

import json
import unittest

from career_agent.models import (
    AnalysisRun,
    AnalysisTask,
    DeepAnalysisRecord,
    JobAnalysisHistory,
    JobStatus,
    ReedHydrationTask,
    RunReport,
)
from career_agent.repository import DynamoRepository
from tests.helpers import fixture_jobs


class PaginatedTable:
    def __init__(self, payloads):
        self.payloads = payloads
        self.query_calls = []
        self.scan_calls = []
        self.put_calls = []

    def _page(self, kwargs):
        if "ExclusiveStartKey" not in kwargs:
            return {"Items": [{"payload": self.payloads[0]}], "LastEvaluatedKey": {"pk": "cursor"}}
        return {"Items": [{"payload": self.payloads[1]}]}

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return self._page(kwargs)

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return self._page(kwargs)

    def put_item(self, **kwargs):
        self.put_calls.append(kwargs)


class FakeDynamo:
    def __init__(self, table):
        self.table = table

    def Table(self, name):
        return self.table

    def batch_get_item(self, RequestItems):
        table_name, request = next(iter(RequestItems.items()))
        items = [
            self.table.items[(key["pk"], key["sk"])]
            for key in request["Keys"]
            if (key["pk"], key["sk"]) in self.table.items
        ]
        return {"Responses": {table_name: items}, "UnprocessedKeys": {}}


class HistoryTable:
    def __init__(self):
        self.items = {}

    def get_item(self, *, Key, **kwargs):
        del kwargs
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item else {}

    def put_item(self, *, Item, **kwargs):
        del kwargs
        self.items[(Item["pk"], Item["sk"])] = Item

    def batch_writer(self, **kwargs):
        del kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class DynamoRepositoryTests(unittest.TestCase):
    def setUp(self):
        jobs = fixture_jobs()[:2]
        self.table = PaginatedTable([json.dumps(job.to_dict()) for job in jobs])
        self.repository = DynamoRepository("test", FakeDynamo(self.table))

    def test_backlog_call_reservation_checks_discovery_state_atomically(self):
        class BudgetTable:
            def __init__(self):
                self.update = None

            def get_item(self, **kwargs):
                return {"Item": {"discovery_state": "reserved", "calls_committed": 49}}

            def update_item(self, **kwargs):
                self.update = kwargs

        table = BudgetTable()
        repo = DynamoRepository("test", FakeDynamo(table))
        self.assertTrue(repo.reserve_analysis_capacity(
            "personal", "2026-09-21", "2026-09-21T12:00:00+00:00",
            input_tokens=500, output_tokens=1000, cost_microusd=16_500,
            maximum_calls=75, maximum_input_tokens=225_000,
            maximum_output_tokens=90_000, maximum_cost_microusd=1_750_000,
            run_type="backlog_drain",
        ))
        self.assertIn("discovery_state = :expected_discovery_state", table.update["ConditionExpression"])
        self.assertEqual(49, table.update["ExpressionAttributeValues"][":calls_before"])
        self.assertEqual("reserved", table.update["ExpressionAttributeValues"][":expected_discovery_state"])

    def test_status_query_reads_every_page(self):
        jobs = self.repository.list_jobs(JobStatus.RECOMMENDED)
        self.assertEqual(2, len(jobs))
        self.assertEqual(2, len(self.table.query_calls))
        self.assertEqual({"pk": "cursor"}, self.table.query_calls[1]["ExclusiveStartKey"])

    def test_unfiltered_scan_reads_every_page(self):
        jobs = self.repository.list_jobs()
        self.assertEqual(2, len(jobs))
        self.assertEqual(2, len(self.table.scan_calls))

    def test_runs_use_time_ordered_gsi_and_are_queryable(self):
        reports = [
            RunReport(run_id="new", started_at="2026-09-05T10:00:00+00:00"),
            RunReport(run_id="old", started_at="2026-09-03T10:00:00+00:00"),
        ]
        table = PaginatedTable([json.dumps(report.to_dict()) for report in reports])
        repository = DynamoRepository("test", FakeDynamo(table))

        repository.put_run(reports[0])
        stored = table.put_calls[0]["Item"]
        runs = repository.list_runs(2)

        self.assertEqual("ENTITY#run", stored["gsi1pk"])
        self.assertEqual(["new", "old"], [run.run_id for run in runs])
        self.assertEqual("gsi1", table.query_calls[0]["IndexName"])

    def test_analysis_history_is_batch_persisted_and_baseline_is_marked(self):
        table = HistoryTable()
        repository = DynamoRepository("test", FakeDynamo(table))
        history = JobAnalysisHistory(
            job_id="job-1",
            first_seen_at="2026-09-05T10:00:00+00:00",
            last_seen_at="2026-09-05T10:00:00+00:00",
            seen_runs=1,
        )

        repository.put_analysis_histories([history])
        loaded = repository.get_analysis_histories(["job-1", "missing"])
        self.assertEqual(1, loaded["job-1"].seen_runs)
        self.assertFalse(repository.analysis_history_initialized())

        repository.mark_analysis_history_initialized("2026-09-05T10:00:00+00:00")
        self.assertTrue(repository.analysis_history_initialized())

    def test_deep_analysis_records_are_versioned_and_batch_queryable(self):
        table = HistoryTable()
        repository = DynamoRepository("test", FakeDynamo(table))
        record = DeepAnalysisRecord(
            cache_key="cache-1",
            job_id="job-1",
            job_fingerprint="job-fingerprint",
            profile_fingerprint="profile-fingerprint",
            analyzer_version="fit-v1",
            model_id="model-1",
            analyzed_at="2026-09-07T10:00:00+00:00",
            match_reasons=["Strong evidence match"],
            input_tokens=100,
            output_tokens=25,
        )

        repository.put_deep_analysis_record(record)
        loaded = repository.get_deep_analysis_records(["cache-1", "missing"])

        self.assertEqual(record.to_dict(), loaded["cache-1"].to_dict())
        stored = table.items[("DEEP_ANALYSIS#cache-1", "RESULT")]
        self.assertEqual("deep_analysis_cache", stored["entity_type"])
        self.assertIsInstance(stored["expires_at"], int)

    def test_analysis_backlog_and_run_round_trip(self):
        table = HistoryTable()
        repository = DynamoRepository("test", FakeDynamo(table))
        task = AnalysisTask(
            customer_id="personal",
            task_id="cache-1",
            cache_key="cache-1",
            job_id="job-1",
            profile_version="profile-v1",
            job_fingerprint="job-fingerprint",
            profile_fingerprint="profile-fingerprint",
            analyzer_version="fit-v1",
            model_id="model-1",
            job=fixture_jobs()[0].to_dict(),
            source="fixture",
            source_key="fixture",
            company="Example",
            role_family="data scientist",
            priority_score=82.5,
            priority_components={"priority_score": 82.5},
            first_queued_at="2026-09-08T10:00:00+00:00",
            last_seen_at="2026-09-08T10:00:00+00:00",
            status="dispatched",
            selected_run_id="run-1",
        )
        analysis_run = AnalysisRun(
            run_id="run-1",
            customer_id="personal",
            profile_version="profile-v1",
            started_at="2026-09-08T10:00:00+00:00",
            prepared_at="2026-09-08T10:01:00+00:00",
            deadline_at="2026-09-08T10:16:00+00:00",
            candidate_artifact_key="raw/jobs/run-1/candidates.json.gz",
            selected_task_ids=["cache-1"],
            expected_tasks=1,
            send_email=False,
            backlog_size=10,
        )

        repository.put_analysis_tasks([task])
        self.assertTrue(repository.begin_analysis_run(analysis_run))
        loaded_task = repository.get_analysis_task("personal", "cache-1")
        loaded_run = repository.get_analysis_run("run-1")

        self.assertEqual(task.to_dict(), loaded_task.to_dict())
        self.assertEqual(analysis_run.to_dict(), loaded_run.to_dict())
        stored = table.items[("ANALYSIS_BACKLOG#personal", "TASK#cache-1")]
        self.assertEqual("ANALYSIS_TASK#personal#dispatched", stored["gsi1pk"])

    def test_reed_hydration_task_round_trip_and_analysis_queue_shape(self):
        table = HistoryTable()
        repository = DynamoRepository("test", FakeDynamo(table))
        now = "2026-09-19T10:00:00+00:00"
        hydration = ReedHydrationTask(
            customer_id="personal",
            source_id="123",
            summary={"jobId": 123, "jobTitle": "Data Scientist"},
            summary_fingerprint="summary-hash",
            first_seen_at=now,
            last_seen_at=now,
        )

        self.assertEqual(["123"], repository.upsert_reed_hydration_tasks([hydration]))
        self.assertEqual(hydration.to_dict(), repository.get_reed_hydration_task("personal", "123").to_dict())
        stored = table.items[("REED_HYDRATION#personal", "TASK#123")]
        self.assertEqual("summary-hash", stored["summary_fingerprint"])
        self.assertNotIn("expires_at", stored)

        job = fixture_jobs()[0]
        analysis = AnalysisTask(
            customer_id="personal",
            task_id="cache-ready",
            cache_key="cache-ready",
            job_id=job.id,
            profile_version="profile-v1",
            job_fingerprint="job-hash",
            profile_fingerprint="profile-hash",
            analyzer_version="fit-v1",
            model_id="model-1",
            job=job.to_dict(),
            source="reed",
            source_key="reed",
            company=job.company,
            role_family="data scientist",
            priority_score=80.0,
            priority_components={"priority_score": 80.0},
            first_queued_at=now,
            last_seen_at=now,
        )
        self.assertTrue(repository.queue_analysis_task(analysis))
        self.assertEqual("queued", repository.get_analysis_task("personal", "cache-ready").status)
        stored_analysis = table.items[("ANALYSIS_BACKLOG#personal", "TASK#cache-ready")]
        self.assertEqual("ANALYSIS_TASK#personal#queued", stored_analysis["gsi1pk"])


if __name__ == "__main__":
    unittest.main()

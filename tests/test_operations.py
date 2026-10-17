from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from career_agent.handlers import api
from career_agent.models import AnalysisBudgetUsage, AnalysisTask, ReedHydrationTask, RunReport
from career_agent.operations import OperationsService, compact_task, resource_configuration, summarize_tasks
from career_agent.repository import DynamoRepository, MemoryRepository
from tests.test_api import event

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


def analysis_task(index=1, **changes):
    values = dict(customer_id="personal", task_id=f"a-{index}", cache_key=f"a-{index}", job_id=f"j-{index}",
                  profile_version="v1", job_fingerprint="f", profile_fingerprint="p", analyzer_version="v1",
                  model_id="m", job={"title": f"AI Engineer {index}", "company": "Example",
                  "description": "PRIVATE FULL DESCRIPTION", "location": "UK"}, source="reed", source_key="reed",
                  company="Example", role_family="AI", priority_score=80, priority_components={},
                  first_queued_at=(NOW-timedelta(hours=index)).isoformat(), last_seen_at=NOW.isoformat())
    values.update(changes)
    return AnalysisTask(**values)


def hydration_task(index=1, **changes):
    values = dict(customer_id="personal", source_id=str(index), summary={"jobTitle": "Data Scientist",
                  "employerName": "Example", "locationName": "UK", "jobDescription": "PRIVATE SNIPPET"},
                  summary_fingerprint="f", first_seen_at=NOW.isoformat(), last_seen_at=NOW.isoformat())
    values.update(changes)
    return ReedHydrationTask(**values)


def fixture_service():
    repo = MemoryRepository()
    for index, state in enumerate(["queued", "timed_out", "dispatched", "processing", "completed", "expired"], 1):
        task = analysis_task(index, status=state,
                             last_error="daily_analysis_budget_exhausted" if state == "timed_out" else None)
        repo.analysis_tasks[("personal", task.task_id)] = task
    for index, state, ready in [(1, "queued", "pending"), (2, "completed", "pending"),
                                (3, "completed", "rejected"), (4, "completed", "analysis_queued"),
                                (5, "failed", "pending")]:
        repo.reed_hydration_tasks[("personal", str(index))] = hydration_task(index, status=state, ready_status=ready)
    repo.put_run(RunReport(run_id="fixture-run", started_at=NOW.isoformat(), completed_at=NOW.isoformat(),
        status="completed", stage_counts={"collected_listings": 200, "canonical_unique": 120,
        "hard_filter_passed": 30, "qualified_shortlist": 4, "digest_included": 4},
        new_stage_counts={"canonical_unique": 100}, source_metrics={"reed": {"canonical_unique": 120}}, emailed=True))
    repo.analysis_budgets[("personal", "2026-09-19")] = AnalysisBudgetUsage(
        "personal", "2026-09-19", calls_committed=25, calls_actual=24,
        input_tokens_committed=75000, input_tokens_actual=74000,
        output_tokens_committed=25000, output_tokens_actual=24000,
        cost_microusd_committed=600000, cost_microusd_actual=580000)
    sqs = Mock()
    sqs.get_queue_attributes.return_value = {"Attributes": {"ApproximateNumberOfMessages": "7",
        "ApproximateNumberOfMessagesNotVisible": "2", "ApproximateNumberOfMessagesDelayed": "1"}}
    cw = Mock()
    cw.get_metric_data.return_value = {"MetricDataResults": []}
    service = OperationsService(repo, sqs=sqs, cloudwatch=cw, clock=lambda: NOW,
        queues=[{"id": "reed", "label": "Reed description retrieval", "unit": "listing messages",
                 "url": "https://sqs.example/queue"}])
    return service


class OperationsTests(unittest.TestCase):
    def test_durable_counts_do_not_count_deliveries_or_terminal_work(self):
        value = fixture_service().overview()
        self.assertEqual(4, value["analysis"]["active"])
        self.assertEqual(4, value["analysis"]["active_unique_jobs"])
        self.assertEqual(1, value["analysis"]["budget_deferred"])
        self.assertEqual(0, value["analysis"]["other_timed_out"])
        self.assertEqual(2, value["analysis"]["waiting_dispatch"])
        self.assertEqual(1, value["hydration"]["active"])
        self.assertEqual(3, value["hydration"]["description_ready_total"])
        self.assertEqual(1, value["hydration"]["description_ready_waiting_admission"])
        self.assertEqual({"pending": 1, "rejected": 1, "analysis_queued": 1}, value["hydration"]["ready_outcomes"])
        self.assertEqual(7, value["queues"]["items"][0]["waiting"])

    def test_active_job_count_deduplicates_versions_and_ignores_completed(self):
        items = [compact_task("analysis", analysis_task(i, job_id="same", status=state))
                 for i, state in enumerate(["queued", "dispatched", "completed"], 1)]
        result = summarize_tasks(items, "analysis", True, NOW)
        self.assertEqual(2, result["active"])
        self.assertEqual(1, result["active_unique_jobs"])
        self.assertEqual(2, result["oldest_active_hours"])

    def test_empty_is_zero_but_age_is_not_invented(self):
        result = summarize_tasks([], "analysis", True, NOW)
        self.assertEqual(0, result["active"])
        self.assertIsNone(result["oldest_active_hours"])

    def test_budget_denial_does_not_hide_other_timed_out_reasons(self):
        items = [compact_task("analysis", analysis_task(status="timed_out", last_error="bedrock_error"))]
        result = summarize_tasks(items, "analysis", True, NOW)
        self.assertEqual(0, result["budget_deferred"])
        self.assertEqual(1, result["other_timed_out"])

    def test_budget_remaining_uses_committed_not_actual(self):
        result = fixture_service().overview()["budget"]
        self.assertEqual(50, result["remaining"]["calls"])
        self.assertEqual(1150000, result["remaining"]["cost_microusd"])

    def test_london_budget_day_near_utc_midnight(self):
        service = OperationsService(MemoryRepository(), clock=lambda: datetime(2026, 9, 19, 23, 30, tzinfo=UTC))
        self.assertEqual("2026-09-20", service.overview()["budget"]["budget_date"])

    def test_counts_and_detail_pages_reconcile(self):
        service = fixture_service()
        overview = service.overview()
        first = service.task_page({"kind": "analysis", "limit": "2"})
        second = service.task_page({"kind": "analysis", "limit": "2", "offset": "2", "snapshot_id": first["snapshot_id"]})
        third = service.task_page({"kind": "analysis", "limit": "2", "offset": "4", "snapshot_id": first["snapshot_id"]})
        items = first["items"] + second["items"] + third["items"]
        self.assertEqual(overview["snapshot_id"], first["snapshot_id"])
        self.assertEqual(overview["analysis"]["retained_total"], len(items))
        self.assertEqual(len(items), len({item["id"] for item in items}))
        self.assertIsNone(third["next_offset"])

    def test_filter_search_and_scope(self):
        service = fixture_service()
        task = analysis_task(99, customer_id="someone-else")
        service.repo.analysis_tasks[("someone-else", task.task_id)] = task
        page = service.task_page({"status": "queued", "q": "example"})
        self.assertEqual(1, page["total"])
        self.assertEqual("a-1", page["items"][0]["id"])

    def test_stale_and_invalid_cursors_are_rejected(self):
        service = fixture_service()
        for parameters in ({"offset": "-1"}, {"limit": "101"}, {"offset": "2"},
                           {"snapshot_id": "old"}, {"kind": "other"}, {"offset": "x"},
                           {"status": "invented"}, {"kind": "hydration", "status": "expired"}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                service.task_page(parameters)

    def test_no_full_advert_or_profile_in_task_results(self):
        service = fixture_service()
        for kind in ("analysis", "hydration"):
            result = json.dumps(service.task_page({"kind": kind}))
            self.assertNotIn("PRIVATE", result)
            self.assertNotIn('"description"', result)
        self.assertNotIn('"items"', json.dumps(service.overview()["analysis"]))

    def test_cached_snapshot_does_not_requery(self):
        service = fixture_service()
        with patch.object(service.repo, "list_analysis_tasks", wraps=service.repo.list_analysis_tasks) as listing:
            service.overview(); service.overview(); service.task_page({})
        self.assertEqual(1, listing.call_count)

    def test_pagination_survives_another_lambda_with_identical_listing_state(self):
        first, second = fixture_service(), fixture_service()
        second.clock = lambda: NOW + timedelta(minutes=3)
        page = first.task_page({"limit": "2"})
        following = second.task_page({"limit": "2", "offset": "2", "snapshot_id": page["snapshot_id"]})
        self.assertEqual(page["snapshot_id"], following["snapshot_id"])
        self.assertFalse({row["id"] for row in page["items"]} & {row["id"] for row in following["items"]})
        second.repo.analysis_tasks[("personal", "a-1")].status = "completed"
        second._expires = 0
        with self.assertRaises(ValueError):
            second.task_page({"offset": "2", "snapshot_id": page["snapshot_id"]})

    def test_section_failure_is_unknown_and_healthy_sections_survive(self):
        service = fixture_service()
        with patch.object(service.repo, "list_analysis_tasks", side_effect=RuntimeError("no access")):
            with self.assertLogs("career_agent.operations", level="ERROR"):
                value = service.overview()
        self.assertEqual("unavailable", value["analysis"]["status"])
        self.assertNotIn("active", value["analysis"])
        self.assertEqual(1, value["hydration"]["active"])
        self.assertIsNone(service.task_page({})["total"])

    def test_missing_sqs_attributes_are_not_zero(self):
        service = fixture_service()
        service.sqs.get_queue_attributes.return_value = {"Attributes": {}}
        with self.assertLogs("career_agent.operations", level="ERROR"):
            result = service.overview()["queues"]["items"][0]
        self.assertEqual("unavailable", result["status"])
        self.assertIsNone(result["waiting"])

    def test_cloudwatch_missing_partial_and_paginated_metrics_are_unknown(self):
        for response in ({"MetricDataResults": []}, {"MetricDataResults": [{"Id": "q0age", "StatusCode": "PartialData", "Values": [5]}]},
                         {"NextToken": "more", "MetricDataResults": [{"Id": "q0age", "StatusCode": "Complete", "Values": [5]}]}):
            service = fixture_service(); service.cloudwatch.get_metric_data.return_value = response
            result = service.overview()["queues"]["items"][0]
            self.assertIsNone(result["oldest_message_seconds"])
            self.assertIsNone(result["deliveries_last_hour"])

    def test_cloudwatch_deliveries_are_not_unique_completions(self):
        service = fixture_service()
        service.cloudwatch.get_metric_data.return_value = {"MetricDataResults": [
            {"Id": "q0age", "StatusCode": "Complete", "Values": [120, 150], "Timestamps": [NOW, NOW-timedelta(minutes=5)]},
            {"Id": "q0received", "StatusCode": "Complete", "Values": [10, 15]}]}
        result = service.overview()["queues"]["items"][0]
        self.assertEqual(120, result["oldest_message_seconds"])
        self.assertEqual(25, result["deliveries_last_hour"])
        self.assertEqual(7, result["waiting"])

    def test_run_counters_preserved_missing_not_filled(self):
        service = fixture_service()
        self.assertNotIn("source_fetches", service.overview()["runs"]["items"][0])
        result = service.run_detail("fixture-run")["item"]
        self.assertEqual(120, result["stage_counts"]["canonical_unique"])
        self.assertNotIn("deep_analysis_evaluated", result["stage_counts"])
        self.assertEqual(100, result["new_stage_counts"]["canonical_unique"])

    def test_run_lookup_does_not_accept_arbitrary_records(self):
        service = fixture_service()
        for value in (None, "", "a" * 201):
            with self.assertRaises(ValueError):
                service.run_detail(value)
        with self.assertRaises(KeyError):
            service.run_detail("unlisted-run")

    def test_schedule_telemetry_distinguishes_invocation_from_success_and_missing(self):
        service = fixture_service()
        service.scheduler = Mock()
        service.scheduler.get_schedule.return_value = {"State": "ENABLED", "ScheduleExpression": "rate(1 hour)"}
        config = {"name": "recovery", "label": "Recovery", "function": "fn", "cadence_seconds": 3600}
        service.cloudwatch.get_metric_data.return_value = {"MetricDataResults": [{"StatusCode": "Complete",
            "Timestamps": [NOW-timedelta(hours=2)], "Values": [1]}]}
        row = service._schedule(config, NOW)
        self.assertTrue(row["cadence_overdue"])
        self.assertNotIn("last_success", row)
        service.cloudwatch.get_metric_data.return_value = {"MetricDataResults": [{"StatusCode": "Complete", "Timestamps": [], "Values": []}]}
        self.assertIsNone(service._schedule(config, NOW)["cadence_overdue"])
        service.schedules = [config]
        service.scheduler.get_schedule.side_effect = RuntimeError("denied")
        with self.assertLogs("career_agent.operations", level="ERROR"):
            self.assertEqual("partial", service._schedules(NOW)["status"])

    def test_alarm_pagination_includes_later_failures(self):
        service = fixture_service()
        service.alarm_prefix = "prod-"
        service.cloudwatch.describe_alarms.side_effect = [
            {"MetricAlarms": [{"AlarmName": "prod-one", "StateValue": "OK", "StateUpdatedTimestamp": NOW}], "NextToken": "next"},
            {"MetricAlarms": [{"AlarmName": "prod-two", "StateValue": "ALARM", "StateUpdatedTimestamp": NOW}]},
        ]
        result = service._alarms()
        self.assertTrue(result["complete"])
        self.assertEqual(["OK", "ALARM"], [row["state"] for row in result["items"]])
        self.assertEqual("next", service.cloudwatch.describe_alarms.call_args.kwargs["NextToken"])

    def test_api_requires_owner_for_both_operations_routes(self):
        for path in ("/operations", "/operations/tasks", "/operations/run"):
            for claims in ({}, {"sub": "x", "email": "other@example.com"}):
                request = event("GET", path)
                request["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
                with patch.dict(os.environ, {"DEV_AUTH_BYPASS": "false"}), patch.object(api, "_operations_service") as factory:
                    self.assertEqual(401, api.handler(request, None)["statusCode"])
                    factory.assert_not_called()

    def test_api_owner_success_no_store(self):
        request = event("GET", "/operations")
        request["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": "owner", "email": "owner@example.com"}}}
        with patch.dict(os.environ, {"DEV_AUTH_BYPASS": "false", "ALLOWED_EMAIL": "owner@example.com"}), patch.object(api, "_operations_service", return_value=fixture_service()):
            response = api.handler(request, None)
        self.assertEqual(200, response["statusCode"])
        self.assertEqual("no-store", response["headers"]["cache-control"])

    def test_coverage_rejects_lookalike_linkedin_hostname(self):
        with patch.dict(os.environ, {"DEV_AUTH_BYPASS": "true"}), patch.object(api, "repository", return_value=MemoryRepository()):
            response = api.handler(event("POST", "/coverage-benchmark/samples", body={
                "title": "AI Engineer", "company": "Example", "reference_url": "https://evillinkedin.com/jobs/1"}), None)
        self.assertEqual(400, response["statusCode"])

    def test_resource_configuration_covers_application_queue_and_recovery_schedule(self):
        environment = {key: f"https://sqs.test/123/prod-{name}" for key, name in (
            ("WORKDAY_QUEUE_URL", "workday"), ("PIPELINE_FINALIZER_QUEUE_URL", "pipeline-finalizer"),
            ("DEEP_ANALYSIS_QUEUE_URL", "deep-analysis"), ("ANALYSIS_FINALIZER_QUEUE_URL", "analysis-finalizer"),
            ("REED_HYDRATION_QUEUE_URL", "reed-hydration"), ("REED_HYDRATION_READY_QUEUE_URL", "reed-hydration-ready"),
            ("APPLICATION_PREPARE_QUEUE_URL", "application-prepare"))}
        queues, schedules, _ = resource_configuration({**environment, "OPERATIONS_RESOURCE_PREFIX": "prod"})
        self.assertEqual(15, len(queues)); self.assertEqual(8, sum(bool(q.get("dlq")) for q in queues))
        self.assertEqual(5, len(schedules))
        self.assertEqual("https://sqs.test/123/prod-scheduler-dlq", queues[-1]["url"])


class OperationsDynamoTests(unittest.TestCase):
    def test_default_reads_beyond_twenty_pages_after_identity_migration(self):
        table = Mock()
        resource = Mock()
        resource.Table.return_value = table
        pages = []
        for index in range(25):
            task = analysis_task(index, status="cancelled" if index < 20 else "queued")
            page = {"Items": [{"payload": json.dumps(task.to_dict()), "status": task.status}]}
            if index < 24:
                page["LastEvaluatedKey"] = {"pk": "backlog", "sk": str(index)}
            pages.append(page)
        table.query.side_effect = pages
        service = OperationsService(DynamoRepository("table", resource), clock=lambda: NOW)
        items, complete = service._tasks("analysis")
        self.assertTrue(complete)
        self.assertEqual(25, len(items))
        self.assertEqual(5, summarize_tasks(items, "analysis", complete, NOW)["active"])

    def service(self, pages, max_pages=20):
        table = Mock()
        table.query.side_effect = pages
        resource = SimpleNamespace(Table=lambda _: table)
        return OperationsService(DynamoRepository("table", resource), clock=lambda: NOW, max_pages=max_pages), table

    def test_reads_all_pages_and_mutable_state_not_stale_payload(self):
        a, b = analysis_task(1), analysis_task(2)
        service, table = self.service([
            {"Items": [{"payload": json.dumps(a.to_dict()), "status": "completed"}], "LastEvaluatedKey": {"pk": "p", "sk": "s"}},
            {"Items": [{"payload": json.dumps(b.to_dict()), "status": "timed_out", "last_error": "daily_analysis_budget_exhausted"}]}])
        items, complete = service._tasks("analysis")
        self.assertTrue(complete)
        self.assertEqual(["completed", "timed_out"], [item["status"] for item in items])
        self.assertEqual({"pk": "p", "sk": "s"}, table.query.call_args_list[1].kwargs["ExclusiveStartKey"])
        self.assertTrue(all(call.kwargs["ConsistentRead"] for call in table.query.call_args_list))
        self.assertEqual("ANALYSIS_BACKLOG#personal", table.query.call_args.kwargs["ExpressionAttributeValues"][":pk"])

    def test_bounded_read_marks_lower_bound_and_deduplicates(self):
        item = {"payload": json.dumps(analysis_task().to_dict())}
        service, _ = self.service([{"Items": [item, item], "LastEvaluatedKey": {"pk": "p", "sk": "s"}}], 1)
        result = service._task_section("analysis", NOW)
        self.assertEqual("partial", result["status"])
        self.assertFalse(result["complete"])
        self.assertEqual(1, result["retained_total"])

    def test_hydration_decodes_mutable_ready_outcome(self):
        service, _ = self.service([{"Items": [{"payload": json.dumps(hydration_task().to_dict()),
            "status": "completed", "ready_status": "rejected", "gate_reasons": ["location"]}]}])
        result = service._task_section("hydration", NOW)
        self.assertEqual(0, result["description_ready_waiting_admission"])
        self.assertEqual({"rejected": 1}, result["ready_outcomes"])

    def test_ingestion_task_pages_are_not_truncated(self):
        def item(key):
            return {"run_id": "r", "source_key": key, "employer": key, "ats": "workday", "status": "completed"}
        service, table = self.service([{"Items": [item("one")], "LastEvaluatedKey": {"pk": "p", "sk": "s"}}, {"Items": [item("two")]}])
        tasks = service.repo.list_ingestion_tasks("r")
        self.assertEqual(2, len(tasks)); self.assertEqual(2, table.query.call_count)


if __name__ == "__main__":
    unittest.main()

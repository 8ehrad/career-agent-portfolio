import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from career_agent.connectors import ReedConnector, ConnectorError, connectors_from_environment, collect_jobs_with_diagnostics
from career_agent.listing_details import make_record, reusable
from career_agent.repository import MemoryRepository, DynamoRepository
from tests.test_repository import HistoryTable, FakeDynamo


NOW = datetime(2026, 9, 18, tzinfo=UTC)


def summary(i=1):
    return {"jobId": i, "jobTitle": "Data Scientist", "employerName": "Example",
            "locationName": "UK", "jobDescription": "Summary...", "jobUrl": f"https://example.test/{i}"}


def details(i=1):
    return {**summary(i), "jobDescription": "<h2>Required</h2><ul><li>Python</li></ul>",
            "yearlyMinimumSalary": 60000, "expirationDate": "2026-10-01"}


class ListingCacheTests(unittest.TestCase):
    @patch("career_agent.connectors.get_json")
    def test_durable_reuse_across_connector_and_repository_instances(self, request):
        table = HistoryTable()
        store = DynamoRepository("test", FakeDynamo(table))
        request.side_effect = [{"results": [summary()]}, details()]
        first = ReedConnector("key", ("data scientist",), detail_store=store).fetch()[0]
        request.reset_mock()
        request.side_effect = [{"results": [summary()]}]
        second = ReedConnector("key", ("data scientist",), detail_results_limit=0,
                               detail_store=DynamoRepository("test", FakeDynamo(table)))
        job = second.fetch()[0]
        self.assertEqual(1, request.call_count)  # Search only; no detail request.
        self.assertEqual(first.description, job.description)
        self.assertEqual(first.salary.minimum_gbp, job.salary.minimum_gbp)
        self.assertEqual(first.closes_at, job.closes_at)
        self.assertEqual(1, second.detail_metrics["cache_hits"])
        saved = json.loads(table.items[("LISTING#reed#1", "DETAILS")]["payload"])
        self.assertEqual(details()["jobDescription"], saved["details"]["jobDescription"])
        self.assertNotIn("expires_at", saved)
        self.assertNotIn("expires_at", table.items[("LISTING#reed#1", "DETAILS")])

    @patch("career_agent.connectors.get_json")
    def test_hits_free_request_budget_for_previously_deferred_jobs(self, request):
        repo = MemoryRepository()
        request.side_effect = [{"results": [summary(1), summary(2)]}, details(1),
                               {"results": [summary(1), summary(2)]}, details(2)]
        connector = ReedConnector("key", ("data scientist",), detail_results_limit=1, detail_store=repo)
        connector.fetch()
        self.assertEqual(1, connector.detail_metrics["deferred"])
        jobs = connector.fetch()
        self.assertEqual(1, connector.detail_metrics["cache_hits"])
        self.assertEqual(1, connector.detail_metrics["requests"])
        self.assertTrue(all("Required" in j.description for j in jobs))
        self.assertEqual(2, len(repo.listing_details))

    def test_refresh_rules_and_integrity(self):
        record = make_record("1", summary(), details(), NOW)
        self.assertTrue(reusable(record, "1", summary()))
        record["fetched_at"] = (NOW - timedelta(days=365)).isoformat()
        self.assertTrue(reusable(record, "1", summary()))
        self.assertFalse(reusable(record, "1", {**summary(), "jobDescription": "Changed"}))
        self.assertFalse(reusable(record, "2", summary()))
        record = make_record("1", summary(), details(), NOW)
        record["details"]["jobDescription"] = "tampered"
        self.assertFalse(reusable(record, "1", summary()))

    @patch("career_agent.connectors.get_json")
    def test_changed_summary_refetches_and_updates_record(self, request):
        repo = MemoryRepository()
        repo.put_listing_details(make_record("1", summary(), details(), datetime.now(UTC)))
        changed = {**summary(), "minimumSalary": 70000}
        request.side_effect = [{"results": [changed]}, {**details(), "yearlyMinimumSalary": 70000}]
        connector = ReedConnector("key", ("data scientist",), detail_store=repo)
        self.assertEqual(70000, connector.fetch()[0].salary.minimum_gbp)
        self.assertEqual(1, connector.detail_metrics["refresh_due"])

    @patch("career_agent.connectors.get_json")
    def test_failed_or_invalid_refresh_does_not_overwrite_previous(self, request):
        for response in [ConnectorError("unavailable"), {}, {"jobDescription": " "},
                         {"jobDescription": "<p>&nbsp;</p>"}, details(2)]:
            with self.subTest(response=type(response).__name__):
                repo = MemoryRepository()
                original = make_record("1", summary(), details(), datetime.now(UTC)-timedelta(days=8))
                repo.put_listing_details(original)
                request.side_effect = [{"results": [{**summary(), "minimumSalary": 70000}]}, response]
                connector = ReedConnector("key", ("data scientist",), detail_store=repo)
                self.assertEqual(1, len(connector.fetch()))
                self.assertEqual(original, repo.get_listing_details("reed", ["1"])["1"])
                self.assertEqual(0, connector.detail_metrics["stored"])

    @patch("career_agent.connectors.get_json")
    def test_read_outage_does_not_refetch_all_details(self, request):
        repo = MemoryRepository()
        request.return_value = {"results": [summary()]}
        with patch.object(repo, "get_listing_details", side_effect=RuntimeError("offline")):
            connector = ReedConnector("key", ("data scientist",), detail_store=repo)
            connector.fetch()
        self.assertEqual(1, request.call_count)
        self.assertEqual(1, connector.detail_metrics["cache_read_errors"])
        self.assertEqual(1, connector.detail_metrics["deferred"])

    @patch("career_agent.connectors.get_json")
    def test_write_failure_is_visible_but_fetched_text_still_used(self, request):
        repo = MemoryRepository()
        request.side_effect = [{"results": [summary()]}, details()]
        with patch.object(repo, "put_listing_details", side_effect=RuntimeError("offline")):
            connector = ReedConnector("key", ("data scientist",), detail_store=repo)
            jobs, _, _, diagnostics = collect_jobs_with_diagnostics([connector])
        self.assertIn("Required", jobs[0].description)
        self.assertEqual(1, diagnostics[0].to_dict()["detail_metrics"]["persist_errors"])

    @patch("career_agent.connectors.get_json")
    def test_duplicate_search_hits_only_fetch_detail_once(self, request):
        request.side_effect = [{"results": [summary()]}, {"results": [summary()]}, details()]
        connector = ReedConnector("key", ("data scientist", "AI scientist"), detail_store=MemoryRepository())
        self.assertEqual(1, len(connector.fetch()))
        self.assertEqual(1, connector.detail_metrics["requests"])

    def test_environment_injects_store_and_has_no_age_refresh(self):
        repo = MemoryRepository()
        with patch.dict(os.environ, {"REED_API_KEY": "key", "REED_DETAIL_MAX_AGE_HOURS": "24"}, clear=True):
            connector = connectors_from_environment(detail_store=repo)[0]
        self.assertIs(repo, connector.detail_store)
        self.assertEqual("search_metadata_change_only", connector.telemetry_configuration()["detail_refresh_policy"])
        self.assertNotIn("detail_max_age_hours", connector.telemetry_configuration())

    @patch("career_agent.connectors.get_json")
    def test_year_old_description_does_not_trigger_detail_request(self, request):
        repo = MemoryRepository()
        repo.put_listing_details(make_record("1", summary(), details(), NOW - timedelta(days=365)))
        request.return_value = {"results": [summary()]}
        connector = ReedConnector("key", ("data scientist",), detail_store=repo)
        self.assertIn("Required", connector.fetch()[0].description)
        self.assertEqual(1, request.call_count)
        self.assertEqual(0, connector.detail_metrics["requests"])

    def test_storage_size_guard_never_truncates(self):
        table = HistoryTable()
        repo = DynamoRepository("test", FakeDynamo(table))
        record = make_record("1", summary(), {**details(), "jobDescription": "字" * 110000}, NOW)
        with self.assertRaisesRegex(ValueError, "safe item size"):
            repo.put_listing_details(record)
        self.assertEqual({}, table.items)

    def test_batch_reads_chunk_deduplicate_and_retry(self):
        class BatchDynamo(FakeDynamo):
            def __init__(self):
                super().__init__(HistoryTable())
                self.calls = []
            def batch_get_item(self, RequestItems):
                self.calls.append(RequestItems)
                if len(self.calls) == 1:
                    return {"UnprocessedKeys": RequestItems}
                return {"Responses": {"test": []}}
        db = BatchDynamo()
        with patch("career_agent.repository.time.sleep"):
            DynamoRepository("test", db).get_listing_details("reed", [str(i) for i in range(101)] + ["1"])
        self.assertEqual([100, 100, 1], [len(c["test"]["Keys"]) for c in db.calls])
        self.assertTrue(all(c["test"]["ConsistentRead"] for c in db.calls))

    def test_unprocessed_reads_fail_after_bounded_retries(self):
        db = FakeDynamo(HistoryTable())
        with patch.object(db, "batch_get_item", side_effect=lambda **kw: {"UnprocessedKeys": kw["RequestItems"]}) as call, patch("career_agent.repository.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "unprocessed"):
                DynamoRepository("test", db).get_listing_details("reed", ["1"])
        self.assertEqual(4, call.call_count)

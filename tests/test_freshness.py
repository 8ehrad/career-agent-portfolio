import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from career_agent.freshness import (DAY, FreshnessService, FreshnessStore, canonical_state, reed_expiration_timestamp,
                                    source_state, target_for, shadow_assess)
from career_agent.freshness_verifiers import NativeVerifier
from career_agent.handlers.freshness_worker import process
from career_agent.models import Job, JobStatus
from career_agent.normalization import normalize_job
from career_agent.source_registry import SourceDefinition

NOW = 1_800_000_000


def job(source="greenhouse", native_id="123", tenant="example"):
    hosts = {"greenhouse": "boards.greenhouse.io", "ashby": "jobs.ashbyhq.com",
             "lever": "jobs.lever.co", "reed": "www.reed.co.uk", "adzuna": "adzuna.co.uk",
             "smartrecruiters": "jobs.smartrecruiters.com", "workday": "example.wd5.myworkdayjobs.com"}
    return normalize_job({"id": native_id, "title": "Data Scientist", "company": "Example",
        "location": "London, UK", "description": "Python and machine learning",
        "url": f"https://{hosts[source]}/{tenant}/jobs/{native_id}"}, source)


def message(target):
    return {"schema_version": 1, "kind": "freshness_check", "customer_id": "personal", "key": target["key"]}


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.store = FreshnessStore()
        self.sqs = Mock()
        self.service = FreshnessService(self.store, sqs=self.sqs, queue_url="queue", clock=lambda: self.now)
        self.job = job()
        self.target = target_for(self.job)

    def row(self):
        return self.store.get("VACANCY#"+self.target["key"])

    def test_identity_stable_across_metadata_and_analysis_versions(self):
        updated = copy.deepcopy(self.job)
        updated.title, updated.description, updated.id = "Senior Data Scientist", "changed", "another-canonical"
        self.assertEqual(self.target["key"], target_for(updated)["key"])
        self.assertNotEqual(self.target["key"], target_for(job(native_id="124"))["key"])

    def test_unknown_and_unsafe_routes_not_guessed(self):
        self.job.source_url = self.job.apply_url = "https://evil.test/example/jobs/123"
        self.assertIsNone(target_for(self.job))
        self.assertIsNone(target_for(job("workday")))
        self.job.source_url = "https://boards.greenhouse.io/../123"
        self.assertIsNone(target_for(self.job))

    def test_disabled_registry_route_is_not_reenabled_from_its_public_url(self):
        self.job.source_key = "greenhouse:example"
        disabled = SourceDefinition("greenhouse:example", "Example", "greenhouse", "example", True, status="disabled")
        self.assertIsNone(target_for(self.job, [disabled]))

    @patch.dict("os.environ", {"FRESHNESS_MODE": "observe"})
    def test_collection_evidence_survives_source_listing_roundtrip(self):
        from career_agent.connectors import GreenhouseConnector, collect_jobs_with_diagnostics
        connector = GreenhouseConnector(("example",))
        with patch.object(GreenhouseConnector, "fetch", return_value=[self.job]):
            jobs, errors, _, diagnostics = collect_jobs_with_diagnostics([connector])
        self.assertFalse(errors)
        self.assertEqual("complete_board", diagnostics[0].coverage_scope)
        restored = Job.from_dict(jobs[0].to_dict())
        self.assertEqual("live_detail", restored.freshness_evidence["outcome"])
        self.assertEqual(restored.freshness_evidence, restored.listings[0].freshness_evidence)

    def test_registry_workday_route_and_identity(self):
        item = job("workday", "R123")
        item.source_key = "workday:example"
        item.source_url = "https://example.wd5.myworkdayjobs.com/Careers/job/London/Data-Scientist_R123"
        definition = SourceDefinition("workday:example", "Example", "workday", "example", True,
                                      config={"host": "example.wd5.myworkdayjobs.com", "site": "Careers"})
        target = target_for(item, [definition])
        self.assertEqual("/job/London/Data-Scientist_R123", target["route"]["external_path"])
        item.source_url = "https://evil.test/Careers/job/London/Data-Scientist_R123"
        self.assertIsNone(target_for(item, [definition]))

    def test_search_sighting_and_cache_read_are_not_live_verification(self):
        self.service.observe(self.target, "live_detail", NOW-DAY*3)
        self.service.observe(self.target, "seen_in_search", NOW)
        self.assertEqual(NOW-3*DAY, self.row()["positive"]["at"])
        self.assertEqual("verification_due", source_state(self.row(), NOW, DAY))
        self.service.observe(self.target, "seen_in_search", NOW)
        self.assertEqual(2, len(self.row()["events"]))

    def test_error_preserves_positive_but_does_not_refresh_it(self):
        self.service.observe(self.target, "live_detail", NOW-100)
        self.service.observe(self.target, "blocked", NOW)
        self.assertEqual(NOW-100, self.row()["positive"]["at"])
        self.assertEqual("verified_current", source_state(self.row(), NOW, DAY))
        self.assertEqual("verification_due", source_state(self.row(), NOW+DAY, DAY))

    def test_missing_identity_and_aggregator_expiry_are_not_closure(self):
        for outcome in ("not_found", "identity_mismatch", "source_closed", "blocked", "check_failed"):
            self.service.observe(self.target, outcome, self.now)
            self.assertNotEqual("confirmed_closed", source_state(self.row(), self.now, DAY))
            self.now += 1

    def test_native_closure_wins_over_aggregator_and_native_reopen(self):
        self.service.observe(self.target, "explicit_closed", NOW-100)
        reed = target_for(job("reed"))
        self.service.observe(reed, "live_detail", NOW)
        rows, _ = self.store.list_rows()
        self.assertEqual("confirmed_closed", canonical_state(rows, NOW, DAY))
        self.service.observe(self.target, "live_detail", NOW+1)
        rows, _ = self.store.list_rows()
        self.assertEqual("verified_current", canonical_state(rows, NOW+1, DAY))

    def test_stale_result_cannot_overwrite_newer_evidence(self):
        self.service.observe(self.target, "explicit_closed", NOW)
        self.service.observe(self.target, "live_detail", NOW-1)
        self.assertEqual("confirmed_closed", source_state(self.row(), NOW, DAY))

    def test_detail_can_supersede_search_in_same_second(self):
        self.service.observe(self.target, "seen_in_search", NOW)
        self.service.observe(self.target, "live_detail", NOW)
        self.assertEqual("verified_current", source_state(self.row(), NOW, DAY))

    def test_dynamo_optimistic_conflict_reloads_and_indexes_resend_time(self):
        from botocore.exceptions import ClientError
        table = Mock()
        base = {"target": self.target, "requested": True, "next_check_at": NOW,
                "sent_at": NOW, "revision": 1}
        table.get_item.side_effect = [{}, {"Item": {"payload": json.dumps(base)}}]
        table.put_item.side_effect = [ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"), {}]
        store = FreshnessStore(table)
        store.mutate("VACANCY#"+self.target["key"], lambda old: {**(old or base), "reason": "test"})
        calls = table.put_item.call_args_list
        self.assertEqual("attribute_not_exists(pk)", calls[0].kwargs["ConditionExpression"])
        self.assertEqual({":r": 1}, calls[1].kwargs["ExpressionAttributeValues"])
        self.assertEqual(f"{NOW+600:012d}#VACANCY#{self.target['key']}", calls[1].kwargs["Item"]["gsi1sk"])
        self.assertTrue(all(c.kwargs["ConsistentRead"] for c in table.get_item.call_args_list))

    def test_dynamo_summary_paginates_and_marks_truncation(self):
        table = Mock()
        row = {"payload": json.dumps({"target": self.target})}
        table.query.side_effect = [{"Items": [row], "LastEvaluatedKey": {"pk": "p", "sk": "s"}}, {"Items": [row]}]
        rows, complete = FreshnessStore(table).list_rows(limit=3)
        self.assertEqual(2, len(rows))
        self.assertTrue(complete)
        self.assertEqual({"pk": "p", "sk": "s"}, table.query.call_args.kwargs["ExclusiveStartKey"])

    @patch.dict("os.environ", {"FRESHNESS_MODE": "observe"})
    def test_operations_counts_are_read_only_and_failure_is_not_zero(self):
        from tests.test_operations import fixture_service
        ops = fixture_service()
        ops.repo._freshness_store = self.store
        self.service.observe(self.target, "live_detail", int(ops.clock().timestamp()))
        before = copy.deepcopy(self.store.rows)
        result = ops.overview()["freshness"]
        self.assertEqual(1, result["observed"])
        self.assertEqual(1, result["state_counts"]["verified_current"])
        self.assertEqual(before, self.store.rows)
        other = fixture_service()
        other.repo._freshness_store = Mock()
        other.repo._freshness_store.list_rows.side_effect = RuntimeError("offline")
        with self.assertLogs("career_agent.operations", level="ERROR"):
            missing = other.overview()["freshness"]
        self.assertEqual("unavailable", missing["status"])
        self.assertNotIn("observed", missing)

    def test_duplicate_dispatch_and_delivery_call_verifier_once(self):
        self.service.assess([self.job], "analysis")
        result = self.service.assess([self.job], "digest")
        self.assertEqual(0, result["check_requested"])
        self.assertEqual(1, self.sqs.send_message.call_count)
        verifier = Mock()
        verifier.check.return_value = {"outcome": "live_detail", "at": NOW+1}
        self.assertEqual("live_detail", process(self.service, message(self.target), verifier)["status"])
        self.assertEqual("duplicate_or_not_due", process(self.service, message(self.target), verifier)["status"])
        self.assertEqual(1, verifier.check.call_count)

    def test_abandoned_lease_recovered_and_old_worker_fenced(self):
        self.service.request(self.target, "test", NOW)
        self.store.mutate("VACANCY#"+self.target["key"], lambda r: {**r, "lease_token": "old", "lease_until": NOW+120})
        verifier = Mock()
        self.assertEqual("duplicate_or_not_due", process(self.service, message(self.target), verifier)["status"])
        self.now += 121
        verifier.check.return_value = {"outcome": "not_found", "at": self.now}
        process(self.service, message(self.target), verifier)
        self.service.observe(self.target, "live_detail", self.now+1, expected_lease="old")
        self.assertNotIn("positive", self.row())
        self.assertGreater(self.row()["next_check_at"], self.now)

    def test_late_board_cache_retries_without_refreshing_positive(self):
        self.service.observe(self.target, "not_found", NOW-10)
        self.store.mutate("VACANCY#"+self.target["key"], lambda r: {**r, "lease_token": "a"})
        self.service.observe(self.target, "live_detail", NOW-100, expected_lease="a")
        self.assertNotIn("positive", self.row())
        self.assertEqual(NOW+3600, self.row()["next_check_at"])

    def test_send_failure_is_durable_and_due_index_not_starved(self):
        self.service.request(self.target, "test", NOW)
        self.sqs.send_message.side_effect = RuntimeError("network")
        with self.assertRaises(RuntimeError):
            self.service.dispatch_due()
        self.assertTrue(self.row()["requested"])
        self.assertEqual([], self.store.due(NOW))
        self.assertEqual(1, len(self.store.due(NOW+600)))

    def test_retry_interest_expires_without_reselection(self):
        self.service.request(self.target, "analysis", NOW)
        self.now += 7*DAY
        self.assertEqual(0, self.service.dispatch_due())
        self.assertFalse(self.row()["requested"])
        self.sqs.send_message.assert_not_called()

    def test_closed_wins_same_second_and_failed_shadow_never_invents_counts(self):
        self.service.observe(self.target, "live_detail", NOW)
        self.service.observe(self.target, "explicit_closed", NOW)
        self.assertEqual("confirmed_closed", source_state(self.row(), NOW, DAY))
        service = Mock()
        service.assess.side_effect = RuntimeError("offline")
        with self.assertLogs("career_agent.freshness", level="ERROR"):
            result = shadow_assess(service, [self.job], "test")
        self.assertEqual("unavailable", result["status"])
        self.assertNotIn("would_hold", result)

    def test_collection_reuses_actual_time_only_for_monitored_jobs(self):
        self.service.request(self.target, "test", NOW)
        self.job.freshness_evidence = {"outcome": "live_detail", "source_id": self.job.source_id,
            "observed_at": datetime.fromtimestamp(NOW-100, UTC).isoformat()}
        result = self.service.observe_collection([self.job, job(native_id="999")])
        self.assertEqual(1, result["observations_reused"])
        self.assertEqual(NOW-100, self.row()["positive"]["at"])
        self.assertEqual(0, self.service.assess([self.job], "analysis")["would_hold"])
        self.sqs.send_message.assert_not_called()

    def test_summary_units_bounds_zero_and_count_reconciliation(self):
        empty = self.service.summary()
        self.assertEqual(0, empty["state_counts"]["confirmed_closed"])
        for i in range(3):
            self.service.request(target_for(job(native_id=str(i))), "test", NOW)
        result = self.service.summary()
        self.assertEqual(len(result["items"]), result["observed"])
        self.assertEqual(3, result["pending"])
        self.assertEqual("lower_bound", self.service.summary(limit=2)["count_kind"])

    def test_tracked_jobs_rotate_beyond_first_page(self):
        jobs = [job(native_id=str(i)) for i in range(3)]
        for i, item in enumerate(jobs):
            item.id = str(i)
            item.status = JobStatus.SHORTLISTED
        self.service.refresh_tracked(jobs, limit=1)
        target = target_for(jobs[0])
        self.service.observe(target, "unsupported", NOW)
        self.service.refresh_tracked(jobs, limit=1)
        self.assertIsNotNone(self.store.get("VACANCY#"+target_for(jobs[1])["key"]))

    def test_shared_rate_daily_budget_and_circuit_breaker(self):
        self.assertTrue(self.store.permit_request("reed", NOW, 2))
        self.assertFalse(self.store.permit_request("reed", NOW, 2))
        self.assertTrue(self.store.permit_request("reed", NOW+2, 2))
        self.assertFalse(self.store.permit_request("ashby", NOW+3, 2))
        self.store.block("ashby", NOW+DAY*2)
        self.assertFalse(self.store.permit_request("ashby", NOW+DAY, 2))

    def test_concurrent_budget_never_over_reserves(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.store.permit_request(str(i), NOW, 7), range(50)))
        self.assertEqual(7, sum(results))

    def test_invalid_message_is_rejected_before_external_request(self):
        verifier = Mock()
        for patch_value in ({"key": "https://evil.test"}, {"customer_id": "other"}, {"schema_version": 99}):
            with self.assertRaises(ValueError):
                process(self.service, {**message(self.target), **patch_value}, verifier)
        verifier.check.assert_not_called()

    def test_timestamp_does_not_invalidate_fit_cache_and_old_job_roundtrips(self):
        from career_agent.analysis_cache import analysis_identity
        from career_agent.enrichment import RuleBasedAnalyzer
        from career_agent.profile import load_profile
        before = analysis_identity(self.job, load_profile(), RuleBasedAnalyzer())
        self.job.freshness_evidence = {"outcome": "live_detail", "observed_at": "2026-09-21T12:00:00Z"}
        self.assertEqual(before, analysis_identity(self.job, load_profile(), RuleBasedAnalyzer()))
        restored = Job.from_dict(self.job.to_dict())
        self.assertEqual(self.job.freshness_evidence, restored.freshness_evidence)
        payload = self.job.to_dict()
        payload.pop("freshness_evidence")
        self.assertIsNone(Job.from_dict(payload).freshness_evidence)

    def test_observation_does_not_change_pipeline_recommendations(self):
        from tests.test_pipeline import PipelineTests
        from tests.helpers import fixture_jobs
        from career_agent.repository import MemoryRepository
        from career_agent.digest import MemoryEmailSender
        outputs = []
        for enabled in (False, True):
            sender = MemoryEmailSender()
            pipeline = PipelineTests()._pipeline(MemoryRepository(), sender, [j.to_dict() for j in fixture_jobs()])
            if enabled:
                pipeline.freshness_service = self.service
            report, jobs = pipeline.run(run_id=f"shadow-{enabled}")
            outputs.append(([j.id for j in jobs], report.digest_count, sender.sent[0][1].text_body))
            if enabled:
                self.assertGreater(report.stage_counts["freshness_digest_would_hold"], 0)
        self.assertEqual(outputs[0], outputs[1])


class VerifierTests(unittest.TestCase):
    def test_atomic_pacing_race_waits_briefly_instead_of_deferring_thirty_minutes(self):
        store = FreshnessStore()
        clock = [NOW]
        sleeps = []
        original = store.permit_request
        calls = [0]
        def raced(source, now, daily_limit):
            calls[0] += 1
            if calls[0] == 1:
                self.assertTrue(original(source, now, daily_limit))  # Other worker wins immediately before us.
            return original(source, now, daily_limit)
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        transport = Mock(return_value=(200, {"jobs": [{"id": 123, "title": "Data Scientist"}]}))
        with patch.object(store, "permit_request", side_effect=raced):
            result = NativeVerifier(store, transport, clock=lambda: clock[0], sleep=sleep).check(target_for(job()))
        self.assertEqual("live_detail", result["outcome"])
        self.assertEqual([2], sleeps)
        self.assertEqual(1, transport.call_count)

    def check(self, source, payload, code=200, native_id="123"):
        self.store = FreshnessStore()
        self.transport = Mock(return_value=(code, payload))
        verifier = NativeVerifier(self.store, self.transport, clock=lambda: NOW, reed_budget=lambda: True)
        target = target_for(job(source, native_id))
        return verifier, target, verifier.check(target)

    def test_greenhouse_board_reused_for_two_jobs_without_new_timestamp(self):
        verifier, target, result = self.check("greenhouse", {"jobs": [
            {"id": 123, "title": "Data Scientist"}, {"id": 456, "title": "Data Scientist"}]})
        self.assertEqual("live_detail", result["outcome"])
        verifier.clock = lambda: NOW+100
        result = verifier.check({**target, "native_id": "456"})
        self.assertEqual(NOW, result["at"])
        self.assertEqual(1, self.transport.call_count)

    def test_ashby_unlisted_is_not_closed(self):
        _, _, result = self.check("ashby", {"jobs": [{"jobUrl": "https://jobs.ashbyhq.com/example/123",
            "title": "Data Scientist", "isListed": False}]})
        self.assertEqual("live_detail", result["outcome"])

    def test_lever_and_smartrecruiters_identity(self):
        for source, payload in (("lever", [{"id": "123", "text": "Data Scientist"}]),
                                ("smartrecruiters", {"id": "123", "name": "Data Scientist"})):
            _, _, result = self.check(source, payload)
            self.assertEqual("live_detail", result["outcome"])

    def test_missing_and_malformed_boards_are_not_closed(self):
        for payload, expected in (({"jobs": []}, "not_found"), ({"jobs": {}}, "check_failed"),
            ({"jobs": [{"id": 123, "title": "Other"}]}, "identity_mismatch")):
            _, _, result = self.check("greenhouse", payload)
            self.assertEqual(expected, result["outcome"])

    def test_http_failures_and_breaker_prevent_repeated_requests(self):
        for code, expected in ((404, "not_found"), (410, "not_found"), (403, "blocked"),
                               (429, "blocked"), (500, "check_failed"), (0, "check_failed")):
            verifier, target, result = self.check("greenhouse", None, code)
            self.assertEqual(expected, result["outcome"])
            verifier.clock = lambda: NOW+10
            if code not in {404, 410}:
                self.assertEqual("deferred", verifier.check(target)["outcome"])
                self.assertEqual(1, self.transport.call_count)

    @patch.dict("os.environ", {"REED_API_KEY": "test-only"})
    def test_reed_expiry_identity_and_shared_budget(self):
        payload = {"jobId": 123, "jobTitle": "Data Scientist", "employerName": "Example",
                   "jobDescription": "Full description", "expirationDate": "2000-01-01T00:00:00Z"}
        verifier, target, result = self.check("reed", payload)
        self.assertEqual("source_closed", result["outcome"])
        verifier.clock = lambda: NOW+3
        verifier.reed_budget = lambda: False
        self.assertEqual("deferred", verifier.check(target)["outcome"])
        self.assertEqual(1, self.transport.call_count)

    @patch.dict("os.environ", {"REED_API_KEY": "test-only"})
    def test_reed_real_html_entities_and_british_date(self):
        payload = {"jobId": 123, "jobTitle": "Data Scientist", "employerName": "Data &amp; AI",
                   "jobDescription": "Full description", "expirationDate": "08/09/2026"}
        store = FreshnessStore()
        target = {**target_for(job("reed")), "company": "Data & AI"}
        now = int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())
        verifier = NativeVerifier(store, Mock(return_value=(200, payload)), clock=lambda: now, reed_budget=lambda: True)
        self.assertEqual("source_closed", verifier.check(target)["outcome"])
        self.assertEqual(int(datetime(2026, 9, 8, 22, 59, 59, tzinfo=UTC).timestamp()),
                         reed_expiration_timestamp("08/09/2026"))

    def test_workday_native_exact_id_and_title(self):
        store = FreshnessStore()
        target = {"source": "workday", "tenant": "example", "native_id": "R123", "title": "Data Scientist",
                  "route": {"host": "example.wd5.myworkdayjobs.com", "site": "Careers", "external_path": "/job/UK/Data_R123"}}
        transport = Mock(return_value=(200, {"jobPostingInfo": {"jobReqId": "R123", "title": "Data Scientist"}}))
        result = NativeVerifier(store, transport, clock=lambda: NOW).check(target)
        self.assertEqual("live_detail", result["outcome"])
        self.assertEqual("https://example.wd5.myworkdayjobs.com/wday/cxs/example/Careers/job/UK/Data_R123", transport.call_args.args[0])

    def test_adzuna_has_no_automated_browser_or_redirect_fallback(self):
        _, _, result = self.check("adzuna", {})
        self.assertEqual("unsupported", result["outcome"])
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()

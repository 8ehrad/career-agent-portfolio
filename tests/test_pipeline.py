from __future__ import annotations

import unittest
from datetime import UTC, datetime

from career_agent.connectors import FixtureConnector, SourceFetchDiagnostic
from career_agent.digest import MemoryEmailSender
from career_agent.enrichment import RuleBasedAnalyzer, SalaryEnricher
from career_agent.models import JobStatus
from career_agent.normalization import normalize_job
from career_agent.pipeline import CareerPipeline, DiscoverySeed
from career_agent.profile import load_profile
from career_agent.repository import MemoryRepository
from tests.helpers import fixture_jobs


class PipelineTests(unittest.TestCase):
    def test_persisted_source_diagnostic_round_trips_detail_metrics(self):
        persisted = {
            "source_key": "reed",
            "source": "reed",
            "employer": None,
            "ats": "reed",
            "tenant": None,
            "started_at": "2026-09-18T15:27:14+00:00",
            "completed_at": "2026-09-18T15:28:14+00:00",
            "duration_ms": 60_000,
            "status": "healthy",
            "listings_fetched": 1_455,
            "error": None,
            "detail_metrics": {"cache_hits": 0, "requests": 100, "stored": 100},
        }

        restored = SourceFetchDiagnostic(**persisted)

        self.assertEqual(persisted, restored.to_dict())

    def _pipeline(self, repository, sender, fixtures):
        profile = load_profile()
        return CareerPipeline(
            profile=profile,
            repository=repository,
            connectors=[FixtureConnector(fixtures)],
            fast_analyzer=RuleBasedAnalyzer(),
            deep_analyzer=RuleBasedAnalyzer(),
            salary_enricher=SalaryEnricher(repository),
            email_sender=sender,
            dashboard_url="https://career.example.com",
            deep_analysis_limit=10,
        )

    def test_recurring_digest_includes_still_open_matches(self):
        repository = MemoryRepository()
        sender = MemoryEmailSender()
        fixtures = [job.to_dict() for job in fixture_jobs()]
        pipeline = self._pipeline(repository, sender, fixtures)

        first_report, _ = pipeline.run(run_id="recurring-first")
        digest_key = f"digest#{datetime.now(UTC).date().isoformat()}"
        repository.release_claim(digest_key)
        sender.sent.clear()  # simulate the next schedule date in this clock-bound unit test
        second_report, second_new_jobs = pipeline.run(run_id="recurring-second")

        self.assertTrue(first_report.emailed)
        self.assertEqual("baseline_estimate", first_report.newness_scope)
        self.assertEqual(first_report.unique, first_report.new_stage_counts["canonical_unique"])
        self.assertEqual(5, first_report.stage_counts["qualified_shortlist"])
        self.assertEqual(10, first_report.source_metrics["fixture"]["collected_listings"])
        self.assertIn("northstar fintech", first_report.employer_metrics)
        self.assertIn("fixture", first_report.registry_source_metrics)
        self.assertEqual("healthy", repository.get_source_health("fixture").status)
        self.assertTrue(second_report.emailed)
        self.assertEqual("exact", second_report.newness_scope)
        self.assertEqual(0, second_report.new_stage_counts["canonical_unique"])
        self.assertEqual(0, second_report.new_stage_counts["fast_analyzed"])
        self.assertEqual([], second_new_jobs)
        self.assertGreater(second_report.digest_count, 0)
        self.assertIn("STILL OPEN", sender.sent[-1][1].text_body)

    def test_empty_successful_run_still_sends_health_digest(self):
        repository = MemoryRepository()
        sender = MemoryEmailSender()
        pipeline = self._pipeline(repository, sender, [])

        report, jobs = pipeline.run(run_id="empty-run")

        self.assertEqual([], jobs)
        self.assertTrue(report.emailed)
        self.assertEqual(0, report.digest_count)
        self.assertIn("no qualified matches", sender.sent[0][1].subject.lower())
        self.assertEqual(0, report.stage_counts["canonical_unique"])

    def test_precollected_discovery_is_merged_into_pipeline_telemetry(self):
        repository = MemoryRepository()
        sender = MemoryEmailSender()
        workday_job = fixture_jobs()[0]
        workday_job.source = "workday"
        diagnostic = SourceFetchDiagnostic(
            source_key="workday:example",
            source="workday",
            employer="Example",
            ats="workday",
            tenant="example",
            started_at="2026-09-07T08:00:00+00:00",
            completed_at="2026-09-07T08:01:00+00:00",
            duration_ms=60_000,
            status="healthy",
            listings_fetched=1,
        )
        pipeline = self._pipeline(repository, sender, [])

        report, _ = pipeline.run(
            run_id="seeded",
            send_email=False,
            discovery_seed=DiscoverySeed(
                jobs=[workday_job],
                source_fetches=[diagnostic],
                configuration={"asynchronous_workday_ingestion": True},
                timings_ms={"workday_fanout_wall_clock": 60_000},
            ),
        )

        self.assertEqual(1, report.discovered)
        self.assertEqual(1, report.source_counts["workday"])
        self.assertTrue(report.configuration["asynchronous_workday_ingestion"])
        self.assertEqual(60_000, report.timings_ms["workday_fanout_wall_clock"])

    def test_failed_run_persists_last_completed_checkpoint(self):
        class FailingOnceSender:
            def __init__(self):
                self.attempts = 0

            def send(self, recipient, digest, idempotency_key):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("delivery failed")

        repository = MemoryRepository()
        pipeline = self._pipeline(repository, FailingOnceSender(), [job.to_dict() for job in fixture_jobs()])

        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            pipeline.run(run_id="failed-checkpoint")

        report = repository.get_run("failed-checkpoint")
        self.assertEqual("failed", report.status)
        self.assertEqual("ranking_and_history_persistence", report.last_completed_stage)
        self.assertIn("pipeline_unhandled:RuntimeError", report.errors)

        history_counts = {
            job_id: (history.seen_runs, history.fast_analysis_runs, history.deep_analysis_runs)
            for job_id, history in repository.analysis_histories.items()
        }
        retried_report, _ = pipeline.run(run_id="failed-checkpoint")

        self.assertEqual("completed", retried_report.status)
        self.assertEqual(
            history_counts,
            {
                job_id: (history.seen_runs, history.fast_analysis_runs, history.deep_analysis_runs)
                for job_id, history in repository.analysis_histories.items()
            },
        )

    def test_digest_keeps_only_the_strongest_company_title_variant(self):
        profile = load_profile()
        repository = MemoryRepository()
        base = next(job for job in fixture_jobs() if job.source_id == "good-1")
        fixtures = []
        for index, location in enumerate(("Bristol, UK", "Remote, United Kingdom")):
            item = base.to_dict()
            item.update({"id": f"variant-{index}", "source_id": f"variant-{index}", "location": location})
            fixtures.append(item)

        pipeline = CareerPipeline(
            profile=profile,
            repository=repository,
            connectors=[FixtureConnector(fixtures)],
            fast_analyzer=RuleBasedAnalyzer(),
            deep_analyzer=RuleBasedAnalyzer(),
            salary_enricher=SalaryEnricher(repository),
            email_sender=MemoryEmailSender(),
            dashboard_url="https://career.example.com",
            deep_analysis_limit=10,
        )

        report, jobs = pipeline.run(run_id="variant-diversity", send_email=False)

        self.assertEqual(1, report.recommended)
        self.assertEqual(1, len(jobs))

    def test_salary_research_is_capped_per_run(self):
        profile = load_profile()
        repository = MemoryRepository()
        base = next(job for job in fixture_jobs() if job.source_id == "good-1")
        fixtures = []
        for index in range(6):
            item = base.to_dict()
            item.update({"id": f"cap-{index}", "source_id": f"cap-{index}", "company": f"Company {index}"})
            item["salary"].update(
                {
                    "minimum_gbp": None,
                    "maximum_gbp": None,
                    "confidence": "unknown",
                    "source_url": None,
                    "source_label": None,
                    "inferred": False,
                }
            )
            fixtures.append(item)

        class CountingEnricher:
            calls = 0

            def enrich(self, job):
                self.calls += 1
                return job, False

        enricher = CountingEnricher()
        pipeline = CareerPipeline(
            profile=profile,
            repository=repository,
            connectors=[FixtureConnector(fixtures)],
            fast_analyzer=RuleBasedAnalyzer(),
            deep_analyzer=RuleBasedAnalyzer(),
            salary_enricher=enricher,
            email_sender=MemoryEmailSender(),
            dashboard_url="https://career.example.com",
            deep_analysis_limit=3,
            salary_research_limit=3,
        )

        report, _ = pipeline.run(run_id="salary-cap", send_email=False)

        self.assertEqual(3, enricher.calls)
        self.assertEqual(6, report.stage_counts["salary_evaluation_pool"])
        self.assertEqual(3, report.stage_counts["salary_research_selected"])

    def test_unknown_salary_reaches_deep_analysis_and_unverified_digest_section(self):
        job = normalize_job(
            {
                "id": "unknown-salary",
                "title": "LLM Engineer",
                "company": "Plausible AI",
                "location": "Remote — EMEA",
                "description": "Own production LLM systems using Python, AWS and model evaluation.",
                "url": "https://example.test/jobs/unknown-salary",
            },
            "fixture",
        )
        repository = MemoryRepository()
        sender = MemoryEmailSender()
        pipeline = self._pipeline(repository, sender, [job.to_dict()])

        report, recommendations = pipeline.run(run_id="unknown-salary")

        self.assertEqual(1, report.stage_counts["salary_unverified_but_plausible"])
        self.assertEqual(1, report.stage_counts["deep_analysis_attempted"])
        self.assertEqual(1, len(recommendations))
        self.assertIn("STRONG MATCHES—SALARY UNVERIFIED", sender.sent[0][1].text_body)

    def test_deep_analysis_cache_reuses_results_without_spending_fresh_slots(self):
        class CountingAnalyzer(RuleBasedAnalyzer):
            analysis_version = "counting-v1"

            def __init__(self):
                self.calls = 0

            def analyze(self, job, profile):
                self.calls += 1
                return super().analyze(job, profile)

        repository = MemoryRepository()
        analyzer = CountingAnalyzer()
        fixtures = [job.to_dict() for job in fixture_jobs()]
        pipeline = CareerPipeline(
            profile=load_profile(),
            repository=repository,
            connectors=[FixtureConnector(fixtures)],
            fast_analyzer=RuleBasedAnalyzer(),
            deep_analyzer=analyzer,
            salary_enricher=SalaryEnricher(repository),
            email_sender=MemoryEmailSender(),
            dashboard_url="https://career.example.com",
            deep_analysis_limit=10,
        )

        first, _ = pipeline.run(run_id="cache-first", send_email=False)
        calls_after_first = analyzer.calls
        second, _ = pipeline.run(run_id="cache-second", send_email=False)

        self.assertEqual(5, calls_after_first)
        self.assertEqual(calls_after_first, analyzer.calls)
        self.assertEqual(5, first.stage_counts["deep_analysis_cache_writes"])
        self.assertEqual(5, second.stage_counts["deep_analysis_cache_hits"])
        self.assertEqual(0, second.stage_counts["deep_analysis_attempted"])
        self.assertEqual(5, second.stage_counts["deep_analysis_evaluated"])
        self.assertEqual(5, second.stage_counts["qualified_shortlist"])

    def test_analyzer_version_change_invalidates_cached_results(self):
        class VersionedAnalyzer(RuleBasedAnalyzer):
            analysis_version = "version-1"

            def __init__(self):
                self.calls = 0

            def analyze(self, job, profile):
                self.calls += 1
                return super().analyze(job, profile)

        repository = MemoryRepository()
        analyzer = VersionedAnalyzer()
        fixtures = [job.to_dict() for job in fixture_jobs()]
        pipeline = CareerPipeline(
            profile=load_profile(),
            repository=repository,
            connectors=[FixtureConnector(fixtures)],
            fast_analyzer=RuleBasedAnalyzer(),
            deep_analyzer=analyzer,
            salary_enricher=SalaryEnricher(repository),
            email_sender=MemoryEmailSender(),
            dashboard_url="https://career.example.com",
            deep_analysis_limit=10,
        )

        pipeline.run(run_id="version-first", send_email=False)
        analyzer.analysis_version = "version-2"
        second, _ = pipeline.run(run_id="version-second", send_email=False)

        self.assertEqual(10, analyzer.calls)
        self.assertEqual(5, second.stage_counts["deep_analysis_cache_misses_changed"])
        self.assertEqual(5, second.stage_counts["deep_analysis_attempted"])

    def test_fixture_pipeline_recommends_only_viable_jobs_and_sends_once(self):
        profile = load_profile()
        repository = MemoryRepository()
        sender = MemoryEmailSender()
        connector = FixtureConnector([job.to_dict() for job in fixture_jobs()])
        pipeline = CareerPipeline(
            profile=profile,
            repository=repository,
            connectors=[connector],
            fast_analyzer=RuleBasedAnalyzer(),
            deep_analyzer=RuleBasedAnalyzer(),
            salary_enricher=SalaryEnricher(repository),
            email_sender=sender,
            dashboard_url="https://career.example.com",
            deep_analysis_limit=10,
        )
        report, jobs = pipeline.run(run_id="fixture-run")
        self.assertEqual(10, report.discovered)
        self.assertEqual(9, report.unique)
        self.assertEqual(5, report.recommended)
        self.assertTrue(report.emailed)
        self.assertEqual(1, len(sender.sent))
        self.assertTrue(all(job.score.hard_gate_passed for job in jobs))
        self.assertTrue(all((job.salary.midpoint_gbp or 0) >= 55_000 for job in jobs))
        self.assertIn("Northstar Fintech", sender.sent[0][1].html_body)

        duplicate_report, duplicate_jobs = pipeline.run(run_id="fixture-run")
        self.assertEqual(["duplicate_run_ignored"], duplicate_report.errors)
        self.assertEqual("duplicate", duplicate_report.status)
        self.assertEqual([], duplicate_jobs)
        self.assertEqual(1, len(sender.sent))

        first = jobs[0]
        repository.update_job_status(first.id, JobStatus.SHORTLISTED)
        second_report, second_jobs = pipeline.run(run_id="fixture-run-retry")
        self.assertEqual(0, second_report.recommended)
        self.assertFalse(second_report.emailed)
        self.assertEqual([], second_jobs)
        self.assertEqual(JobStatus.SHORTLISTED, repository.get_job(first.id).status)
        self.assertEqual(1, len(sender.sent))
        self.assertEqual(5, len(repository.jobs))


if __name__ == "__main__":
    unittest.main()

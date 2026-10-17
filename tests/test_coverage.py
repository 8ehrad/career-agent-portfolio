from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from career_agent.coverage import evaluate_coverage_samples
from career_agent.models import CoverageSample
from career_agent.repository import MemoryRepository
from tests.helpers import fixture_jobs


class CoverageBenchmarkTests(unittest.TestCase):
    def test_human_sample_is_matched_to_licensed_source_within_24_hours(self):
        repository = MemoryRepository()
        observed = datetime(2026, 9, 6, 9, tzinfo=UTC)
        repository.put_coverage_sample(
            CoverageSample(
                sample_id="sample-1",
                observed_at=observed.isoformat(),
                deadline_at=(observed + timedelta(hours=24)).isoformat(),
                title="Senior Data Scientist",
                company="Northstar Fintech Ltd",
            )
        )

        metrics = evaluate_coverage_samples(repository, fixture_jobs(), observed + timedelta(hours=3))
        stored = repository.list_coverage_samples()[0]

        self.assertEqual("matched_within_24h", stored.status)
        self.assertEqual("fixture", stored.matched_source)
        self.assertEqual(100.0, metrics["recall_within_24h_percent"])

    def test_unmatched_sample_becomes_missed_after_deadline(self):
        repository = MemoryRepository()
        observed = datetime(2026, 9, 6, 9, tzinfo=UTC)
        repository.put_coverage_sample(
            CoverageSample(
                sample_id="sample-2",
                observed_at=observed.isoformat(),
                deadline_at=(observed + timedelta(hours=24)).isoformat(),
                title="ML Scientist",
                company="Missing Employer",
            )
        )

        metrics = evaluate_coverage_samples(repository, fixture_jobs(), observed + timedelta(hours=25))

        self.assertEqual("missed", repository.list_coverage_samples()[0].status)
        self.assertEqual(0.0, metrics["recall_within_24h_percent"])

    def test_same_title_and_company_in_a_different_city_does_not_match(self):
        repository = MemoryRepository()
        observed = datetime(2026, 9, 6, 9, tzinfo=UTC)
        repository.put_coverage_sample(
            CoverageSample(
                sample_id="sample-3",
                observed_at=observed.isoformat(),
                deadline_at=(observed + timedelta(hours=24)).isoformat(),
                title="Senior Data Scientist",
                company="Northstar Fintech",
                location="London, UK",
            )
        )

        metrics = evaluate_coverage_samples(repository, fixture_jobs(), observed + timedelta(hours=3))

        self.assertEqual("pending", repository.list_coverage_samples()[0].status)
        self.assertEqual(0, metrics["resolved_samples"])

    def test_country_level_location_matches_a_uk_city(self):
        repository = MemoryRepository()
        observed = datetime(2026, 9, 6, 9, tzinfo=UTC)
        repository.put_coverage_sample(
            CoverageSample(
                sample_id="sample-4",
                observed_at=observed.isoformat(),
                deadline_at=(observed + timedelta(hours=24)).isoformat(),
                title="Senior Data Scientist",
                company="Northstar Fintech",
                location="United Kingdom",
            )
        )

        evaluate_coverage_samples(repository, fixture_jobs(), observed + timedelta(hours=3))

        self.assertEqual("matched_within_24h", repository.list_coverage_samples()[0].status)


if __name__ == "__main__":
    unittest.main()

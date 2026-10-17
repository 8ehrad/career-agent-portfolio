from __future__ import annotations

import unittest
from unittest.mock import patch

from career_agent.enrichment import BraveSearchProvider, PageFetcher, SalaryEnricher, SearchProvider, SourcePage
from career_agent.models import SalaryConfidence
from career_agent.repository import MemoryRepository
from tests.helpers import fixture_jobs


class FakeSearch(SearchProvider):
    def __init__(self):
        self.calls = 0

    def search(self, query: str, count: int = 5):
        self.calls += 1
        return [
            {"title": "Transient search result", "url": "https://salary.example/1", "description": "£190k - £210k"},
            {"title": "Transient search result", "url": "https://salary.example/2", "description": "£200k - £220k"},
            {"title": "Must not be fetched", "url": "https://salary.example/3", "description": "£250k"},
        ]


class FakePages(PageFetcher):
    def __init__(self, include_salary: bool = True):
        self.include_salary = include_salary
        self.calls: list[str] = []

    def fetch(self, url: str) -> SourcePage:
        self.calls.append(url)
        suffix = "Salary range £76k to £88k" if self.include_salary else "Compensation is competitive"
        return SourcePage(url=url, title="Signal Works Applied Scientist salary", text=suffix)


class EnrichmentTests(unittest.TestCase):
    def test_brave_provider_respects_one_request_per_second_plan(self):
        class FakeTime:
            now = 100.0

            def __init__(self):
                self.sleeps: list[float] = []

            def clock(self):
                return self.now

            def sleep(self, seconds: float):
                self.sleeps.append(seconds)
                self.now += seconds

        fake_time = FakeTime()
        provider = BraveSearchProvider("test-key", clock=fake_time.clock, sleeper=fake_time.sleep)
        with patch("career_agent.enrichment.get_json", return_value={"web": {"results": []}}) as request:
            provider.search("first")
            provider.search("second")

        self.assertEqual(2, request.call_count)
        self.assertEqual(1, len(fake_time.sleeps))
        self.assertGreaterEqual(fake_time.sleeps[0], 1.0)

    def test_searches_and_caches_missing_salary(self):
        repository = MemoryRepository()
        search = FakeSearch()
        pages = FakePages()
        enricher = SalaryEnricher(repository, search, pages)
        job = next(item for item in fixture_jobs() if item.source_id == "good-4")
        job.salary.minimum_gbp = None
        job.salary.maximum_gbp = None
        enriched, searched = enricher.enrich(job)
        self.assertTrue(searched)
        self.assertEqual(SalaryConfidence.HIGH, enriched.salary.confidence)
        self.assertGreaterEqual(enriched.salary.midpoint_gbp or 0, 75_000)
        self.assertLess(enriched.salary.midpoint_gbp or 0, 100_000)
        self.assertEqual("Signal Works Applied Scientist salary", enriched.salary.source_label)
        self.assertNotIn("salary_sources", enriched.company_context)
        self.assertEqual(2, len(pages.calls))
        second, searched_again = enricher.enrich(job)
        self.assertFalse(searched_again)
        self.assertEqual(1, search.calls)

    def test_transient_search_snippets_are_not_used_as_salary_evidence(self):
        repository = MemoryRepository()
        search = FakeSearch()
        pages = FakePages(include_salary=False)
        enricher = SalaryEnricher(repository, search, pages)
        job = next(item for item in fixture_jobs() if item.source_id == "good-4")
        job.salary.minimum_gbp = None
        job.salary.maximum_gbp = None

        enriched, searched = enricher.enrich(job)

        self.assertTrue(searched)
        self.assertIsNone(enriched.salary.midpoint_gbp)
        self.assertIsNone(repository.get_salary_evidence("signal works#applied scientist#uk"))

    def test_generic_company_jobs_page_is_not_salary_evidence(self):
        repository = MemoryRepository()
        search = FakeSearch()

        class GenericJobsPages(PageFetcher):
            def fetch(self, url: str) -> SourcePage:
                return SourcePage(url=url, title="Jobs at Signal Works", text="Salary range £150k to £180k")

        job = next(item for item in fixture_jobs() if item.source_id == "good-4")
        job.salary.minimum_gbp = None
        job.salary.maximum_gbp = None
        enriched, searched = SalaryEnricher(repository, search, GenericJobsPages()).enrich(job)

        self.assertTrue(searched)
        self.assertIsNone(enriched.salary.midpoint_gbp)


if __name__ == "__main__":
    unittest.main()

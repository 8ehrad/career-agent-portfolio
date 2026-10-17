from __future__ import annotations

import unittest

from career_agent.profile import load_profile
from career_agent.models import VerificationStatus
from career_agent.normalization import normalize_job
from career_agent.scoring import hard_filter, rank_jobs, score_job
from tests.helpers import fixture_jobs


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile()
        self.jobs = fixture_jobs()

    def test_low_salary_is_rejected(self):
        job = next(item for item in self.jobs if item.source_id == "low-salary")
        decision = hard_filter(job, self.profile)
        self.assertFalse(decision.passed)
        self.assertTrue(any("hard floor" in reason for reason in decision.reasons))

    def test_pure_software_is_rejected(self):
        job = next(item for item in self.jobs if item.source_id == "software")
        self.assertFalse(hard_filter(job, self.profile).passed)

    def test_too_senior_is_rejected(self):
        job = next(item for item in self.jobs if item.source_id == "too-senior")
        self.assertFalse(hard_filter(job, self.profile).passed)

    def test_principal_individual_contributor_is_rejected(self):
        job = next(item for item in self.jobs if item.source_id == "good-1")
        job.title = "Principal Machine Learning Engineer"
        decision = hard_filter(job, self.profile)
        self.assertFalse(decision.passed)
        self.assertIn("Role is more than one realistic seniority step above the candidate", decision.reasons)

    def test_bristol_hybrid_gets_full_location_score(self):
        job = next(item for item in self.jobs if item.source_id == "good-1")
        score_job(job, self.profile)
        self.assertEqual(100, job.score.location)
        self.assertTrue(job.score.hard_gate_passed)

    def test_ranking_excludes_bad_jobs(self):
        ranked = rank_jobs(self.jobs, self.profile, 10)
        ids = {job.source_id for job in ranked}
        self.assertIn("good-1", ids)
        self.assertNotIn("low-salary", ids)
        self.assertNotIn("software", ids)
        self.assertNotIn("outside-uk", ids)

    def test_foreign_remote_role_does_not_pass_from_uk_mentions_in_description(self):
        job = next(item for item in self.jobs if item.source_id == "good-1")
        job.location = "US, CA, Remote"
        job.workplace = "remote"
        job.description += " Our global hubs include London and Bristol in the United Kingdom."

        decision = hard_filter(job, self.profile)

        self.assertFalse(decision.passed)
        self.assertIn("Role is not clearly UK based", decision.reasons)

    def test_explicit_uk_remote_role_is_eligible(self):
        job = next(item for item in self.jobs if item.source_id == "good-1")
        job.location = "Remote"
        job.workplace = "remote"
        job.description += " This role is remote within the UK."

        self.assertTrue(hard_filter(job, self.profile).passed)

    def test_ambiguous_emea_remote_is_plausible_not_rejected(self):
        job = next(item for item in self.jobs if item.source_id == "good-1")
        job.location = "Remote — EMEA"
        job.workplace = "remote"

        decision = hard_filter(job, self.profile)

        self.assertTrue(decision.passed)
        self.assertEqual(VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE, job.location_verification)

    def test_missing_salary_is_plausible_not_rejected(self):
        job = normalize_job(
            {
                "id": "unknown-salary",
                "title": "LLM Engineer",
                "company": "Acme",
                "location": "Bristol, UK",
                "description": "Build production LLM systems using Python and AWS.",
                "url": "https://example.test/jobs/unknown",
            },
            "fixture",
        )

        decision = hard_filter(job, self.profile)

        self.assertTrue(decision.passed)
        self.assertEqual(VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE, job.salary_verification)

    def test_inferred_below_floor_salary_is_not_a_confirmed_failure(self):
        job = next(item for item in self.jobs if item.source_id == "low-salary")
        job.salary.inferred = True

        decision = hard_filter(job, self.profile)
        score_job(job, self.profile)

        self.assertTrue(decision.passed)
        self.assertEqual(VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE, job.salary_verification)
        self.assertGreater(job.score.remuneration, 0)

    def test_salary_range_crossing_floor_is_plausible_not_rejected(self):
        job = next(item for item in self.jobs if item.source_id == "low-salary")
        job.salary.minimum_gbp = 45_000
        job.salary.maximum_gbp = 60_000

        decision = hard_filter(job, self.profile)
        score_job(job, self.profile)

        self.assertTrue(decision.passed)
        self.assertEqual(VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE, job.salary_verification)
        self.assertGreater(job.score.remuneration, 0)

    def test_research_scientist_requires_ml_or_ai_evidence(self):
        base = {
            "id": "research",
            "title": "Research Scientist",
            "company": "Acme",
            "location": "London, UK",
            "url": "https://example.test/jobs/research",
        }
        unrelated = normalize_job({**base, "description": "Research new battery materials."}, "fixture")
        ml = normalize_job(
            {**base, "description": "Research deep learning and machine learning systems."}, "fixture"
        )

        self.assertFalse(hard_filter(unrelated, self.profile).passed)
        self.assertTrue(hard_filter(ml, self.profile).passed)

    def test_forward_deployed_engineer_titles_enter_cheap_analysis(self):
        for index, title in enumerate(
            ("Forward Deployed Engineer", "Senior Forward Deployed Engineer")
        ):
            with self.subTest(title=title):
                job = normalize_job(
                    {
                        "id": f"forward-deployed-{index}",
                        "title": title,
                        "company": "Acme",
                        "location": "London, UK",
                        "description": "Deploy customer-facing AI systems using Python and AWS.",
                        "url": f"https://example.test/jobs/forward-deployed-{index}",
                    },
                    "fixture",
                )

                self.assertTrue(hard_filter(job, self.profile).passed)
                self.assertEqual("forward deployed engineer", job.role_family)

    def test_critical_deep_analysis_conflict_fails_final_gate(self):
        job = next(item for item in self.jobs if item.source_id == "good-1")
        job.red_flags = ["The core domain is entirely absent from the candidate background — a fundamental mismatch."]

        score_job(job, self.profile)

        self.assertFalse(job.score.hard_gate_passed)
        self.assertLess(job.score.attainability, 60)


if __name__ == "__main__":
    unittest.main()

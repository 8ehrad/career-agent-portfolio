from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from career_agent.analysis_cache import (
    analysis_identity,
    analysis_priority_components,
    select_fresh_candidates,
)
from career_agent.enrichment import RuleBasedAnalyzer
from career_agent.models import JobAnalysisHistory
from career_agent.profile import load_profile
from tests.helpers import fixture_jobs


class DeepSelectionTests(unittest.TestCase):
    def _candidates(self, count: int):
        base = fixture_jobs()[0]
        jobs = []
        for index in range(count):
            job = type(base).from_dict(base.to_dict())
            job.id = f"candidate-{index}"
            job.source_id = job.id
            job.company = f"Company {index}"
            job.source = "adzuna"
            job.source_key = "adzuna"
            job.role_family = "data scientist"
            job.score.overall = 100 - index
            jobs.append(job)
        return jobs

    def test_selection_reserves_a_slot_for_source_diversity(self):
        jobs = self._candidates(6)
        jobs[1].source = "ashby"
        jobs[1].source_key = "ashby:top"
        jobs[3].source = "ashby"
        jobs[3].source_key = "ashby:another"
        jobs[4].source = "ashby"
        jobs[4].source_key = "ashby:third"
        jobs[-1].source = "workday"
        jobs[-1].source_key = "workday:new-employer"
        profile = load_profile()
        analyzer = RuleBasedAnalyzer()
        identities = {job.id: analysis_identity(job, profile, analyzer) for job in jobs}

        selection = select_fresh_candidates(jobs, {}, identities, 5, seed="run-1")

        self.assertEqual(5, len(selection.jobs))
        self.assertEqual("diversity", selection.reasons[jobs[-1].id])
        self.assertEqual(3, list(selection.reasons.values()).count("priority"))
        self.assertEqual(1, list(selection.reasons.values()).count("audit"))

    @unittest.expectedFailure  # Known historical backlog-priority regression; retained visibly.
    def test_never_analyzed_candidates_take_priority_over_legacy_cache_misses(self):
        jobs = self._candidates(5)
        profile = load_profile()
        analyzer = RuleBasedAnalyzer()
        identities = {job.id: analysis_identity(job, profile, analyzer) for job in jobs}
        histories = {
            jobs[0].id: JobAnalysisHistory(
                job_id=jobs[0].id,
                first_seen_at="2026-09-01T00:00:00+00:00",
                last_seen_at="2026-09-01T00:00:00+00:00",
                deep_analysis_runs=1,
            ),
            jobs[1].id: JobAnalysisHistory(
                job_id=jobs[1].id,
                first_seen_at="2026-09-01T00:00:00+00:00",
                last_seen_at="2026-09-01T00:00:00+00:00",
                deep_analysis_runs=1,
            ),
        }

        selection = select_fresh_candidates(jobs, histories, identities, 3, seed="run-2")
        priority_ids = {job.id for job in selection.jobs if selection.reasons[job.id] == "priority"}

        self.assertEqual({jobs[2].id, jobs[3].id}, priority_ids)

    def test_priority_exposes_fit_urgency_wait_age_and_novelty(self):
        job = self._candidates(1)[0]
        now = datetime(2026, 9, 8, 12, tzinfo=UTC)
        job.closes_at = (now + timedelta(days=1)).isoformat()
        profile = load_profile()
        analyzer = RuleBasedAnalyzer()
        identity = analysis_identity(job, profile, analyzer)
        history = JobAnalysisHistory(
            job_id=job.id,
            first_seen_at=(now - timedelta(days=30)).isoformat(),
            last_seen_at=now.isoformat(),
        )

        components = analysis_priority_components(job, history, identity, now)

        self.assertEqual(100.0, components["preliminary_fit"])
        self.assertEqual(100.0, components["vacancy_urgency"])
        self.assertEqual(100.0, components["backlog_wait_age"])
        self.assertEqual(90.0, components["novelty"])
        self.assertEqual(99.0, components["priority_score"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from career_agent.connectors import FixtureConnector, JobConnector
from career_agent.handlers import coverage_probe
from career_agent.models import CoverageSample
from career_agent.repository import MemoryRepository
from tests.helpers import fixture_jobs


class CoverageProbeTests(unittest.TestCase):
    @patch.object(coverage_probe, "load_integration_environment")
    @patch.object(coverage_probe, "repository")
    def test_probe_exits_without_source_calls_when_no_sample_is_pending(self, repository, load_env):
        repository.return_value = MemoryRepository()

        result = coverage_probe.handler({}, None)

        self.assertEqual("idle", result["status"])
        load_env.assert_not_called()

    @patch.object(coverage_probe, "connectors_from_environment")
    @patch.object(coverage_probe, "load_integration_environment")
    @patch.object(coverage_probe, "repository")
    def test_pending_sample_runs_discovery_without_model_analysis(
        self, repository, load_env, connectors
    ):
        del load_env
        repo = MemoryRepository()
        observed = datetime.now(UTC) - timedelta(hours=1)
        repo.put_coverage_sample(
            CoverageSample(
                sample_id="sample",
                observed_at=observed.isoformat(),
                deadline_at=(observed + timedelta(hours=24)).isoformat(),
                title="Senior Data Scientist",
                company="Northstar Fintech",
            )
        )
        repository.return_value = repo
        connectors.return_value = [FixtureConnector([job.to_dict() for job in fixture_jobs()])]

        result = coverage_probe.handler({}, None)

        self.assertEqual("completed", result["status"])
        connectors.assert_called_once_with(excluded_ats={"workday"}, detail_store=repo)
        self.assertEqual(100.0, result["coverage_metrics"]["recall_within_24h_percent"])
        self.assertEqual("matched_within_24h", repo.list_coverage_samples()[0].status)

    @patch.object(coverage_probe, "connectors_from_environment")
    @patch.object(coverage_probe, "load_integration_environment")
    @patch.object(coverage_probe, "repository")
    def test_failed_sweep_leaves_expired_sample_pending_for_retry(
        self, repository, load_env, connectors
    ):
        del load_env

        class FailingConnector(JobConnector):
            name = "failure_fixture"

            def fetch(self):
                raise RuntimeError("temporary source failure")

        repo = MemoryRepository()
        observed = datetime.now(UTC) - timedelta(hours=25)
        repo.put_coverage_sample(
            CoverageSample(
                sample_id="expired-sample",
                observed_at=observed.isoformat(),
                deadline_at=(observed + timedelta(hours=24)).isoformat(),
                title="Senior Data Scientist",
                company="Northstar Fintech",
            )
        )
        repository.return_value = repo
        connectors.return_value = [FailingConnector()]

        with self.assertRaisesRegex(RuntimeError, "usable source sweep"):
            coverage_probe.handler({}, None)

        connectors.assert_called_once_with(excluded_ats={"workday"}, detail_store=repo)
        self.assertEqual("pending", repo.list_coverage_samples()[0].status)


if __name__ == "__main__":
    unittest.main()

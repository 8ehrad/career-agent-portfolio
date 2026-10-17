from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from career_agent.connectors import FixtureConnector  # noqa: E402
from career_agent.digest import MemoryEmailSender  # noqa: E402
from career_agent.enrichment import RuleBasedAnalyzer, SalaryEnricher  # noqa: E402
from career_agent.pipeline import CareerPipeline  # noqa: E402
from career_agent.profile import load_profile  # noqa: E402
from career_agent.repository import MemoryRepository  # noqa: E402


def main() -> None:
    with (ROOT / "tests" / "fixtures" / "jobs.json").open(encoding="utf-8") as handle:
        fixtures = json.load(handle)
    profile = load_profile()
    repository = MemoryRepository()
    email = MemoryEmailSender()
    pipeline = CareerPipeline(
        profile=profile,
        repository=repository,
        connectors=[FixtureConnector(fixtures)],
        fast_analyzer=RuleBasedAnalyzer(),
        deep_analyzer=RuleBasedAnalyzer(),
        salary_enricher=SalaryEnricher(repository),
        email_sender=email,
        dashboard_url="http://localhost:5173",
    )
    report, jobs = pipeline.run(run_id="local-fixture-run")
    print(json.dumps({"report": report.to_dict(), "jobs": [job.to_dict() for job in jobs]}, indent=2))


if __name__ == "__main__":
    main()

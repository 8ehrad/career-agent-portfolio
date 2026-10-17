from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from career_agent.application import EvidenceBasedPackGenerator  # noqa: E402
from career_agent.documents import build_cover_letter_docx, build_cv_docx, scrub_docx_metadata  # noqa: E402
from career_agent.enrichment import RuleBasedAnalyzer  # noqa: E402
from career_agent.normalization import normalize_job  # noqa: E402
from career_agent.profile import load_profile  # noqa: E402
from career_agent.scoring import score_job  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (ROOT / "tests" / "fixtures" / "jobs.json").open(encoding="utf-8") as handle:
        raw = json.load(handle)[1]
    profile = load_profile()
    job = normalize_job(raw, "fixture")
    RuleBasedAnalyzer().analyze(job, profile)
    score_job(job, profile)
    pack = EvidenceBasedPackGenerator().generate(job, profile)
    (args.output_dir / "Example_Candidate_CV.docx").write_bytes(scrub_docx_metadata(build_cv_docx(pack, job, profile)))
    (args.output_dir / "Example_Candidate_Cover_Letter.docx").write_bytes(
        scrub_docx_metadata(build_cover_letter_docx(pack, job, profile))
    )


if __name__ == "__main__":
    main()

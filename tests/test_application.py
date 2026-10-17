from __future__ import annotations

import zipfile
from io import BytesIO
import re
import unittest

from career_agent.application import BedrockPackGenerator, EvidenceBasedPackGenerator, ResilientPackGenerator
from career_agent.enrichment import RuleBasedAnalyzer
from career_agent.models import JobStatus
from career_agent.profile import load_profile
from career_agent.repository import MemoryRepository
from career_agent.scoring import score_job
from career_agent.services import ApplicationService
from career_agent.storage import MemoryArtifactStore
from tests.helpers import fixture_jobs


class ApplicationTests(unittest.TestCase):
    def test_application_pack_is_grounded_and_tracked(self):
        profile = load_profile()
        job = next(item for item in fixture_jobs() if item.source_id == "good-2")
        RuleBasedAnalyzer().analyze(job, profile)
        job.skills.append("Unverified Magic Platform")
        score_job(job, profile)
        repository = MemoryRepository()
        repository.put_job(job)
        store = MemoryArtifactStore()
        service = ApplicationService(repository, profile, EvidenceBasedPackGenerator(), store)
        try:
            import docx  # noqa: F401
        except ImportError:
            self.skipTest("python-docx unavailable in this runtime")
        pack = service.prepare(job.id)
        self.assertTrue(pack.selected_evidence_ids)
        allowed = {item["id"] for item in profile.evidence}
        self.assertTrue(set(pack.selected_evidence_ids) <= allowed)
        self.assertEqual(JobStatus.READY, repository.get_job(job.id).status)
        self.assertIn("cv", pack.artifact_keys)
        self.assertEqual(2, len(store.objects))
        cv_content = store.objects[pack.artifact_keys["cv"]][0]
        with zipfile.ZipFile(BytesIO(cv_content)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertNotIn("Unverified Magic Platform", document_xml)
            self.assertIn("Machine Learning", document_xml)
        for content, _ in store.objects.values():
            self.assertTrue(content.startswith(b"PK"))
            with zipfile.ZipFile(BytesIO(content)) as archive:
                core = archive.read("docProps/core.xml").decode("utf-8")
                self.assertIn("<dc:creator></dc:creator>", core)
                for name in archive.namelist():
                    if name.startswith("word/") and name.endswith(".xml"):
                        self.assertIsNone(
                            re.search(r'\s+w:rsid[A-Za-z]+="[0-9A-Fa-f]+"', archive.read(name).decode("utf-8"))
                        )

    def test_invalid_model_pack_uses_grounded_fallback(self):
        profile = load_profile()
        job = next(item for item in fixture_jobs() if item.source_id == "good-1")
        job.skills.insert(0, "Unverified Magic Platform")

        class BrokenGenerator:
            def generate(self, job, profile):
                raise ValueError("unsupported claim")

        generator = ResilientPackGenerator(BrokenGenerator(), EvidenceBasedPackGenerator())
        pack = generator.generate(job, profile)
        allowed = {item["id"] for item in profile.evidence}
        self.assertTrue(pack.selected_evidence_ids)
        self.assertTrue(set(pack.selected_evidence_ids) <= allowed)
        self.assertNotIn("Unverified Magic Platform", str(pack.to_dict()))

    def test_failed_artifact_write_restores_previous_status(self):
        try:
            import docx  # noqa: F401
        except ImportError:
            self.skipTest("python-docx unavailable in this runtime")

        profile = load_profile()
        job = next(item for item in fixture_jobs() if item.source_id == "good-1")
        job.status = JobStatus.SHORTLISTED
        repository = MemoryRepository()
        repository.put_job(job)

        class BrokenStore:
            def put(self, key, content, content_type):
                raise RuntimeError("storage unavailable")

            def signed_url(self, key, expires_seconds=900):
                raise RuntimeError("storage unavailable")

        service = ApplicationService(repository, profile, EvidenceBasedPackGenerator(), BrokenStore())
        with self.assertRaises(RuntimeError):
            service.prepare(job.id)
        self.assertEqual(JobStatus.SHORTLISTED, repository.get_job(job.id).status)

    def test_model_generated_signature_is_replaced_with_canonical_identity(self):
        profile = load_profile()
        job = next(item for item in fixture_jobs() if item.source_id == "good-1")
        evidence_id = profile.evidence[0]["id"]

        class FakeBedrock:
            def converse(self, **kwargs):
                payload = {
                    "cv_headline": "Data Scientist",
                    "cv_summary": "Evidence-grounded candidate.",
                    "selected_evidence_ids": [evidence_id],
                    "tailored_bullets": [profile.evidence[0]["claim"]],
                    "cover_letter": "Dear Hiring Team,\n\nA grounded letter.\n\nExample Wrongname\nwrong@example.com",
                    "recruiter_message": "A grounded message.",
                    "interview_topics": [],
                    "gaps_to_address": [],
                }
                import json

                return {"output": {"message": {"content": [{"text": json.dumps(payload)}]}}}

        pack = BedrockPackGenerator("test-model", FakeBedrock()).generate(job, profile)
        self.assertNotIn("Wrongname", pack.cover_letter)
        self.assertTrue(pack.cover_letter.endswith("Kind regards,\nExample Candidate"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from .application import ApplicationPackGenerator
from .documents import build_cover_letter_docx, build_cv_docx, scrub_docx_metadata
from .models import ApplicationPack, JobStatus
from .profile import CandidateProfile
from .repository import Repository
from .storage import ArtifactStore


class ApplicationService:
    def __init__(
        self,
        repository: Repository,
        profile: CandidateProfile,
        generator: ApplicationPackGenerator,
        artifact_store: ArtifactStore,
    ) -> None:
        self.repository = repository
        self.profile = profile
        self.generator = generator
        self.artifact_store = artifact_store

    def prepare(self, job_id: str, *, expected_identity: str | None = None) -> ApplicationPack:
        job = self.repository.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if expected_identity is not None and (not hasattr(self.generator, "identity") or
                self.generator.identity(job, self.profile) != expected_identity or
                job.status != JobStatus.PREPARING):
            raise ValueError("Application request no longer matches the preparing job")
        existing = self.repository.get_application_pack(job_id)
        if existing and hasattr(self.generator, "identity"):
            reusable = existing.generation_metadata.get("generation_mode") == "rich_model"
            if reusable and existing.generation_metadata.get("identity") == self.generator.identity(job, self.profile):
                return existing
        previous_status = job.status
        self.repository.update_job_status(job_id, JobStatus.PREPARING)
        try:
            pack = self.generator.generate(job, self.profile)
            if expected_identity is not None:
                current = self.repository.get_job(job_id)
                if (current is None or current.status != JobStatus.PREPARING or
                        self.generator.identity(current, self.profile) != expected_identity):
                    raise ValueError("Job changed while application documents were being prepared")
            from uuid import uuid4
            generation = uuid4().hex
            cv_key = f"applications/{job_id}/{generation}/Example_Candidate_CV.docx"
            letter_key = f"applications/{job_id}/{generation}/Example_Candidate_Cover_Letter.docx"
            cv_bytes = scrub_docx_metadata(build_cv_docx(pack, job, self.profile))
            letter_bytes = scrub_docx_metadata(build_cover_letter_docx(pack, job, self.profile))
            self.artifact_store.put(
                cv_key,
                cv_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            self.artifact_store.put(
                letter_key,
                letter_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            pack.artifact_keys = {"cv": cv_key, "cover_letter": letter_key}
            self.repository.put_application_pack(pack)
            self.repository.update_job_status(job_id, JobStatus.READY)
            return pack
        except Exception:
            current = self.repository.get_job(job_id)
            if current and current.status == JobStatus.PREPARING:
                self.repository.update_job_status(job_id, previous_status)
            raise

    def download_urls(self, job_id: str) -> dict[str, str]:
        pack = self.repository.get_application_pack(job_id)
        if pack is None:
            raise KeyError(job_id)
        return {name: self.artifact_store.signed_url(key) for name, key in pack.artifact_keys.items()}

    def pack_freshness(self, job_id: str, pack: ApplicationPack) -> dict:
        if not hasattr(self.generator, "identity"):
            return {"state": "legacy_unversioned", "requires_regeneration": False}
        job = self.repository.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        current = self.generator.identity(job, self.profile)
        stale = pack.generation_metadata.get("identity") != current
        fallback = pack.generation_metadata.get("generation_mode") == "rich_extractive"
        return {"state": "outdated" if stale else "current", "requires_regeneration": stale,
                "fallback_draft": fallback, "requires_human_review": True}

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from .analysis_budget import AnalysisBudgetLimits

from .analysis_cache import (
    AnalysisIdentity,
    DEEP_SELECTION_POLICY_VERSION,
    DIVERSITY_PERCENT,
    PRIORITY_PERCENT,
    analysis_identity,
    analysis_state,
    apply_record,
    record_from_job,
    select_fresh_candidates,
    vacancy_is_closed,
)
from .async_analysis import AnalysisDispatcher
from .connectors import (
    JobConnector,
    SourceFetchDiagnostic,
    collect_jobs_with_diagnostics,
    deduplicate_jobs,
    discover_ats_connectors,
)
from .coverage import evaluate_coverage_samples
from .digest import EmailSender, render_digest
from .enrichment import JobAnalyzer, SalaryEnricher
from .models import (
    AnalysisRun,
    AnalysisTask,
    Job,
    JobAnalysisHistory,
    JobStatus,
    RunReport,
    SourceHealth,
    VerificationStatus,
)
from .normalization import SALARY_PARSER_VERSION, canonical_company, canonical_title
from .profile import CandidateProfile
from .repository import Repository
from .scoring import hard_filter, rank_jobs, score_job


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DiscoverySeed:
    """Results collected before the scoring Lambda starts, such as Workday fan-out output."""

    jobs: list[Job] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_fetches: list[SourceFetchDiagnostic] = field(default_factory=list)
    configuration: dict[str, Any] = field(default_factory=dict)
    timings_ms: dict[str, int] = field(default_factory=dict)


class CareerPipeline:
    def __init__(
        self,
        *,
        profile: CandidateProfile,
        repository: Repository,
        connectors: list[JobConnector],
        fast_analyzer: JobAnalyzer,
        deep_analyzer: JobAnalyzer,
        salary_enricher: SalaryEnricher,
        email_sender: EmailSender,
        dashboard_url: str,
        deep_analysis_limit: int = 25,
        digest_limit: int = 10,
        salary_research_limit: int = 20,
        discovery_excluded_ats: set[str] | frozenset[str] = frozenset(),
        analysis_dispatcher: AnalysisDispatcher | None = None,
        freshness_service=None,
    ) -> None:
        self.profile = profile
        self.repository = repository
        self.connectors = connectors
        self.fast_analyzer = fast_analyzer
        self.deep_analyzer = deep_analyzer
        self.salary_enricher = salary_enricher
        self.email_sender = email_sender
        self.dashboard_url = dashboard_url
        self.deep_analysis_limit = deep_analysis_limit
        self.digest_limit = digest_limit
        self.salary_research_limit = salary_research_limit
        self.discovery_excluded_ats = frozenset(discovery_excluded_ats)
        self.analysis_dispatcher = analysis_dispatcher
        self.freshness_service = freshness_service

    def run(
        self,
        run_id: str | None = None,
        send_email: bool = True,
        discovery_seed: DiscoverySeed | None = None,
    ) -> tuple[RunReport, list[Job]]:
        now = datetime.now(UTC)
        run_id = run_id or f"{now.date().isoformat()}-{uuid.uuid4().hex[:8]}"
        claim_key = f"run#{run_id}"
        if not self.repository.claim_once(claim_key):
            report = RunReport(
                run_id=run_id,
                started_at=now.isoformat(),
                completed_at=now.isoformat(),
                status="duplicate",
                last_completed_stage="claim",
                errors=["duplicate_run_ignored"],
            )
            return report, []
        try:
            if self.analysis_dispatcher is not None:
                existing_analysis_run = self.repository.get_analysis_run(run_id)
                if (
                    existing_analysis_run is not None
                    and existing_analysis_run.status != "completed"
                ):
                    report = self.repository.get_run(run_id) or RunReport(
                        run_id=run_id, started_at=existing_analysis_run.started_at
                    )
                    report.status = "analysis_pending"
                    report.completed_at = None
                    report.last_completed_stage = "deep_analysis_dispatched"
                    self.repository.put_run(report)
                    self.analysis_dispatcher.enqueue(existing_analysis_run)
                    return report, []
            return self._run_claimed(now, run_id, send_email, discovery_seed or DiscoverySeed())
        except Exception as exc:
            # An unexpected crash should remain retryable. Successful runs keep
            # the claim so at-least-once scheduler delivery cannot repeat spend.
            report = self.repository.get_run(run_id) or RunReport(run_id=run_id, started_at=now.isoformat())
            report.status = "failed"
            report.completed_at = datetime.now(UTC).isoformat()
            report.errors.append(f"pipeline_unhandled:{type(exc).__name__}")
            self.repository.put_run(report)
            self.repository.release_claim(claim_key)
            raise

    def _run_claimed(
        self,
        now: datetime,
        run_id: str,
        send_email: bool,
        discovery_seed: DiscoverySeed,
    ) -> tuple[RunReport, list[Job]]:
        pipeline_started = monotonic()
        report = RunReport(
            run_id=run_id,
            started_at=now.isoformat(),
            configuration={
                "profile_version": self.profile.data.get("version", "unknown"),
                "connector_count": len(self.connectors),
                "connector_types": sorted({connector.name for connector in self.connectors}),
                "source_searches": [connector.telemetry_configuration() for connector in self.connectors],
                "deep_analysis_limit": self.deep_analysis_limit,
                "deep_analysis_fresh_limit": self.deep_analysis_limit,
                "deep_analysis_cache_enabled": True,
                "asynchronous_deep_analysis": self.analysis_dispatcher is not None,
                "deep_analysis_analyzer_version": str(
                    getattr(self.deep_analyzer, "analysis_version", "job-fit-v1")
                ),
                "deep_analysis_model_id": str(
                    getattr(self.deep_analyzer, "model_id", type(self.deep_analyzer).__name__)
                ),
                "deep_selection_policy": {
                    "version": DEEP_SELECTION_POLICY_VERSION,
                    "priority_percent": PRIORITY_PERCENT,
                    "diversity_percent": DIVERSITY_PERCENT,
                    "audit_percent": 100 - PRIORITY_PERCENT - DIVERSITY_PERCENT,
                },
                "digest_limit": self.digest_limit,
                "salary_research_limit": self.salary_research_limit,
                "salary_floor_gbp": self.profile.preferences["exceptional_move_floor_gbp"],
                "salary_parser_version": SALARY_PARSER_VERSION,
                "email_requested": send_email,
                **discovery_seed.configuration,
            },
        )
        report.timings_ms.update(discovery_seed.timings_ms)
        source_metrics: dict[str, Counter[str]] = {}

        discovery_started = monotonic()
        jobs = list(discovery_seed.jobs)
        errors = list(discovery_seed.errors)
        source_fetches = list(discovery_seed.source_fetches)
        source_counts: dict[str, int] = {}
        for fetch in source_fetches:
            source_counts[fetch.source] = source_counts.get(fetch.source, 0) + fetch.listings_fetched
        fetched_jobs, fetched_errors, fetched_counts, fetched_diagnostics = collect_jobs_with_diagnostics(
            self.connectors
        )
        jobs.extend(fetched_jobs)
        errors.extend(fetched_errors)
        source_fetches.extend(fetched_diagnostics)
        for source, count in fetched_counts.items():
            source_counts[source] = source_counts.get(source, 0) + count
        discovered_connectors = discover_ats_connectors(
            jobs, self.connectors, excluded_ats=self.discovery_excluded_ats
        )
        if discovered_connectors:
            discovered_jobs, discovered_errors, discovered_counts, discovered_fetches = (
                collect_jobs_with_diagnostics(discovered_connectors)
            )
            jobs.extend(discovered_jobs)
            errors.extend(discovered_errors)
            source_fetches.extend(discovered_fetches)
            for source, count in discovered_counts.items():
                source_counts[source] = source_counts.get(source, 0) + count
        if not self.connectors and not discovery_seed.source_fetches:
            errors.append("configuration:no_job_connectors_enabled")
        report.discovered = len(jobs)
        collected_jobs = list(jobs)
        if self.freshness_service is not None:
            try:
                report.configuration["freshness_collection"] = self.freshness_service.observe_collection(collected_jobs)
            except Exception:
                logging.exception("Freshness collection observations unavailable")
        report.source_counts = source_counts
        report.source_fetches = [item.to_dict() for item in source_fetches]
        report.errors.extend(errors)
        report.stage_counts["collected_listings"] = len(jobs)
        report.stage_counts["source_failures"] = len(errors)
        for source, count in source_counts.items():
            source_metrics.setdefault(source, Counter())["collected_listings"] += count
        report.timings_ms["discovery"] = _elapsed_ms(discovery_started)
        report.source_metrics = _serialize_source_metrics(source_metrics)
        self._checkpoint(report, "discovery")

        normalization_started = monotonic()
        jobs = deduplicate_jobs(jobs)
        report.unique = len(jobs)
        report.stage_counts["canonical_unique"] = len(jobs)
        report.coverage_metrics = evaluate_coverage_samples(self.repository, jobs, now)
        telemetry_baseline = not self.repository.analysis_history_initialized()
        report.newness_scope = "baseline_estimate" if telemetry_baseline else "exact"
        histories = self.repository.get_analysis_histories([job.id for job in jobs])
        if telemetry_baseline:
            _backfill_recommended_histories(histories, self.repository.list_jobs())
        new_unique_ids = {job.id for job in jobs if job.id not in histories}
        report.new_stage_counts["canonical_unique"] = len(new_unique_ids)
        report.stage_counts["previously_seen_canonical_jobs"] = len(jobs) - len(new_unique_ids)
        for job in jobs:
            metrics = source_metrics.setdefault(job.source, Counter())
            metrics["canonical_unique"] += 1
            if job.id in new_unique_ids:
                metrics["new_canonical_jobs"] += 1
        report.timings_ms["normalization_and_history_lookup"] = _elapsed_ms(normalization_started)
        report.source_metrics = _serialize_source_metrics(source_metrics)
        self._checkpoint(report, "normalization_and_history_lookup")

        filtering_started = monotonic()
        plausible: list[Job] = []
        fast_analyzed_ids: set[str] = set()
        new_fast_analyzed_ids: set[str] = set()
        rejection_counts: Counter[str] = Counter()
        for job in jobs:
            decision = hard_filter(job, self.profile)
            if decision.passed:
                plausible.append(self.fast_analyzer.analyze(job, self.profile))
                fast_analyzed_ids.add(job.id)
                source_metrics.setdefault(job.source, Counter())["hard_filter_passed"] += 1
                source_metrics[job.source]["fast_analyzed"] += 1
                if job.id not in histories or histories[job.id].fast_analysis_runs == 0:
                    new_fast_analyzed_ids.add(job.id)
                    source_metrics[job.source]["new_fast_analyzed"] += 1
            else:
                job.red_flags.extend(decision.reasons)
                source_metrics.setdefault(job.source, Counter())["hard_filter_rejected"] += 1
                for reason in decision.reasons:
                    rejection_counts[f"hard_filter:{_rejection_category(reason)}"] += 1
        report.hard_filter_passed = len(plausible)
        report.stage_counts["hard_filter_passed"] = len(plausible)
        report.stage_counts["hard_filter_rejected"] = len(jobs) - len(plausible)
        report.stage_counts["location_confirmed_pass"] = sum(
            job.location_verification == VerificationStatus.CONFIRMED_PASS for job in jobs
        )
        report.stage_counts["location_unverified_but_plausible"] = sum(
            job.location_verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE for job in jobs
        )
        report.stage_counts["location_confirmed_fail"] = sum(
            job.location_verification == VerificationStatus.CONFIRMED_FAIL for job in jobs
        )
        report.stage_counts["fast_analyzed"] = len(fast_analyzed_ids)
        report.new_stage_counts["fast_analyzed"] = len(new_fast_analyzed_ids)
        report.timings_ms["hard_filter_and_fast_analysis"] = _elapsed_ms(filtering_started)
        report.rejection_counts = dict(sorted(rejection_counts.items()))
        report.source_metrics = _serialize_source_metrics(source_metrics)
        self._checkpoint(report, "hard_filter_and_fast_analysis")

        salary_started = monotonic()
        preliminary = diversify_opportunities(
            sorted(
                [score_job(job, self.profile) for job in plausible],
                key=lambda item: item.score.overall,
                reverse=True,
            )
        )
        # Salary uncertainty is not an admission gate. Every plausible job is
        # classified, while only missing-salary finalists consume the bounded
        # external research quota.
        salary_candidates = preliminary
        report.stage_counts["salary_evaluation_pool"] = len(salary_candidates)
        report.stage_counts["salary_known_before_research"] = sum(
            job.salary.midpoint_gbp is not None for job in salary_candidates
        )
        report.stage_counts["salary_missing_before_research"] = sum(
            job.salary.midpoint_gbp is None for job in salary_candidates
        )
        for job in salary_candidates:
            source_metrics.setdefault(job.source, Counter())["salary_evaluation_pool"] += 1

        salary_research_candidates = [
            job for job in salary_candidates if job.salary.midpoint_gbp is None
        ][: self.salary_research_limit]
        report.stage_counts["salary_research_selected"] = len(salary_research_candidates)
        for job in salary_research_candidates:
            source_metrics.setdefault(job.source, Counter())["salary_research_selected"] += 1
            try:
                _, researched = self.salary_enricher.enrich(job)
                report.salary_researched += int(researched)
                source_metrics[job.source]["salary_researched"] += int(researched)
            except Exception as exc:  # external search failure must not kill the run
                report.errors.append(f"salary:{job.id}:{type(exc).__name__}")
                rejection_counts["salary:research_failed"] += 1
                logger.exception("Salary enrichment failed", extra={"job_id": job.id})

        for job in salary_candidates:
            # Enrichment may have changed the evidence, so derive the state again.
            score_job(job, self.profile)
        salary_confirmed = [
            job for job in salary_candidates if job.salary_verification == VerificationStatus.CONFIRMED_PASS
        ]
        salary_unverified = [
            job
            for job in salary_candidates
            if job.salary_verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE
        ]
        salary_failed = [
            job for job in salary_candidates if job.salary_verification == VerificationStatus.CONFIRMED_FAIL
        ]
        salary_eligible = salary_confirmed + salary_unverified
        report.stage_counts["salary_researched"] = report.salary_researched
        report.stage_counts["salary_confirmed_pass"] = len(salary_confirmed)
        report.stage_counts["salary_unverified_but_plausible"] = len(salary_unverified)
        report.stage_counts["salary_confirmed_fail"] = len(salary_failed)
        # Retained as a compatibility metric; this now means eligible for deep
        # analysis, not that salary has necessarily been verified.
        report.stage_counts["salary_qualified"] = len(salary_eligible)
        report.stage_counts["salary_research_attempted"] = report.salary_researched
        for job in salary_candidates:
            if job.salary_verification == VerificationStatus.CONFIRMED_PASS:
                source_metrics[job.source]["salary_confirmed_pass"] += 1
                source_metrics[job.source]["salary_qualified"] += 1
            elif job.salary_verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE:
                source_metrics[job.source]["salary_unverified_but_plausible"] += 1
                source_metrics[job.source]["salary_qualified"] += 1
            else:
                rejection_counts["salary:below_floor"] += 1
                source_metrics[job.source]["salary_confirmed_fail"] += 1
        report.timings_ms["salary_evaluation"] = _elapsed_ms(salary_started)
        report.rejection_counts = dict(sorted(rejection_counts.items()))
        report.source_metrics = _serialize_source_metrics(source_metrics)
        self._checkpoint(report, "salary_evaluation")

        deep_analysis_started = monotonic()
        model_calls_before = int(getattr(self.deep_analyzer, "model_calls", 0))
        model_input_before = int(getattr(self.deep_analyzer, "input_tokens", 0))
        model_output_before = int(getattr(self.deep_analyzer, "output_tokens", 0))
        closed_jobs = [job for job in salary_eligible if vacancy_is_closed(job, now)]
        for job in closed_jobs:
            rejection_counts["deep_analysis:closed"] += 1
            source_metrics.setdefault(job.source, Counter())["deep_analysis_closed"] += 1
        deep_universe = sorted(
            [
                score_job(job, self.profile)
                for job in salary_eligible
                if not vacancy_is_closed(job, now)
            ],
            key=lambda item: item.score.overall,
            reverse=True,
        )
        identities = {
            job.id: analysis_identity(job, self.profile, self.deep_analyzer) for job in deep_universe
        }
        cache_records = self.repository.get_deep_analysis_records(
            [identity.cache_key for identity in identities.values()]
        )
        cached_analyzed: list[Job] = []
        cache_misses: list[Job] = []
        evaluated_identities: dict[str, AnalysisIdentity] = {}
        for job in deep_universe:
            identity = identities[job.id]
            record = cache_records.get(identity.cache_key)
            if record is not None and record.job_id == job.id:
                cached_analyzed.append(score_job(apply_record(job, record), self.profile))
                evaluated_identities[job.id] = identity
                source_metrics.setdefault(job.source, Counter())["deep_analysis_cache_hit"] += 1
                source_metrics[job.source]["deep_analysis_evaluated"] += 1
            else:
                cache_misses.append(job)
                source_metrics.setdefault(job.source, Counter())["deep_analysis_cache_miss"] += 1

        first_queued_at_by_job_id: dict[str, str] = {}
        if self.analysis_dispatcher is not None:
            existing_tasks = self.repository.get_analysis_tasks(
                self.analysis_dispatcher.customer_id,
                [identities[job.id].cache_key for job in cache_misses],
            )
            first_queued_at_by_job_id = {
                job.id: (
                    task.first_queued_at
                    if (task := existing_tasks.get(identities[job.id].cache_key)) is not None
                    else now.isoformat()
                )
                for job in cache_misses
            }
        selection = select_fresh_candidates(
            cache_misses,
            histories,
            identities,
            self.deep_analysis_limit,
            seed=run_id,
            now=now,
            first_queued_at_by_job_id=first_queued_at_by_job_id,
        )
        deep_candidates = selection.jobs
        self._record_freshness(report, deep_candidates, "analysis", 48 * 3600)
        selected_ids = {job.id for job in deep_candidates}
        deferred = [job for job in cache_misses if job.id not in selected_ids]
        cache_miss_states = Counter(
            analysis_state(identities[job.id], histories.get(job.id)) for job in cache_misses
        )
        selection_reasons = Counter(selection.reasons.values())
        report.stage_counts["deep_analysis_candidate_universe"] = len(deep_universe)
        report.stage_counts["deep_analysis_closed"] = len(closed_jobs)
        report.stage_counts["deep_analysis_cache_hits"] = len(cached_analyzed)
        report.stage_counts["deep_analysis_cache_misses"] = len(cache_misses)
        report.stage_counts["deep_analysis_cache_misses_never_analyzed"] = cache_miss_states.get(
            "never_analyzed", 0
        )
        report.stage_counts["deep_analysis_cache_misses_changed"] = cache_miss_states.get("changed", 0)
        report.stage_counts["deep_analysis_cache_misses_legacy"] = cache_miss_states.get(
            "legacy_uncached", 0
        )
        report.stage_counts["deep_analysis_deferred"] = len(deferred)
        report.stage_counts["deep_selection_priority"] = selection_reasons.get("priority", 0)
        report.stage_counts["deep_selection_diversity"] = selection_reasons.get("diversity", 0)
        report.stage_counts["deep_selection_audit"] = selection_reasons.get("audit", 0)
        report.stage_counts["deep_analysis_dispatched"] = (
            len(deep_candidates) if self.analysis_dispatcher else 0
        )
        report.stage_counts["deep_analysis_attempted"] = (
            0 if self.analysis_dispatcher else len(deep_candidates)
        )
        for job in deferred:
            source_metrics[job.source]["deep_analysis_deferred"] += 1

        if self.analysis_dispatcher is not None:
            return self._dispatch_deep_analysis(
                now=now,
                pipeline_started=pipeline_started,
                deep_analysis_started=deep_analysis_started,
                run_id=run_id,
                send_email=send_email,
                report=report,
                collected_jobs=collected_jobs,
                jobs=jobs,
                plausible=plausible,
                salary_candidates=salary_candidates,
                salary_research_candidates=salary_research_candidates,
                salary_confirmed=salary_confirmed,
                salary_unverified=salary_unverified,
                closed_jobs=closed_jobs,
                deep_universe=deep_universe,
                cache_misses=cache_misses,
                cached_analyzed=cached_analyzed,
                cache_records=cache_records,
                identities=identities,
                histories=histories,
                new_unique_ids=new_unique_ids,
                fast_analyzed_ids=fast_analyzed_ids,
                deep_candidates=deep_candidates,
                selection=selection,
                source_metrics=source_metrics,
            )

        analyzed: list[Job] = list(cached_analyzed)
        deep_analyzed_ids: set[str] = set()
        new_deep_analyzed_ids: set[str] = set()
        cache_writes = 0
        cache_write_failures = 0
        for job in deep_candidates:
            source_metrics.setdefault(job.source, Counter())["deep_analysis_attempted"] += 1
            reason = selection.reasons[job.id]
            source_metrics[job.source][f"deep_selection_{reason}"] += 1
            input_before = int(getattr(self.deep_analyzer, "input_tokens", 0))
            output_before = int(getattr(self.deep_analyzer, "output_tokens", 0))
            try:
                analyzed_job = score_job(self.deep_analyzer.analyze(job, self.profile), self.profile)
                analyzed.append(analyzed_job)
                report.deeply_analyzed += 1
                deep_analyzed_ids.add(job.id)
                evaluated_identities[job.id] = identities[job.id]
                source_metrics[job.source]["deep_analysis_succeeded"] += 1
                source_metrics[job.source]["deep_analysis_evaluated"] += 1
                if job.id not in histories or histories[job.id].deep_analysis_runs == 0:
                    new_deep_analyzed_ids.add(job.id)
                    source_metrics[job.source]["new_deep_analyzed"] += 1
                try:
                    self.repository.put_deep_analysis_record(
                        record_from_job(
                            analyzed_job,
                            identities[job.id],
                            input_tokens=int(getattr(self.deep_analyzer, "input_tokens", 0))
                            - input_before,
                            output_tokens=int(getattr(self.deep_analyzer, "output_tokens", 0))
                            - output_before,
                        )
                    )
                    cache_writes += 1
                    source_metrics[job.source]["deep_analysis_cache_write"] += 1
                except Exception as exc:
                    cache_write_failures += 1
                    report.errors.append(f"analysis_cache:{job.id}:{type(exc).__name__}")
                    logger.exception("Deep-analysis cache write failed", extra={"job_id": job.id})
            except Exception as exc:
                report.errors.append(f"analysis:{job.id}:{type(exc).__name__}")
                rejection_counts["deep_analysis:failed"] += 1
                source_metrics[job.source]["deep_analysis_failed"] += 1
                source_metrics[job.source]["deep_analysis_evaluated"] += 1
                logger.exception("Deep analysis failed", extra={"job_id": job.id})
                analyzed.append(score_job(job, self.profile))
        report.stage_counts["deep_analysis_succeeded"] = report.deeply_analyzed
        report.stage_counts["deep_analysis_failed"] = len(deep_candidates) - report.deeply_analyzed
        report.stage_counts["deep_analysis_evaluated"] = len(analyzed)
        report.stage_counts["deep_analysis_cache_writes"] = cache_writes
        report.stage_counts["deep_analysis_cache_write_failures"] = cache_write_failures
        report.new_stage_counts["deep_analysis_succeeded"] = len(new_deep_analyzed_ids)
        report.timings_ms["deep_analysis"] = _elapsed_ms(deep_analysis_started)
        report.model_usage = {
            "calls": int(getattr(self.deep_analyzer, "model_calls", 0)) - model_calls_before,
            "input_tokens": int(getattr(self.deep_analyzer, "input_tokens", 0)) - model_input_before,
            "output_tokens": int(getattr(self.deep_analyzer, "output_tokens", 0))
            - model_output_before,
            "cache_hits": len(cached_analyzed),
            "cached_input_tokens": sum(record.input_tokens for record in cache_records.values()),
            "cached_output_tokens": sum(record.output_tokens for record in cache_records.values()),
        }
        report.rejection_counts = dict(sorted(rejection_counts.items()))
        report.source_metrics = _serialize_source_metrics(source_metrics)
        self._checkpoint(report, "deep_analysis")

        ranking_started = monotonic()
        ranked = diversify_opportunities(rank_jobs(analyzed, self.profile, self.deep_analysis_limit))
        viable_analyzed = sum(job.score.hard_gate_passed for job in analyzed)
        qualified_ids = {job.id for job in ranked}
        new_qualified_ids = {
            job.id for job in ranked if job.id not in histories or histories[job.id].qualified_runs == 0
        }
        report.stage_counts["qualified_shortlist"] = len(ranked)
        report.stage_counts["shortlist_salary_confirmed"] = sum(
            job.salary_verification == VerificationStatus.CONFIRMED_PASS for job in ranked
        )
        report.stage_counts["shortlist_salary_unverified"] = sum(
            job.salary_verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE for job in ranked
        )
        report.stage_counts["shortlist_location_confirmed"] = sum(
            job.location_verification == VerificationStatus.CONFIRMED_PASS for job in ranked
        )
        report.stage_counts["shortlist_location_unverified"] = sum(
            job.location_verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE for job in ranked
        )
        report.new_stage_counts["qualified_shortlist"] = len(new_qualified_ids)
        rejection_counts["ranking:final_viability_gate"] += len(analyzed) - viable_analyzed
        rejection_counts["ranking:shortlist_limit"] += max(0, viable_analyzed - len(ranked))
        for job in ranked:
            source_metrics.setdefault(job.source, Counter())["qualified_shortlist"] += 1
            if job.id in new_qualified_ids:
                source_metrics[job.source]["new_qualified_shortlist"] += 1
        recommendations: list[Job] = []
        digest_jobs: list[Job] = []
        run_date = now.date().isoformat()
        for job in ranked:
            existing = self.repository.get_job(job.id)
            if existing is not None:
                job.status = existing.status
                job.recommendation_date = existing.recommendation_date
                job.dismissal_reason = existing.dismissal_reason
                job.application_tracking = existing.application_tracking
                job.first_seen_at = existing.first_seen_at
                listings = {(item.source, item.source_id): item for item in existing.listings}
                for listing in job.listings:
                    key = (listing.source, listing.source_id)
                    if previous := listings.get(key):
                        listing.first_seen_at = previous.first_seen_at
                    listings[key] = listing
                job.listings = list(listings.values())
                self.repository.put_job(job)
                if job.status in {
                    JobStatus.RECOMMENDED,
                    JobStatus.SHORTLISTED,
                    JobStatus.PREPARING,
                    JobStatus.READY,
                }:
                    digest_jobs.append(job)
                continue
            if len(recommendations) >= self.digest_limit:
                continue
            job.status = JobStatus.RECOMMENDED
            job.recommendation_date = run_date
            self.repository.put_job(job)
            recommendations.append(job)
            digest_jobs.append(job)
            source_metrics[job.source]["new_recommendations"] += 1
        report.recommended = len(recommendations)
        digest_jobs = digest_jobs[: self.digest_limit]
        self._record_freshness(report, digest_jobs, "digest", 24 * 3600)
        report.digest_count = len(digest_jobs)
        report.stage_counts["new_recommendations"] = len(recommendations)
        report.stage_counts["digest_included"] = len(digest_jobs)
        report.stage_counts["user_shortlisted_total"] = len(
            self.repository.list_jobs(JobStatus.SHORTLISTED)
        )
        for job in digest_jobs:
            source_metrics[job.source]["digest_included"] += 1
        report.timings_ms["ranking_and_persistence"] = _elapsed_ms(ranking_started)

        history_started = monotonic()
        _update_analysis_histories(
            histories,
            jobs,
            now.isoformat(),
            run_id=run_id,
            fast_analyzed_ids=fast_analyzed_ids,
            deep_analyzed_ids=deep_analyzed_ids,
            evaluated_identities=evaluated_identities,
            qualified_ids=qualified_ids,
        )
        self.repository.put_analysis_histories(list(histories.values()))
        if telemetry_baseline:
            self.repository.mark_analysis_history_initialized(now.isoformat())
        report.timings_ms["analysis_history_persistence"] = _elapsed_ms(history_started)
        stage_jobs = {
            "collected_listings": collected_jobs,
            "canonical_unique": jobs,
            "new_canonical_jobs": [job for job in jobs if job.id in new_unique_ids],
            "hard_filter_passed": plausible,
            "fast_analyzed": [job for job in plausible if job.id in fast_analyzed_ids],
            "salary_evaluation_pool": salary_candidates,
            "salary_research_selected": salary_research_candidates,
            "salary_confirmed_pass": salary_confirmed,
            "salary_unverified_but_plausible": salary_unverified,
            "deep_analysis_closed": closed_jobs,
            "deep_analysis_attempted": deep_candidates,
            "deep_analysis_succeeded": [job for job in analyzed if job.id in deep_analyzed_ids],
            "deep_analysis_cache_hits": cached_analyzed,
            "deep_analysis_evaluated": analyzed,
            "qualified_shortlist": ranked,
            "new_recommendations": recommendations,
            "digest_included": digest_jobs,
        }
        report.registry_source_metrics = _dimension_metrics(
            stage_jobs, lambda job: job.source_key or job.source
        )
        report.employer_metrics = _dimension_metrics(
            stage_jobs, lambda job: canonical_company(job.company) or "unknown"
        )
        report.role_family_metrics = _dimension_metrics(
            stage_jobs, lambda job: job.role_family or "unmatched"
        )
        _persist_source_health(self.repository, source_fetches, report.registry_source_metrics)
        report.rejection_counts = dict(sorted(rejection_counts.items()))
        report.source_metrics = _serialize_source_metrics(source_metrics)
        self._checkpoint(report, "ranking_and_history_persistence")

        email_started = monotonic()
        if send_email:
            digest_key = f"digest#{run_date}"
            if self.repository.claim_once(digest_key):
                try:
                    digest = render_digest(
                        digest_jobs,
                        self.dashboard_url,
                        run_date,
                        report=report,
                        new_job_ids={job.id for job in recommendations},
                    )
                    self.email_sender.send(
                        self.profile.preferences["digest_recipient"], digest, idempotency_key=digest_key
                    )
                    report.emailed = True
                except Exception:
                    self.repository.release_claim(digest_key)
                    raise
        report.stage_counts["email_sent"] = int(report.emailed)
        report.timings_ms["email"] = _elapsed_ms(email_started)
        report.timings_ms["total"] = _elapsed_ms(pipeline_started)
        report.completed_at = datetime.now(UTC).isoformat()
        report.status = "completed"
        report.last_completed_stage = "completed"
        self.repository.put_run(report)
        self._release_discovery_reservation(report)
        return report, recommendations

    def finalize_analysis_run(
        self,
        analysis_run: AnalysisRun,
        candidates: list[Job],
        tasks: list[AnalysisTask],
    ) -> tuple[RunReport, list[Job]]:
        """Join asynchronous task results, rank once, and finish the original run."""
        report = self.repository.get_run(analysis_run.run_id)
        if report is None:
            raise ValueError(f"Unknown pipeline run {analysis_run.run_id}")
        if report.status == "completed":
            duplicate = RunReport(**report.to_dict())
            duplicate.status = "duplicate"
            return duplicate, []
        if analysis_run.profile_version != self.profile.version:
            raise ValueError("Analysis run profile version no longer matches the configured profile")

        claim_key = f"analysis-finalize#{analysis_run.run_id}"
        if not self.repository.claim_once(claim_key):
            current = self.repository.get_run(analysis_run.run_id)
            if current and current.status == "completed":
                duplicate = RunReport(**current.to_dict())
                duplicate.status = "duplicate"
                return duplicate, []
            return report, []
        try:
            return self._finalize_analysis_run_claimed(analysis_run, candidates, tasks, report)
        except Exception:
            self.repository.release_claim(claim_key)
            raise

    def _finalize_analysis_run_claimed(
        self,
        analysis_run: AnalysisRun,
        candidates: list[Job],
        tasks: list[AnalysisTask],
        report: RunReport,
    ) -> tuple[RunReport, list[Job]]:
        now = datetime.now(UTC)
        identities = {
            job.id: analysis_identity(job, self.profile, self.deep_analyzer) for job in candidates
        }
        cache_records = self.repository.get_deep_analysis_records(
            [identity.cache_key for identity in identities.values()]
        )
        histories = self.repository.get_analysis_histories([job.id for job in candidates])
        task_by_job_id = {task.job_id: task for task in tasks}
        source_metrics = {
            source: Counter(metrics) for source, metrics in report.source_metrics.items()
        }
        rejection_counts = Counter(report.rejection_counts)
        analyzed: list[Job] = []
        selected_evaluated: list[Job] = []
        fresh_succeeded: list[Job] = []
        worker_cache_hits: list[Job] = []
        failed_jobs: list[Job] = []
        evaluated_identities: dict[str, AnalysisIdentity] = {}

        for job in candidates:
            identity = identities[job.id]
            task = task_by_job_id.get(job.id)
            record = cache_records.get(identity.cache_key)
            if record is not None and record.job_id == job.id:
                analyzed_job = score_job(apply_record(job, record), self.profile)
                analyzed.append(analyzed_job)
                evaluated_identities[job.id] = identity
                if task is not None:
                    selected_evaluated.append(analyzed_job)
                    metrics = source_metrics.setdefault(job.source, Counter())
                    metrics["deep_analysis_evaluated"] += 1
                    if task.result_source == "fresh":
                        fresh_succeeded.append(analyzed_job)
                        metrics["deep_analysis_succeeded"] += 1
                        metrics["deep_analysis_cache_write"] += 1
                    else:
                        worker_cache_hits.append(analyzed_job)
                        metrics["deep_analysis_worker_cache_hit"] += 1
                continue
            if task is not None:
                failed_jobs.append(job)
                rejection_counts["deep_analysis:failed"] += 1
                source_metrics.setdefault(job.source, Counter())["deep_analysis_failed"] += 1
                error_type = "timed_out" if task.status == "timed_out" else "failed"
                report.errors.append(f"analysis:{job.id}:{error_type}")

        attempted_tasks = [task for task in tasks if task.attempts > 0]
        fresh_ids = {job.id for job in fresh_succeeded}
        new_deep_ids = {
            job.id
            for job in fresh_succeeded
            if job.id not in histories or histories[job.id].deep_analysis_runs == 0
        }
        for task in attempted_tasks:
            source_metrics.setdefault(task.source, Counter())["deep_analysis_attempted"] += 1
        for job in fresh_succeeded:
            if job.id in new_deep_ids:
                source_metrics[job.source]["new_deep_analyzed"] += 1

        report.deeply_analyzed = len(fresh_succeeded)
        report.stage_counts["deep_analysis_attempted"] = len(attempted_tasks)
        report.stage_counts["deep_analysis_worker_attempts"] = sum(
            task.attempts for task in tasks
        )
        report.stage_counts["deep_analysis_succeeded"] = len(fresh_succeeded)
        report.stage_counts["deep_analysis_worker_cache_hits"] = len(worker_cache_hits)
        report.stage_counts["deep_analysis_failed"] = len(failed_jobs)
        report.stage_counts["deep_analysis_evaluated"] = len(analyzed)
        report.stage_counts["deep_analysis_cache_writes"] = len(fresh_succeeded)
        report.stage_counts["deep_analysis_cache_write_failures"] = 0
        report.new_stage_counts["deep_analysis_succeeded"] = len(new_deep_ids)
        report.model_usage.update(
            {
                "calls": sum(task.model_calls for task in tasks),
                "input_tokens": sum(task.input_tokens for task in tasks),
                "output_tokens": sum(task.output_tokens for task in tasks),
                "worker_cache_hits": len(worker_cache_hits),
            }
        )
        report.timings_ms["deep_analysis"] = _duration_ms(analysis_run.prepared_at, now)

        ranking_started = monotonic()
        ranked = diversify_opportunities(rank_jobs(analyzed, self.profile, self.deep_analysis_limit))
        viable_analyzed = sum(job.score.hard_gate_passed for job in analyzed)
        qualified_ids = {job.id for job in ranked}
        new_qualified_ids = {
            job.id for job in ranked if job.id not in histories or histories[job.id].qualified_runs == 0
        }
        report.stage_counts["qualified_shortlist"] = len(ranked)
        report.stage_counts["shortlist_salary_confirmed"] = sum(
            job.salary_verification == VerificationStatus.CONFIRMED_PASS for job in ranked
        )
        report.stage_counts["shortlist_salary_unverified"] = sum(
            job.salary_verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE for job in ranked
        )
        report.stage_counts["shortlist_location_confirmed"] = sum(
            job.location_verification == VerificationStatus.CONFIRMED_PASS for job in ranked
        )
        report.stage_counts["shortlist_location_unverified"] = sum(
            job.location_verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE for job in ranked
        )
        report.new_stage_counts["qualified_shortlist"] = len(new_qualified_ids)
        rejection_counts["ranking:final_viability_gate"] += len(analyzed) - viable_analyzed
        rejection_counts["ranking:shortlist_limit"] += max(0, viable_analyzed - len(ranked))
        for job in ranked:
            source_metrics.setdefault(job.source, Counter())["qualified_shortlist"] += 1
            if job.id in new_qualified_ids:
                source_metrics[job.source]["new_qualified_shortlist"] += 1

        recommendations: list[Job] = []
        digest_jobs: list[Job] = []
        run_date = datetime.fromisoformat(report.started_at).date().isoformat()
        for job in ranked:
            existing = self.repository.get_job(job.id)
            if existing is not None:
                job.status = existing.status
                job.recommendation_date = existing.recommendation_date
                job.dismissal_reason = existing.dismissal_reason
                job.application_tracking = existing.application_tracking
                job.first_seen_at = existing.first_seen_at
                listings = {(item.source, item.source_id): item for item in existing.listings}
                for listing in job.listings:
                    key = (listing.source, listing.source_id)
                    if previous := listings.get(key):
                        listing.first_seen_at = previous.first_seen_at
                    listings[key] = listing
                job.listings = list(listings.values())
                self.repository.put_job(job)
                if job.status in {
                    JobStatus.RECOMMENDED,
                    JobStatus.SHORTLISTED,
                    JobStatus.PREPARING,
                    JobStatus.READY,
                }:
                    digest_jobs.append(job)
                continue
            if len(recommendations) >= self.digest_limit:
                continue
            job.status = JobStatus.RECOMMENDED
            job.recommendation_date = run_date
            self.repository.put_job(job)
            recommendations.append(job)
            digest_jobs.append(job)
            source_metrics[job.source]["new_recommendations"] += 1
        report.recommended = len(recommendations)
        digest_jobs = digest_jobs[: self.digest_limit]
        self._record_freshness(report, digest_jobs, "digest", 24 * 3600)
        report.digest_count = len(digest_jobs)
        report.stage_counts["new_recommendations"] = len(recommendations)
        report.stage_counts["digest_included"] = len(digest_jobs)
        report.stage_counts["user_shortlisted_total"] = len(
            self.repository.list_jobs(JobStatus.SHORTLISTED)
        )
        for job in digest_jobs:
            source_metrics[job.source]["digest_included"] += 1

        _update_analysis_histories(
            histories,
            candidates,
            now.isoformat(),
            run_id=analysis_run.run_id,
            fast_analyzed_ids=set(),
            deep_analyzed_ids=fresh_ids,
            evaluated_identities=evaluated_identities,
            qualified_ids=qualified_ids,
        )
        self.repository.put_analysis_histories(list(histories.values()))

        final_stage_jobs = {
            "deep_analysis_attempted": [
                Job.from_dict(task.job) for task in attempted_tasks
            ],
            "deep_analysis_succeeded": fresh_succeeded,
            "deep_analysis_worker_cache_hits": worker_cache_hits,
            "deep_analysis_evaluated": selected_evaluated,
            "qualified_shortlist": ranked,
            "new_recommendations": recommendations,
            "digest_included": digest_jobs,
        }
        report.registry_source_metrics = _merge_dimension_metrics(
            report.registry_source_metrics,
            _dimension_metrics(final_stage_jobs, lambda job: job.source_key or job.source),
        )
        report.employer_metrics = _merge_dimension_metrics(
            report.employer_metrics,
            _dimension_metrics(
                final_stage_jobs, lambda job: canonical_company(job.company) or "unknown"
            ),
        )
        report.role_family_metrics = _merge_dimension_metrics(
            report.role_family_metrics,
            _dimension_metrics(final_stage_jobs, lambda job: job.role_family or "unmatched"),
        )
        source_fetches = [SourceFetchDiagnostic(**item) for item in report.source_fetches]
        _persist_source_health(self.repository, source_fetches, report.registry_source_metrics)

        backlog = self.repository.list_analysis_tasks(analysis_run.customer_id)
        report.stage_counts["analysis_backlog_queued_after_completion"] = sum(
            task.status in {"queued", "timed_out"} for task in backlog
        )
        report.stage_counts["analysis_tasks_completed"] = sum(
            task.status == "completed" for task in tasks
        )
        report.stage_counts["analysis_tasks_timed_out"] = sum(
            task.status == "timed_out" for task in tasks
        )
        report.stage_counts["analysis_budget_deferred"] = sum(
            task.status == "timed_out" and task.last_error in {
                "daily_analysis_budget_exhausted", "discovery_capacity_reserved"}
            for task in tasks
        )
        report.stage_counts["analysis_deadline_timed_out"] = sum(
            task.status == "timed_out" and task.last_error == "analysis_deadline_exceeded"
            for task in tasks
        )
        for task in tasks:
            if task.status == "timed_out" and task.last_error in {
                "daily_analysis_budget_exhausted", "discovery_capacity_reserved"
            }:
                source_metrics.setdefault(task.source, Counter())["analysis_budget_deferred"] += 1
            elif task.status == "timed_out" and task.last_error == "analysis_deadline_exceeded":
                source_metrics.setdefault(task.source, Counter())["analysis_deadline_timed_out"] += 1
        report.rejection_counts = dict(sorted(rejection_counts.items()))
        report.source_metrics = _serialize_source_metrics(source_metrics)
        report.timings_ms["ranking_and_persistence"] = _elapsed_ms(ranking_started)

        email_started = monotonic()
        if analysis_run.send_email:
            digest_key = f"digest#{run_date}"
            if self.repository.claim_once(digest_key):
                try:
                    digest = render_digest(
                        digest_jobs,
                        self.dashboard_url,
                        run_date,
                        report=report,
                        new_job_ids={job.id for job in recommendations},
                    )
                    self.email_sender.send(
                        self.profile.preferences["digest_recipient"],
                        digest,
                        idempotency_key=digest_key,
                    )
                    report.emailed = True
                except Exception:
                    self.repository.release_claim(digest_key)
                    raise
        report.stage_counts["email_sent"] = int(report.emailed)
        report.timings_ms["email"] = _elapsed_ms(email_started)
        completed_at = datetime.now(UTC)
        report.timings_ms["total"] = _duration_ms(report.started_at, completed_at)
        report.completed_at = completed_at.isoformat()
        report.status = "completed"
        report.last_completed_stage = "completed"
        report.telemetry_schema_version = 4
        self.repository.put_run(report)
        self._release_discovery_reservation(report)
        self.repository.complete_analysis_run(
            analysis_run.run_id, completed_at.isoformat(), report.status
        )
        ingestion_run = self.repository.get_ingestion_run(analysis_run.run_id)
        if ingestion_run is not None:
            self.repository.complete_ingestion_run(
                analysis_run.run_id, completed_at.isoformat(), report.status
            )
        return report, recommendations

    def _release_discovery_reservation(self, report: RunReport) -> None:
        try:
            scheduled_at = datetime.fromisoformat(report.run_id.replace("Z", "+00:00"))
        except ValueError:
            return  # Manual runs do not release a scheduled discovery reservation.
        limits = AnalysisBudgetLimits.from_environment()
        try:
            self.repository.set_analysis_discovery_state(
                getattr(self.analysis_dispatcher, "customer_id", "personal"),
                limits.budget_date(scheduled_at), "released",
                datetime.now(UTC).isoformat(),
                maximum_backlog_calls=max(0, limits.maximum_calls - 25),
            )
        except Exception:
            logging.exception("Unable to release discovery call reservation")

    def _record_freshness(self, report, jobs, stage, max_age):
        from .freshness import shadow_assess
        result = shadow_assess(self.freshness_service, jobs, stage, max_age)
        report.configuration[f"freshness_{stage}"] = result
        for key, value in result.items():
            if isinstance(value, int):
                report.stage_counts[f"freshness_{stage}_{key}"] = value

    def _dispatch_deep_analysis(
        self,
        *,
        now: datetime,
        pipeline_started: float,
        deep_analysis_started: float,
        run_id: str,
        send_email: bool,
        report: RunReport,
        collected_jobs: list[Job],
        jobs: list[Job],
        plausible: list[Job],
        salary_candidates: list[Job],
        salary_research_candidates: list[Job],
        salary_confirmed: list[Job],
        salary_unverified: list[Job],
        closed_jobs: list[Job],
        deep_universe: list[Job],
        cache_misses: list[Job],
        cached_analyzed: list[Job],
        cache_records: dict[str, Any],
        identities: dict[str, AnalysisIdentity],
        histories: dict[str, JobAnalysisHistory],
        new_unique_ids: set[str],
        fast_analyzed_ids: set[str],
        deep_candidates: list[Job],
        selection,
        source_metrics: dict[str, Counter[str]],
    ) -> tuple[RunReport, list[Job]]:
        dispatcher = self.analysis_dispatcher
        if dispatcher is None:  # pragma: no cover - guarded by caller
            raise RuntimeError("Asynchronous analysis dispatcher is not configured")

        for job in deep_candidates:
            reason = selection.reasons[job.id]
            source_metrics.setdefault(job.source, Counter())["deep_analysis_dispatched"] += 1
            source_metrics[job.source][f"deep_selection_{reason}"] += 1

        report.stage_counts["deep_analysis_succeeded"] = 0
        report.stage_counts["deep_analysis_failed"] = 0
        report.stage_counts["deep_analysis_evaluated"] = len(cached_analyzed)
        report.stage_counts["deep_analysis_cache_writes"] = 0
        report.stage_counts["deep_analysis_cache_write_failures"] = 0
        report.new_stage_counts["deep_analysis_succeeded"] = 0
        report.model_usage = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hits": len(cached_analyzed),
            "cached_input_tokens": sum(record.input_tokens for record in cache_records.values()),
            "cached_output_tokens": sum(record.output_tokens for record in cache_records.values()),
        }

        _update_analysis_histories(
            histories,
            jobs,
            now.isoformat(),
            run_id=run_id,
            fast_analyzed_ids=fast_analyzed_ids,
            deep_analyzed_ids=set(),
            evaluated_identities={job.id: identities[job.id] for job in cached_analyzed},
            qualified_ids=set(),
        )
        self.repository.put_analysis_histories(list(histories.values()))
        if report.newness_scope == "baseline_estimate":
            self.repository.mark_analysis_history_initialized(now.isoformat())

        stage_jobs = {
            "collected_listings": collected_jobs,
            "canonical_unique": jobs,
            "new_canonical_jobs": [job for job in jobs if job.id in new_unique_ids],
            "hard_filter_passed": plausible,
            "fast_analyzed": [job for job in plausible if job.id in fast_analyzed_ids],
            "salary_evaluation_pool": salary_candidates,
            "salary_research_selected": salary_research_candidates,
            "salary_confirmed_pass": salary_confirmed,
            "salary_unverified_but_plausible": salary_unverified,
            "deep_analysis_closed": closed_jobs,
            "deep_analysis_dispatched": deep_candidates,
            "deep_analysis_cache_hits": cached_analyzed,
            "deep_analysis_evaluated": cached_analyzed,
        }
        report.registry_source_metrics = _dimension_metrics(
            stage_jobs, lambda job: job.source_key or job.source
        )
        report.employer_metrics = _dimension_metrics(
            stage_jobs, lambda job: canonical_company(job.company) or "unknown"
        )
        report.role_family_metrics = _dimension_metrics(
            stage_jobs, lambda job: job.role_family or "unmatched"
        )
        report.source_metrics = _serialize_source_metrics(source_metrics)

        analysis_run = dispatcher.prepare(
            run_id=run_id,
            started_at=report.started_at,
            send_email=send_email,
            profile=self.profile,
            candidates=deep_universe,
            cache_misses=cache_misses,
            selected_jobs=deep_candidates,
            identities=identities,
            selection_reasons=selection.reasons,
            priorities=selection.priorities,
        )
        report.stage_counts["analysis_backlog_total"] = analysis_run.backlog_size
        backlog_tasks = self.repository.list_analysis_tasks(analysis_run.customer_id)
        report.stage_counts["analysis_backlog_queued_after_dispatch"] = sum(
            task.status in {"queued", "timed_out"} for task in backlog_tasks
        )
        report.stage_counts["analysis_backlog_expired_total"] = sum(
            task.status == "expired"
            for task in backlog_tasks
        )
        report.configuration.update(
            {
                "analysis_customer_id": analysis_run.customer_id,
                "deep_analysis_deadline_minutes": dispatcher.deadline_minutes,
                "deep_analysis_maximum_concurrency": dispatcher.maximum_concurrency,
                "deep_analysis_estimated_input_tokens": analysis_run.estimated_input_tokens,
            }
        )
        report.timings_ms["deep_analysis_dispatch"] = _elapsed_ms(deep_analysis_started)
        report.timings_ms["total"] = _elapsed_ms(pipeline_started)
        report.status = "analysis_pending"
        report.last_completed_stage = "deep_analysis_dispatched"
        report.telemetry_schema_version = 4
        self.repository.put_run(report)

        messages_sent = dispatcher.enqueue(analysis_run)
        if messages_sent != analysis_run.expected_tasks:
            logger.warning(
                "Analysis dispatch resumed with fewer messages than the original batch",
                extra={
                    "run_id": run_id,
                    "expected_tasks": analysis_run.expected_tasks,
                    "messages_sent": messages_sent,
                },
            )
        return report, []

    def _checkpoint(self, report: RunReport, stage: str) -> None:
        report.last_completed_stage = stage
        self.repository.put_run(report)


def _elapsed_ms(started_at: float) -> int:
    return round((monotonic() - started_at) * 1000)


def _duration_ms(started_at: str, completed_at: datetime) -> int:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0, round((completed_at - started).total_seconds() * 1000))


def _serialize_source_metrics(source_metrics: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {source: dict(sorted(metrics.items())) for source, metrics in sorted(source_metrics.items())}


def _dimension_metrics(
    stage_jobs: dict[str, list[Job]], key_for_job
) -> dict[str, dict[str, int]]:
    metrics: dict[str, Counter[str]] = {}
    for stage, jobs in stage_jobs.items():
        for job in jobs:
            metrics.setdefault(str(key_for_job(job)), Counter())[stage] += 1
    return _serialize_source_metrics(metrics)


def _merge_dimension_metrics(
    existing: dict[str, dict[str, int]],
    additions: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    merged = {dimension: Counter(metrics) for dimension, metrics in existing.items()}
    for dimension, metrics in additions.items():
        merged.setdefault(dimension, Counter()).update(metrics)
    return _serialize_source_metrics(merged)


def _persist_source_health(
    repository: Repository,
    source_fetches,
    registry_metrics: dict[str, dict[str, int]],
) -> None:
    for fetch in source_fetches:
        previous = repository.get_source_health(fetch.source_key)
        succeeded = fetch.status != "error"
        metrics = registry_metrics.get(fetch.source_key, {})
        repository.put_source_health(
            SourceHealth(
                source_key=fetch.source_key,
                source=fetch.source,
                ats=fetch.ats,
                employer=fetch.employer,
                tenant=fetch.tenant,
                status=fetch.status,
                last_attempt_at=fetch.completed_at,
                last_successful_fetch_at=(
                    fetch.completed_at
                    if succeeded
                    else previous.last_successful_fetch_at if previous else None
                ),
                last_error=fetch.error,
                consecutive_failures=(
                    0 if succeeded else (previous.consecutive_failures if previous else 0) + 1
                ),
                listings_fetched=fetch.listings_fetched,
                relevant_jobs_produced=metrics.get("hard_filter_passed", 0),
                qualified_jobs=metrics.get("qualified_shortlist", 0),
            )
        )


def _rejection_category(reason: str) -> str:
    mappings = (
        ("outside the configured", "role_family"),
        ("explicitly excluded", "excluded_engineering"),
        ("realistic seniority", "seniority"),
        ("not clearly uk", "non_uk_location"),
        ("non-uk location restriction", "non_uk_restriction"),
        ("requires ", "experience"),
        ("people-management", "people_management"),
        ("below the", "salary_below_floor"),
    )
    lower = reason.lower()
    return next((category for fragment, category in mappings if fragment in lower), "other")


def _backfill_recommended_histories(
    histories: dict[str, JobAnalysisHistory], existing_jobs: list[Job]
) -> None:
    for job in existing_jobs:
        if job.id in histories:
            continue
        first_seen = job.first_seen_at
        qualified_at = f"{job.recommendation_date}T00:00:00+00:00" if job.recommendation_date else first_seen
        histories[job.id] = JobAnalysisHistory(
            job_id=job.id,
            first_seen_at=first_seen,
            last_seen_at=job.last_verified_at,
            seen_runs=1,
            last_seen_run_id="historical-baseline",
            sources=sorted({job.source, *(listing.source for listing in job.listings)}),
            first_fast_analyzed_at=first_seen,
            last_fast_analyzed_at=job.last_verified_at,
            fast_analysis_runs=1,
            last_fast_analysis_run_id="historical-baseline",
            first_deep_analyzed_at=first_seen,
            last_deep_analyzed_at=job.last_verified_at,
            deep_analysis_runs=1,
            last_deep_analysis_run_id="historical-baseline",
            first_qualified_at=qualified_at,
            last_qualified_at=job.last_verified_at,
            qualified_runs=1,
            last_qualified_run_id="historical-baseline",
        )


def _update_analysis_histories(
    histories: dict[str, JobAnalysisHistory],
    jobs: list[Job],
    observed_at: str,
    *,
    run_id: str,
    fast_analyzed_ids: set[str],
    deep_analyzed_ids: set[str],
    evaluated_identities: dict[str, AnalysisIdentity],
    qualified_ids: set[str],
) -> None:
    for job in jobs:
        history = histories.get(job.id)
        if history is None:
            history = JobAnalysisHistory(job.id, observed_at, observed_at)
            histories[job.id] = history
        history.last_seen_at = observed_at
        if history.last_seen_run_id != run_id:
            history.seen_runs += 1
            history.last_seen_run_id = run_id
        history.sources = sorted(set(history.sources) | {job.source} | {item.source for item in job.listings})
        if job.id in fast_analyzed_ids and history.last_fast_analysis_run_id != run_id:
            history.first_fast_analyzed_at = history.first_fast_analyzed_at or observed_at
            history.last_fast_analyzed_at = observed_at
            history.fast_analysis_runs += 1
            history.last_fast_analysis_run_id = run_id
        if job.id in deep_analyzed_ids and history.last_deep_analysis_run_id != run_id:
            history.first_deep_analyzed_at = history.first_deep_analyzed_at or observed_at
            history.last_deep_analyzed_at = observed_at
            history.deep_analysis_runs += 1
            history.last_deep_analysis_run_id = run_id
        if identity := evaluated_identities.get(job.id):
            history.last_deep_job_fingerprint = identity.job_fingerprint
            history.last_deep_profile_fingerprint = identity.profile_fingerprint
            history.last_deep_analyzer_version = identity.analyzer_version
            history.last_deep_model_id = identity.model_id
        if job.id in qualified_ids and history.last_qualified_run_id != run_id:
            history.first_qualified_at = history.first_qualified_at or observed_at
            history.last_qualified_at = observed_at
            history.qualified_runs += 1
            history.last_qualified_run_id = run_id


def diversify_opportunities(jobs: list[Job]) -> list[Job]:
    """Keep the strongest company/title variant so one employer cannot crowd a digest."""
    seen: set[tuple[str, str]] = set()
    diversified: list[Job] = []
    for job in jobs:
        key = (canonical_company(job.company), canonical_title(job.title))
        if key in seen:
            continue
        seen.add(key)
        diversified.append(job)
    return diversified

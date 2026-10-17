from __future__ import annotations

import json
import os

from .application import BedrockPackGenerator, EvidenceBasedPackGenerator, ResilientPackGenerator
from .async_analysis import SqsAnalysisDispatcher
from .connectors import connectors_from_environment
from .digest import SesEmailSender, SnsEmailSender
from .enrichment import (
    BedrockAnalyzer,
    BraveSearchProvider,
    PublicPageFetcher,
    RuleBasedAnalyzer,
    SalaryEnricher,
)
from .ingestion import S3JobStagingStore
from .pipeline import CareerPipeline
from .profile import load_profile
from .rich_matching import make_analyzer
from .reed_hydration import SqsReedHydrationDispatcher
from .repository import DynamoRepository
from .services import ApplicationService
from .storage import S3ArtifactStore


INTEGRATION_KEYS = (
    "BRAVE_API_KEY",
    "ADZUNA_APP_ID",
    "ADZUNA_APP_KEY",
    "REED_API_KEY",
    "CVLIBRARY_API_KEY",
    "ORACLE_HCM_USERNAME",
    "ORACLE_HCM_PASSWORD",
    "ICIMS_USERNAME",
    "ICIMS_PASSWORD",
    "SAP_SUCCESSFACTORS_USERNAME",
    "SAP_SUCCESSFACTORS_PASSWORD",
)


def secrets() -> dict[str, str]:
    secret_arn = os.getenv("INTEGRATION_SECRET_ARN")
    if not secret_arn:
        return {}
    import boto3

    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    return json.loads(response["SecretString"])


def load_integration_environment() -> None:
    payload = secrets()
    for key in INTEGRATION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


# Backward-compatible private alias for callers deployed before the public helper.
_load_integration_environment = load_integration_environment


def repository() -> DynamoRepository:
    return DynamoRepository(os.environ["TABLE_NAME"])


def application_service(*, async_worker: bool = False) -> ApplicationService:
    profile = load_profile(os.environ.get("PROFILE_PATH"))
    repo = repository()
    if os.getenv("APPLICATION_PROFILE_MODE", "legacy") == "rich":
        from .rich_application import RichApplicationGenerator
        from .rich_matching import RichAnalyzer, ProductionProvider
        from botocore.config import Config
        import boto3
        import time
        model_id = os.environ["STRONG_MODEL_ID"]
        analyzer = RichAnalyzer(model_id)
        session = boto3.Session()
        from pathlib import Path
        session._session.get_component("data_loader").search_paths.insert(
            0, str(Path(analyzer.directory) / "aws_models"))
        analyzer.client = session.client("bedrock-runtime", config=Config(
            connect_timeout=2, read_timeout=150 if async_worker else 15,
            retries={"total_max_attempts": 1}))
        # Same daily ledger, lower-priority lane: application requests do not borrow discovery capacity.
        analyzer.bind(repo, os.getenv("CAREER_CUSTOMER_ID", "personal"), "manual_discovery")
        if async_worker:
            class AsyncApplicationProvider(ProductionProvider):
                def structured(self, *args, **kwargs):
                    # The analysis worker's 35-second continuation cutoff applies
                    # per request; this worker has a longer bounded lifetime.
                    self.started = time.monotonic()
                    return super().structured(*args, **kwargs)
            provider = AsyncApplicationProvider(analyzer, repo,
                os.getenv("CAREER_CUSTOMER_ID", "personal"), "manual_discovery")
        else:
            provider = analyzer.provider
        generator = RichApplicationGenerator(analyzer.draft, analyzer.index,
            provider=provider, model_id=model_id, max_attempts=2 if async_worker else 1)
        return ApplicationService(repo, profile, generator, S3ArtifactStore(os.environ["ARTIFACT_BUCKET"]))
    fallback_generator = EvidenceBasedPackGenerator()
    generator = (
        ResilientPackGenerator(BedrockPackGenerator(os.environ["STRONG_MODEL_ID"]), fallback_generator)
        if os.getenv("STRONG_MODEL_ID")
        else fallback_generator
    )
    return ApplicationService(
        repository(),
        profile,
        generator,
        S3ArtifactStore(os.environ["ARTIFACT_BUCKET"]),
    )


def pipeline(
    *,
    excluded_ats: set[str] | frozenset[str] = frozenset(),
    enable_async_analysis: bool | None = None,
) -> CareerPipeline:
    load_integration_environment()
    repo = repository()
    profile = load_profile(os.environ.get("PROFILE_PATH"))
    search = BraveSearchProvider(os.environ["BRAVE_API_KEY"]) if os.getenv("BRAVE_API_KEY") else None
    deep_analyzer = (
        make_analyzer(os.environ["STRONG_MODEL_ID"])
        if os.getenv("STRONG_MODEL_ID")
        else RuleBasedAnalyzer()
    )
    email_sender = (
        SnsEmailSender(os.environ["DIGEST_TOPIC_ARN"])
        if os.getenv("DIGEST_TOPIC_ARN")
        else SesEmailSender(os.environ["SES_SENDER"])
    )
    async_enabled = (
        _environment_flag("ASYNC_DEEP_ANALYSIS_ENABLED", False)
        if enable_async_analysis is None
        else enable_async_analysis
    )
    analysis_dispatcher = None
    reed_hydration_dispatcher = None
    sqs = None
    if os.getenv("REED_HYDRATION_QUEUE_URL"):
        import boto3

        sqs = boto3.client("sqs")
        reed_hydration_dispatcher = SqsReedHydrationDispatcher(
            repository=repo,
            sqs=sqs,
            queue_url=os.environ["REED_HYDRATION_QUEUE_URL"],
            customer_id=os.getenv("CAREER_CUSTOMER_ID", "personal"),
        )
    if async_enabled:
        import boto3

        sqs = sqs or boto3.client("sqs")
        analysis_dispatcher = SqsAnalysisDispatcher(
            repository=repo,
            staging=S3JobStagingStore(os.environ["ARTIFACT_BUCKET"]),
            sqs=sqs,
            analysis_queue_url=os.environ["DEEP_ANALYSIS_QUEUE_URL"],
            finalizer_queue_url=os.environ["ANALYSIS_FINALIZER_QUEUE_URL"],
            customer_id=os.getenv("CAREER_CUSTOMER_ID", "personal"),
            deadline_minutes=int(os.getenv("DEEP_ANALYSIS_DEADLINE_MINUTES", "15")),
            maximum_concurrency=int(os.getenv("DEEP_ANALYSIS_MAXIMUM_CONCURRENCY", "4")),
            stale_after_days=int(os.getenv("ANALYSIS_BACKLOG_STALE_DAYS", "14")),
        )
    from .freshness import service_from_environment
    return CareerPipeline(
        profile=profile,
        repository=repo,
        connectors=connectors_from_environment(
            excluded_ats=excluded_ats,
            detail_store=repo,
            hydration_dispatcher=reed_hydration_dispatcher,
        ),
        fast_analyzer=RuleBasedAnalyzer(),
        deep_analyzer=deep_analyzer,
        salary_enricher=SalaryEnricher(repo, search, PublicPageFetcher()),
        email_sender=email_sender,
        dashboard_url=os.environ["DASHBOARD_URL"],
        deep_analysis_limit=int(os.getenv("DEEP_ANALYSIS_LIMIT", "25")),
        digest_limit=int(os.getenv("DIGEST_LIMIT", "10")),
        salary_research_limit=int(
            os.getenv("SALARY_RESEARCH_LIMIT", os.getenv("SALARY_SEARCH_LIMIT", "20"))
        ),
        discovery_excluded_ats=excluded_ats,
        analysis_dispatcher=analysis_dispatcher,
        freshness_service=service_from_environment(repo, sqs),
    )


def _environment_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

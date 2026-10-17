"""Read-only operational and traffic metrics for the private dashboard."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from typing import Any, Callable


@dataclass(frozen=True)
class RangeSpec:
    duration: timedelta
    period: int


RANGES = {
    "24h": RangeSpec(timedelta(hours=24), 3600),
    "7d": RangeSpec(timedelta(days=7), 21600),
    "30d": RangeSpec(timedelta(days=30), 86400),
}


def _metric(
    query_id: str,
    namespace: str,
    name: str,
    dimensions: dict[str, str],
    stat: str,
    period: int,
) -> dict[str, Any]:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": namespace,
                "MetricName": name,
                "Dimensions": [{"Name": key, "Value": value} for key, value in dimensions.items()],
            },
            "Period": period,
            "Stat": stat,
        },
        "ReturnData": True,
    }


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _nested(value: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = value
        for part in path:
            current = _decode(current)
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            return _decode(current)
    return None


class MetricsService:
    def __init__(
        self,
        cloudwatch: Any,
        global_cloudwatch: Any,
        rum: Any,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.cloudwatch = cloudwatch
        self.global_cloudwatch = global_cloudwatch
        self.rum = rum
        self.now = now or (lambda: datetime.now(UTC))

    @classmethod
    def from_environment(cls) -> "MetricsService":
        import boto3

        region = os.getenv("AWS_REGION", "eu-west-2")
        return cls(
            boto3.client("cloudwatch", region_name=region),
            boto3.client("cloudwatch", region_name="us-east-1"),
            boto3.client("rum", region_name=region),
        )

    def snapshot(self, range_name: str) -> dict[str, Any]:
        if range_name not in RANGES:
            raise ValueError("range must be one of 24h, 7d, or 30d")
        spec = RANGES[range_name]
        end = self.now().astimezone(UTC).replace(second=0, microsecond=0)
        start = end - spec.duration
        regional = self._regional_metrics(start, end, spec.period)
        global_metrics = self._global_metrics(start, end, spec.period)
        pages = self._top_pages(start, end)

        return {
            "range": range_name,
            "generated_at": end.isoformat(),
            "traffic": {
                "page_views": self._sum(regional, "page_views"),
                "cloudfront_requests": self._sum(global_metrics, "cloudfront_requests"),
                "average_page_load_ms": self._average(regional, "page_load_ms"),
                "javascript_errors": self._sum(regional, "javascript_errors"),
                "browser_http_5xx": self._sum(regional, "browser_http_5xx"),
                "cloudfront_4xx_rate": self._average(global_metrics, "cloudfront_4xx_rate"),
                "cloudfront_5xx_rate": self._average(global_metrics, "cloudfront_5xx_rate"),
                "top_pages": pages,
            },
            "twin": {
                "chat_requests": self._sum(regional, "chat_requests"),
                "chat_failures": self._sum(regional, "chat_failures"),
                "semantic_fallbacks": self._sum(regional, "semantic_fallbacks"),
                "average_retrieval_ms": self._average(regional, "retrieval_ms"),
                "average_model_ms": self._average(regional, "model_ms"),
                "input_tokens": self._sum(regional, "input_tokens"),
                "output_tokens": self._sum(regional, "output_tokens"),
                "api_5xx": self._sum(regional, "api_5xx"),
            },
            "career_agent": {
                "api_errors": self._sum(regional, "career_api_errors"),
                "scheduled_errors": self._sum(regional, "career_scheduled_errors"),
            },
            "series": self._series(regional),
            "notes": [
                "Page views come from real browsers; CloudFront requests also include assets and bots.",
                "Traffic monitoring uses no analytics cookies or persistent visitor identifier.",
                "Metrics can take several minutes to appear after a visit or chat.",
            ],
        }

    def _regional_metrics(self, start: datetime, end: datetime, period: int) -> dict[str, list[tuple[datetime, float]]]:
        monitor = os.environ["RUM_APP_MONITOR_NAME"]
        twin_environment = os.getenv("TWIN_ENVIRONMENT", "dev")
        twin_lambda = os.getenv("TWIN_LAMBDA_NAME", "example-twin-api")
        twin_api = os.environ["TWIN_API_ID"]
        career_api = os.environ["CAREER_API_LAMBDA_NAME"]
        career_scheduled = os.environ["CAREER_SCHEDULED_LAMBDA_NAME"]
        rum_dimensions = {"application_name": monitor}
        twin_dimensions = {"Environment": twin_environment}
        queries = [
            _metric("page_views", "AWS/RUM", "PageViewCount", rum_dimensions, "Sum", period),
            _metric("page_load_ms", "AWS/RUM", "PerformanceNavigationDuration", rum_dimensions, "Average", period),
            _metric("javascript_errors", "AWS/RUM", "JsErrorCount", rum_dimensions, "Sum", period),
            _metric("browser_http_5xx", "AWS/RUM", "Http5xxCount", rum_dimensions, "Sum", period),
            _metric("chat_requests", "Twin/Runtime", "ChatRequests", twin_dimensions, "Sum", period),
            _metric("chat_failures", "Twin/Runtime", "ChatFailures", twin_dimensions, "Sum", period),
            _metric("semantic_fallbacks", "Twin/Runtime", "SemanticFallbacks", twin_dimensions, "Sum", period),
            _metric("retrieval_ms", "Twin/Runtime", "RetrievalLatency", twin_dimensions, "Average", period),
            _metric("model_ms", "Twin/Runtime", "ModelLatency", twin_dimensions, "Average", period),
            _metric("input_tokens", "Twin/Runtime", "InputTokens", twin_dimensions, "Sum", period),
            _metric("output_tokens", "Twin/Runtime", "OutputTokens", twin_dimensions, "Sum", period),
            _metric("api_5xx", "AWS/ApiGateway", "5xx", {"ApiId": twin_api}, "Sum", period),
            _metric("career_api_errors", "AWS/Lambda", "Errors", {"FunctionName": career_api}, "Sum", period),
            _metric("career_scheduled_errors", "AWS/Lambda", "Errors", {"FunctionName": career_scheduled}, "Sum", period),
        ]
        return self._query(self.cloudwatch, queries, start, end)

    def _global_metrics(self, start: datetime, end: datetime, period: int) -> dict[str, list[tuple[datetime, float]]]:
        dimensions = {
            "DistributionId": os.environ["PORTFOLIO_DISTRIBUTION_ID"],
            "Region": "Global",
        }
        queries = [
            _metric("cloudfront_requests", "AWS/CloudFront", "Requests", dimensions, "Sum", period),
            _metric("cloudfront_4xx_rate", "AWS/CloudFront", "4xxErrorRate", dimensions, "Average", period),
            _metric("cloudfront_5xx_rate", "AWS/CloudFront", "5xxErrorRate", dimensions, "Average", period),
        ]
        return self._query(self.global_cloudwatch, queries, start, end)

    @staticmethod
    def _query(client: Any, queries: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, float]]]:
        response = client.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampAscending",
        )
        values: dict[str, list[tuple[datetime, float]]] = {}
        for result in response.get("MetricDataResults", []):
            points = sorted(
                zip(result.get("Timestamps", []), result.get("Values", [])),
                key=lambda item: item[0],
            )
            values[result["Id"]] = [(timestamp.astimezone(UTC), float(value)) for timestamp, value in points]
        return values

    def _top_pages(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        token: str | None = None
        pages = 0
        while pages < 10:
            request: dict[str, Any] = {
                "Name": os.environ["RUM_APP_MONITOR_NAME"],
                "TimeRange": {"After": int(start.timestamp() * 1000), "Before": int(end.timestamp() * 1000)},
                "Filters": [{"Name": "EventType", "Values": ["com.amazon.rum.page_view_event"]}],
                "MaxResults": 100,
            }
            if token:
                request["NextToken"] = token
            response = self.rum.get_app_monitor_data(**request)
            for raw_event in response.get("Events", []):
                event = _decode(raw_event)
                if not isinstance(event, dict):
                    continue
                page = _nested(
                    event,
                    ("metadata", "pageId"),
                    ("metadata", "page", "id"),
                    ("event_metadata", "page_id"),
                    ("event_details", "pageId"),
                    ("details", "pageId"),
                )
                if isinstance(page, str) and page and not page.startswith("/career"):
                    counts[page.split("?", 1)[0]] += 1
            token = response.get("NextToken")
            pages += 1
            if not token:
                break
        return [{"path": path, "views": views} for path, views in counts.most_common(8)]

    @staticmethod
    def _sum(values: dict[str, list[tuple[datetime, float]]], key: str) -> int:
        return round(sum(value for _, value in values.get(key, [])))

    @staticmethod
    def _average(values: dict[str, list[tuple[datetime, float]]], key: str) -> float:
        points = [value for _, value in values.get(key, [])]
        return round(sum(points) / len(points), 1) if points else 0.0

    @staticmethod
    def _series(values: dict[str, list[tuple[datetime, float]]]) -> list[dict[str, Any]]:
        by_timestamp: dict[str, dict[str, Any]] = {}
        for key in ("page_views", "chat_requests", "chat_failures"):
            for timestamp, value in values.get(key, []):
                iso = timestamp.isoformat()
                by_timestamp.setdefault(
                    iso,
                    {"timestamp": iso, "page_views": 0, "chat_requests": 0, "chat_failures": 0},
                )[key] = round(value)
        return [by_timestamp[key] for key in sorted(by_timestamp)]

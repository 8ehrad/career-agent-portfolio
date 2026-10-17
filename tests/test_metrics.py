from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import unittest
from unittest.mock import patch

from career_agent.metrics import MetricsService


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


class FakeCloudWatch:
    def __init__(self, values: dict[str, list[float]]):
        self.values = values
        self.requests: list[dict] = []

    def get_metric_data(self, **request):
        self.requests.append(request)
        results = []
        for query in request["MetricDataQueries"]:
            values = self.values.get(query["Id"], [])
            results.append(
                {
                    "Id": query["Id"],
                    "Timestamps": [NOW for _ in values],
                    "Values": values,
                }
            )
        return {"MetricDataResults": results}


class FakeRum:
    def __init__(self):
        self.requests: list[dict] = []

    def get_app_monitor_data(self, **request):
        self.requests.append(request)
        events = [
            {"metadata": json.dumps({"pageId": "/"})},
            {"metadata": {"page": {"id": "/work?campaign=test"}}},
            {"event_metadata": {"page_id": "/work"}},
            {"event_details": {"pageId": "/career"}},
        ]
        return {"Events": [json.dumps(event) for event in events]}


class MetricsServiceTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "RUM_APP_MONITOR_NAME": "portfolio",
            "TWIN_ENVIRONMENT": "dev",
            "TWIN_LAMBDA_NAME": "example-twin-api",
            "TWIN_API_ID": "abc123",
            "CAREER_API_LAMBDA_NAME": "career-api",
            "CAREER_SCHEDULED_LAMBDA_NAME": "career-scheduled",
            "PORTFOLIO_DISTRIBUTION_ID": "EDIST",
        }

    def test_snapshot_aggregates_operational_and_traffic_metrics(self):
        regional = FakeCloudWatch(
            {
                "page_views": [2, 3],
                "page_load_ms": [120, 180],
                "javascript_errors": [1],
                "chat_requests": [4, 2],
                "chat_failures": [1],
                "semantic_fallbacks": [2],
                "retrieval_ms": [30, 50],
                "model_ms": [500, 700],
                "input_tokens": [1000],
                "output_tokens": [200],
                "api_5xx": [1],
                "career_api_errors": [0],
                "career_scheduled_errors": [1],
            }
        )
        global_cloudwatch = FakeCloudWatch(
            {"cloudfront_requests": [100, 50], "cloudfront_4xx_rate": [1, 3], "cloudfront_5xx_rate": [0, 0]}
        )
        rum = FakeRum()

        with patch.dict(os.environ, self.environment, clear=False):
            result = MetricsService(regional, global_cloudwatch, rum, now=lambda: NOW).snapshot("24h")

        self.assertEqual(5, result["traffic"]["page_views"])
        self.assertEqual(150, result["traffic"]["cloudfront_requests"])
        self.assertEqual(150.0, result["traffic"]["average_page_load_ms"])
        self.assertEqual(2.0, result["traffic"]["cloudfront_4xx_rate"])
        self.assertEqual([{"path": "/work", "views": 2}, {"path": "/", "views": 1}], result["traffic"]["top_pages"])
        self.assertEqual(6, result["twin"]["chat_requests"])
        self.assertEqual(40.0, result["twin"]["average_retrieval_ms"])
        self.assertEqual(1, result["career_agent"]["scheduled_errors"])
        self.assertTrue(result["series"])

        rum_query = regional.requests[0]["MetricDataQueries"][0]
        self.assertEqual("AWS/RUM", rum_query["MetricStat"]["Metric"]["Namespace"])
        self.assertEqual({"Name": "application_name", "Value": "portfolio"}, rum_query["MetricStat"]["Metric"]["Dimensions"][0])
        self.assertEqual("EDIST", global_cloudwatch.requests[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"])
        self.assertEqual("com.amazon.rum.page_view_event", rum.requests[0]["Filters"][0]["Values"][0])
        self.assertEqual(int((NOW.timestamp() - 86400) * 1000), rum.requests[0]["TimeRange"]["After"])

    def test_invalid_range_fails_before_aws_calls(self):
        regional = FakeCloudWatch({})
        with self.assertRaisesRegex(ValueError, "range must be"):
            MetricsService(regional, FakeCloudWatch({}), FakeRum(), now=lambda: NOW).snapshot("year")
        self.assertEqual([], regional.requests)


if __name__ == "__main__":
    unittest.main()

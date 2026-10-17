from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from career_agent.handlers import scheduled
from career_agent.models import RunReport


class ScheduledHandlerTests(unittest.TestCase):
    def test_operator_can_disable_email_during_smoke_test(self):
        runner = Mock()
        runner.run.return_value = (RunReport(run_id="smoke", started_at="now"), [])

        with patch.object(scheduled, "pipeline", return_value=runner):
            response = scheduled.handler({"run_id": "smoke", "send_email": False}, None)

        runner.run.assert_called_once_with(run_id="smoke", send_email=False)
        self.assertEqual([], response["recommendation_ids"])

    def test_scheduled_runs_send_email_by_default(self):
        runner = Mock()
        runner.run.return_value = (RunReport(run_id="scheduled", started_at="now"), [])

        with patch.object(scheduled, "pipeline", return_value=runner):
            scheduled.handler({}, None)

        runner.run.assert_called_once_with(run_id=None, send_email=True)

    def test_unresolved_scheduler_context_attribute_fails_loudly(self):
        runner = Mock()

        with patch.object(scheduled, "pipeline", return_value=runner):
            with self.assertRaisesRegex(ValueError, "context attribute was not resolved"):
                scheduled.handler({"run_id": "<aws.scheduler.scheduled-time>"}, None)

        runner.run.assert_not_called()

    @patch.object(scheduled, "handle_records")
    def test_sqs_events_are_routed_to_asynchronous_finalization(self, handle_records):
        event = {"Records": [{"body": "{}"}]}
        handle_records.return_value = {"batchItemFailures": []}

        response = scheduled.handler(event, None)

        self.assertEqual({"batchItemFailures": []}, response)
        handle_records.assert_called_once_with(event)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from career_agent.digest import Digest, SnsEmailSender


class FakeSns:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)


class DigestTests(unittest.TestCase):
    def test_sns_email_delivery_is_scoped_and_traceable(self):
        client = FakeSns()
        sender = SnsEmailSender("arn:aws:sns:eu-west-2:123:career", client)
        sender.send("owner@example.com", Digest("Subject", "<p>HTML</p>", "Plain digest"), "digest#2026-08-02")

        self.assertEqual(1, len(client.calls))
        self.assertEqual("Plain digest", client.calls[0]["Message"])
        self.assertEqual("digest#2026-08-02", client.calls[0]["MessageAttributes"]["CareerAgentRun"]["StringValue"])


if __name__ == "__main__":
    unittest.main()

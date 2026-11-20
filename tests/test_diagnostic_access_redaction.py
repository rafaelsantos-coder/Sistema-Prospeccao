import logging
import unittest

from app.main import _RedactWebhookSecretFilter


class AccessLogRedactionTest(unittest.TestCase):
    def test_request_targets(self):
        cases = [
            ("/internal/diag/example", "/internal/diag/***"),
            ("/internal/diag/example?detail=example", "/internal/diag/***"),
            ("/webhooks/zapi/instance/example", "/webhooks/zapi/instance/***"),
            ("/webhooks/zapi/example", "/webhooks/zapi/***"),
            ("/api/health", "/api/health"),
        ]
        for target, expected in cases:
            with self.subTest(target=target):
                args = ("client", "GET", target, "1.1", 200)
                record = logging.LogRecord("uvicorn.access", 20, "", 0, "%s %s %s %s %s", args, None)
                self.assertTrue(_RedactWebhookSecretFilter().filter(record))
                self.assertEqual(record.args, ("client", "GET", expected, "1.1", 200))

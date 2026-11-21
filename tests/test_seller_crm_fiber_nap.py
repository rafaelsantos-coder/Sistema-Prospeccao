import copy
import json
import unittest
from unittest.mock import patch

from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient


class FiberNapCRMTest(unittest.TestCase):
    def setUp(self):
        self.info = {"fibra": {"metragem": 38}, "categoryForm": {"values": {"plano": "650"}, "files": {"doc": 1}}}
        self.client = object.__new__(SulnetCRMClient)

    def request(self, method, path, **kwargs):
        self.assertEqual(path, "/api/opportunities/42")
        if method == "PATCH":
            self.info = copy.deepcopy(kwargs["json_body"]["productInfo"])
            return {}
        return {"id": 42, "product_info": json.dumps(self.info)}

    def test_update_correct_and_clear(self):
        with patch.object(SulnetCRMClient, "_request", side_effect=self.request) as request:
            for value in ["NAP-A (10) — 420 m", "NAP-B (11) — 89 m", ""]:
                self.client.sync_fiber_nap(42, value)
                self.assertEqual(self.info["fibra"], {"metragem": 38, "caixaNap": value})
                form = self.info["categoryForm"]
                self.assertEqual(form["values"], {"plano": "650", "caixa_nap": value})
                self.assertEqual(form["files"], {"doc": 1})
                request.reset_mock()
                self.client.sync_fiber_nap(42, value)
                self.assertEqual(request.call_count, 1)

    def test_unconfirmed_readback(self):
        responses = [{"productInfo": {}}, {}, {"productInfo": {"fibra": {"caixaNap": "NAP"}}}]
        with patch.object(SulnetCRMClient, "_request", side_effect=responses):
            with self.assertRaises(CRMIntegrationError):
                self.client.sync_fiber_nap(42, "NAP")

# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path

from supplier_client import DahaoshangClient, SupplierError, SupplierDeliveryPending


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")

    def json(self):
        return self.payload


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json, timeout))
        return FakeResponse(self.responses.pop(0))


class SupplierClientTests(unittest.TestCase):
    def make_client(self, responses):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "session.json"
        path.write_text(json.dumps({
            "base_url": "https://dahaoshang.com",
            "access_token": "test-token",
        }), encoding="utf-8")
        os.chmod(path, 0o600)
        http = FakeHTTP(responses)
        return DahaoshangClient(path, http=http), http

    def test_session_token_is_used_without_printing_or_persisting_password(self):
        client, http = self.make_client([{"Code": 200, "Data": {"UserId": 1}}])
        info = client.get_user_info()
        self.assertEqual(info["UserId"], 1)
        self.assertEqual(http.headers["Authorization"], "Bearer test-token")
        self.assertNotIn("Password", http.headers)

    def test_refuses_purchase_when_no_balance_payment_method_exists(self):
        product = {"Code": 200, "Data": {
            "ID": "P1", "CategoryId": "C1", "IsOnSale": True,
            "InStock": 5, "ProductModels": [],
        }}
        methods = {"Code": 200, "Data": [
            {"Text": "USDT(TRC20)", "Value": "115"},
            {"Text": "支付宝", "Value": "97"},
        ]}
        client, http = self.make_client([product, methods])
        with self.assertRaisesRegex(SupplierError, "账户余额支付方式"):
            client.buy_with_balance("P1")
        self.assertEqual(len(http.calls), 2)
        self.assertNotIn("/api/sysorder/buy", [c[1] for c in http.calls])

    def test_buy_uses_confirmed_payload_and_reads_content(self):
        responses = [
            {"Code": 200, "Data": {
                "ID": "P1", "CategoryId": "C1", "IsOnSale": True,
                "InStock": 5, "ProductModels": [{"ID": "M1", "Qty": 5}],
            }},
            {"Code": 200, "Data": [{"Text": "账户余额支付", "Value": "BAL"}]},
            {"Code": 200, "Data": {"ID": "O1", "Entity": {"ID": "O1", "OrderNo": "NO1"}}},
            {"Code": 200, "Data": {"Content": "account|password", "ShipStatus": "已发货"}},
        ]
        client, http = self.make_client(responses)
        result = client.buy_with_balance("P1", quantity=2)
        self.assertEqual(result["supplier_order_id"], "O1")
        self.assertEqual(result["content"], "account|password")
        buy_call = next(c for c in http.calls if c[1].endswith("/api/sysorder/buy"))
        self.assertEqual(buy_call[3], {
            "ProductId": "P1", "ModelId": "M1", "PayTypeId": "BAL",
            "Qty": 2, "Email": None,
        })

    def test_multiple_models_require_explicit_choice(self):
        product = {"Code": 200, "Data": {
            "ID": "P1", "CategoryId": "C1", "IsOnSale": True,
            "InStock": 10,
            "ProductModels": [{"ID": "M1", "Qty": 5}, {"ID": "M2", "Qty": 5}],
        }}
        client, http = self.make_client([product])
        with self.assertRaisesRegex(SupplierError, "多个规格"):
            client.buy_with_balance("P1")
        self.assertEqual(len(http.calls), 1)

    def test_missing_order_content_is_not_marked_delivered(self):
        responses = [
            {"Code": 200, "Data": {
                "ID": "P1", "CategoryId": "C1", "IsOnSale": True,
                "InStock": 1, "ProductModels": [],
            }},
            {"Code": 200, "Data": [{"Text": "余额支付", "Value": "BAL"}]},
            {"Code": 200, "Data": {"ID": "O1"}},
            {"Code": 200, "Data": {"Content": ""}},
        ]
        client, _ = self.make_client(responses)
        with self.assertRaises(SupplierDeliveryPending) as caught:
            client.buy_with_balance("P1")
        self.assertEqual(caught.exception.order_id, "O1")


if __name__ == "__main__":
    unittest.main()

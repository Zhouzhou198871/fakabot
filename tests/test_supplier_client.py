# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest.mock import patch
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
        client, http = self.make_client([{"Code": 200, "Data": {"UserId": 1}}, product, methods])
        with self.assertRaisesRegex(SupplierError, "账户余额支付方式"):
            client.buy_with_balance("P1")
        self.assertEqual(len(http.calls), 3)
        self.assertNotIn("/api/sysorder/buy", [c[1] for c in http.calls])

    def test_buy_uses_confirmed_payload_and_reads_content(self):
        responses = [
            {"Code": 200, "Data": {"UserId": 1}},
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
        client, http = self.make_client([{"Code": 200, "Data": {"UserId": 1}}, product])
        with self.assertRaisesRegex(SupplierError, "多个规格"):
            client.buy_with_balance("P1")
        self.assertEqual(len(http.calls), 2)

    def test_missing_order_content_is_not_marked_delivered(self):
        responses = [
            {"Code": 200, "Data": {"UserId": 1}},
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


    def test_recovers_encrypted_remote_session_without_writing_plaintext(self):
        from datetime import datetime, timedelta, timezone
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / "session-private.pem"
            private_path.write_bytes(private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            os.chmod(private_path, 0o600)
            aes_key = os.urandom(32)
            iv = os.urandom(12)
            token = "test-remote-encrypted-token"
            ciphertext = AESGCM(aes_key).encrypt(iv, token.encode(), None)
            wrapped = private_key.public_key().encrypt(
                aes_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            payload = {
                "ok": True,
                "encrypted_state": {
                    "v": 1,
                    "alg": "RSA-OAEP-256+A256GCM",
                    "iv": base64.b64encode(iv).decode(),
                    "ciphertext": base64.b64encode(ciphertext).decode(),
                    "wrapped_key": base64.b64encode(wrapped).decode(),
                },
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
            http = FakeHTTP([{"Code": 200, "Data": {"UserId": 7}}])
            remote = FakeResponse(payload)
            old_url = os.environ.get("SUPPLIER_SESSION_URL")
            old_key = os.environ.get("SUPPLIER_SESSION_PRIVATE_KEY")
            os.environ["SUPPLIER_SESSION_URL"] = "https://auth.example.test/_internal/session"
            os.environ["SUPPLIER_SESSION_PRIVATE_KEY"] = str(private_path)
            try:
                with patch("supplier_client.requests.get", return_value=remote) as get:
                    client = DahaoshangClient(
                        Path(directory) / "missing-session.json", http=http
                    )
                    self.assertEqual(client.get_user_info()["UserId"], 7)
                    self.assertEqual(http.headers["Authorization"], "Bearer " + token)
                    get.assert_called_once()
                self.assertFalse((Path(directory) / "missing-session.json").exists())
            finally:
                if old_url is None:
                    os.environ.pop("SUPPLIER_SESSION_URL", None)
                else:
                    os.environ["SUPPLIER_SESSION_URL"] = old_url
                if old_key is None:
                    os.environ.pop("SUPPLIER_SESSION_PRIVATE_KEY", None)
                else:
                    os.environ["SUPPLIER_SESSION_PRIVATE_KEY"] = old_key

    def test_rejects_expired_remote_session(self):
        from datetime import datetime, timedelta, timezone
        payload = {
            "ok": True,
            "encrypted_state": {},
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }
        old_url = os.environ.get("SUPPLIER_SESSION_URL")
        old_key = os.environ.get("SUPPLIER_SESSION_PRIVATE_KEY")
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "private.pem"
            key_path.write_text("unused", encoding="utf-8")
            os.chmod(key_path, 0o600)
            os.environ["SUPPLIER_SESSION_URL"] = "https://auth.example.test/_internal/session"
            os.environ["SUPPLIER_SESSION_PRIVATE_KEY"] = str(key_path)
            try:
                with patch("supplier_client.requests.get", return_value=FakeResponse(payload)):
                    with self.assertRaisesRegex(SupplierError, "已过期"):
                        DahaoshangClient(Path(directory) / "missing.json")
            finally:
                if old_url is None:
                    os.environ.pop("SUPPLIER_SESSION_URL", None)
                else:
                    os.environ["SUPPLIER_SESSION_URL"] = old_url
                if old_key is None:
                    os.environ.pop("SUPPLIER_SESSION_PRIVATE_KEY", None)
                else:
                    os.environ["SUPPLIER_SESSION_PRIVATE_KEY"] = old_key



if __name__ == "__main__":
    unittest.main()

[executed on device: iZj6cakb7vguedvzvbqrvbZ (14411471-502a-43de-a2d7-530edf7bf831)]

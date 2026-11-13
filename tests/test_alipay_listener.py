import base64
import unittest
from decimal import Decimal
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from alipay_listener import verify_alipay_notification, process_alipay_notification, AlipayNotificationError, _canonical_message


class AlipayListenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_pem = cls.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def setUp(self):
        self.order = {"order_id": "ALTEST001", "amount_cny": "16.00"}
        self.params = {"app_id": "sandbox-app", "seller_id": "sandbox-seller",
            "out_trade_no": "ALTEST001", "total_amount": "16.00",
            "trade_status": "TRADE_SUCCESS", "trade_no": "TRADE-SANDBOX-001",
            "sign_type": "RSA2"}
        self.sign()

    def sign(self):
        sig = self.private.sign(_canonical_message(self.params), padding.PKCS1v15(), hashes.SHA256())
        self.params["sign"] = base64.b64encode(sig).decode()

    def verify(self, params=None, order=None):
        return verify_alipay_notification(params or self.params, self.public_pem,
            "sandbox-app", "sandbox-seller", order or self.order)

    def test_valid_signed_success_notification(self):
        result = self.verify()
        self.assertTrue(result["verified"])
        self.assertEqual(result["provider"], "alipay")
        self.assertEqual(result["amount_cny"], "16.00")
        self.assertEqual(result["payment_ref"], "TRADE-SANDBOX-001")

    def test_rejects_tampered_amount(self):
        changed = dict(self.params, total_amount="160.00")
        with self.assertRaisesRegex(AlipayNotificationError, "signature_verification_failed"):
            self.verify(changed)

    def test_rejects_wrong_merchant_or_order(self):
        changed = dict(self.params, seller_id="other-seller")
        changed.pop("sign")
        sig = self.private.sign(_canonical_message(changed), padding.PKCS1v15(), hashes.SHA256())
        changed["sign"] = base64.b64encode(sig).decode()
        with self.assertRaisesRegex(AlipayNotificationError, "seller_id_mismatch"):
            self.verify(changed)

    def test_rejects_wrong_order_amount(self):
        order = dict(self.order, amount_cny="15.99")
        with self.assertRaisesRegex(AlipayNotificationError, "amount_mismatch"):
            self.verify(order=order)

    def test_rejects_non_success_status(self):
        changed = dict(self.params, trade_status="WAIT_BUYER_PAY")
        changed.pop("sign")
        sig = self.private.sign(_canonical_message(changed), padding.PKCS1v15(), hashes.SHA256())
        changed["sign"] = base64.b64encode(sig).decode()
        with self.assertRaisesRegex(AlipayNotificationError, "trade_not_successful"):
            self.verify(changed)

    def test_signed_notification_updates_order_without_real_money(self):
        import os
        from datetime import datetime, timedelta, timezone
        orders = [dict(self.order, payment_method="alipay_gateway", payment_status="pending",
                       status="pending_payment", supplier_verified=True, procurement_ready=True,
                       expires_at_ms=int((datetime.now(timezone.utc)+timedelta(minutes=15)).timestamp()*1000))]
        class FakeBot:
            def __init__(self): self.messages=[]
            def send_message(self, *args, **kwargs): self.messages.append((args, kwargs))
        old = os.environ.get("SUPPLIER_AUTOPROCUREMENT_ENABLED")
        os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = "0"
        bot = FakeBot()
        try:
            result = process_alipay_notification(self.params, self.public_pem,
                "sandbox-app", "sandbox-seller", lambda: orders,
                lambda rows: orders.__setitem__(slice(None), rows), bot)
            self.assertTrue(result["ok"])
            self.assertEqual(orders[0]["payment_status"], "paid")
            self.assertEqual(orders[0]["txid"], "TRADE-SANDBOX-001")
            replay = process_alipay_notification(self.params, self.public_pem,
                "sandbox-app", "sandbox-seller", lambda: orders,
                lambda rows: orders.__setitem__(slice(None), rows), bot)
            self.assertTrue(replay["replayed"])
        finally:
            if old is None: os.environ.pop("SUPPLIER_AUTOPROCUREMENT_ENABLED", None)
            else: os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = old

    def test_rejects_invalid_signature(self):
        changed = dict(self.params, sign=base64.b64encode(b"bad").decode())
        with self.assertRaises(AlipayNotificationError):
            self.verify(changed)


if __name__ == "__main__":
    unittest.main()
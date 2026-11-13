# -*- coding: utf-8 -*-
"""Alipay RSA2 asynchronous notification verification core.

This module accepts provider notifications only after RSA2 signature, merchant,
order, amount and successful trade-status checks. It does not scrape personal
Alipay accounts or treat screenshots/browser redirects as payment evidence.
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature


class AlipayNotificationError(ValueError):
    pass


def _canonical_message(params):
    pairs = []
    for key in sorted(params):
        if key in ("sign", "sign_type"):
            continue
        value = params[key]
        if value is None or str(value) == "":
            continue
        pairs.append((str(key), str(value)))
    return "&".join(f"{key}={value}" for key, value in pairs).encode("utf-8")


def verify_alipay_notification(params, alipay_public_key_pem, expected_app_id,
                               expected_seller_id, order):
    """Verify one form-encoded Alipay notify payload against an existing order."""
    if not isinstance(params, dict) or not isinstance(order, dict):
        raise AlipayNotificationError("invalid_notification")
    signature = str(params.get("sign") or "")
    if not signature or str(params.get("sign_type") or "RSA2").upper() != "RSA2":
        raise AlipayNotificationError("missing_or_unsupported_signature")
    try:
        key_data = alipay_public_key_pem.encode() if isinstance(alipay_public_key_pem, str) else alipay_public_key_pem
        public_key = serialization.load_pem_public_key(key_data)
        import base64
        public_key.verify(base64.b64decode(signature, validate=True),
                          _canonical_message(params), padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise AlipayNotificationError("signature_verification_failed") from exc
    if str(params.get("app_id") or "") != str(expected_app_id):
        raise AlipayNotificationError("app_id_mismatch")
    if str(params.get("seller_id") or "") != str(expected_seller_id):
        raise AlipayNotificationError("seller_id_mismatch")
    if str(params.get("out_trade_no") or "") != str(order.get("order_id") or ""):
        raise AlipayNotificationError("order_id_mismatch")
    try:
        paid = Decimal(str(params.get("total_amount"))).quantize(Decimal("0.01"))
        expected = Decimal(str(order.get("amount_cny"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AlipayNotificationError("invalid_amount") from exc
    if not paid.is_finite() or paid != expected:
        raise AlipayNotificationError("amount_mismatch")
    if str(params.get("trade_status") or "") not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        raise AlipayNotificationError("trade_not_successful")
    trade_no = str(params.get("trade_no") or "").strip()
    if not trade_no:
        raise AlipayNotificationError("missing_trade_no")
    return {
        "verified": True,
        "provider": "alipay",
        "event_id": f"alipay:{trade_no}:{params.get('trade_status')}",
        "payment_ref": trade_no,
        "order_id": str(order["order_id"]),
        "currency": "CNY",
        "amount_cny": format(paid, ".2f"),
        "trade_status": str(params["trade_status"]),
    }


def process_alipay_notification(params, alipay_public_key_pem, expected_app_id,
                                expected_seller_id, load_orders, save_orders,
                                bot, admin_id=None, supplier_client_factory=None):
    """Verify then apply one provider notification to the existing order flow."""
    orders = load_orders()
    if not isinstance(orders, list):
        raise AlipayNotificationError("order_store_unavailable")
    order_id = str(params.get("out_trade_no") or "")
    order = next((item for item in orders if isinstance(item, dict)
                  and str(item.get("order_id") or "") == order_id), None)
    if order is None:
        raise AlipayNotificationError("order_not_found")
    if order.get("payment_method") not in ("alipay_qr", "alipay_gateway"):
        raise AlipayNotificationError("wrong_payment_method")
    if int(order.get("expires_at_ms") or 0) < int(__import__("time").time() * 1000):
        raise AlipayNotificationError("order_expired")
    if order.get("payment_status") in ("paid", "completed", "delivered"):
        return {"ok": True, "replayed": True, "order_id": order_id}
    event = verify_alipay_notification(params, alipay_public_key_pem,
        expected_app_id, expected_seller_id, order)
    from payment_flow import handle_confirmed_payment
    handled = handle_confirmed_payment(
        bot, order_id,
        {"txid": event["payment_ref"], "provider": "alipay",
         "manual_verification": "provider_rsa2_notification",
         "amount_cny": event["amount_cny"]},
        load_orders=load_orders, save_orders=save_orders,
        admin_id=admin_id, supplier_client_factory=supplier_client_factory,
    )
    return {"ok": bool(handled), "replayed": not bool(handled),
            "order_id": order_id, "event_id": event["event_id"]}
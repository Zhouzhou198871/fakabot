# -*- coding: utf-8 -*-
"""Alipay RSA2 asynchronous notification verification core.

This module accepts provider notifications only after RSA2 signature, merchant,
order, amount and successful trade-status checks. It does not scrape personal
Alipay accounts or treat screenshots/browser redirects as payment evidence.
"""
from decimal import Decimal, InvalidOperation
import os, hmac, hashlib, time
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
    if not signature or str(params.get("sign_type") or "") != "RSA2":
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
    if str(params.get("trade_status") or "") != "TRADE_SUCCESS":
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


def _process_mock_notification(params, order, order_id, load_orders, save_orders, bot, admin_id=None):
    """Confirm a signed local mock event without ever marking the order as paid or procuring."""
    if str(params.get("mock_delivery") or "") == "1":
        # Restricted internal end-to-end test: never accepted by the public RSA2 /notify route.
        if os.getenv("ALIPAY_PAYMENT_MODE", "").strip().lower() != "real":
            raise AlipayNotificationError("mock_delivery_requires_real_sandbox_mode")
        if os.getenv("UPSTREAM_MOCK_MODE", "false").strip().lower() not in ("1", "true", "yes", "on"):
            raise AlipayNotificationError("mock_delivery_disabled")
        if "sandbox" not in os.getenv("ALIPAY_GATEWAY", "").strip().lower():
            raise AlipayNotificationError("mock_delivery_non_sandbox_gateway")
        if order.get("payment_method") != "alipay_gateway" or order.get("sandbox_test_order") is not True:
            raise AlipayNotificationError("mock_delivery_not_test_order")
        if not order_id.startswith("AL") or str(params.get("trade_status") or "") != "MOCK_DELIVERY_SUCCESS":
            raise AlipayNotificationError("invalid_mock_delivery_event")
        try:
            paid = Decimal(str(params.get("total_amount"))).quantize(Decimal("0.01"))
            expected = Decimal(str(order.get("amount_cny"))).quantize(Decimal("0.01"))
            expires = int(params.get("expires_at_ms"))
            order_expires = int(order.get("expires_at_ms") or 0)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AlipayNotificationError("invalid_mock_delivery_payload") from exc
        if not paid.is_finite() or paid != expected:
            raise AlipayNotificationError("mock_delivery_amount_mismatch")
        if expires != order_expires:
            raise AlipayNotificationError("mock_delivery_order_expiry_mismatch")
        token = os.getenv("ALIPAY_NOTIFY_TOKEN", "").strip()
        if not token:
            raise AlipayNotificationError("mock_delivery_token_missing")
        message = f"{order_id}|{format(paid, '.2f')}|{expires}|mock_delivery".encode("utf-8")
        expected_sig = hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()
        supplied_sig = str(params.get("mock_signature") or "")
        if not supplied_sig or not hmac.compare_digest(supplied_sig, expected_sig):
            raise AlipayNotificationError("mock_delivery_signature_invalid")
        expected_test_code = os.getenv("UPSTREAM_MOCK_DELIVERY_CODE", "TEST-CODE-123456")
        if (order.get("status") == "delivered"
                and order.get("procurement_status") == "delivered"
                and order.get("delivery_content") == expected_test_code
                and order.get("delivery_notification_status") == "sent"):
            # A verified replay of an already delivered test order is ACK-safe even after expiry.
            # No payment transition, supplier call, or Telegram resend occurs here.
            return {"ok": True, "replayed": True, "mock": True, "mock_delivery": True, "order_id": order_id}
        if expires < int(time.time() * 1000):
            raise AlipayNotificationError("mock_delivery_order_expired")
        from payment_flow import handle_confirmed_payment
        handled = handle_confirmed_payment(
            bot, order_id,
            {"txid": str(params.get("trade_no") or "MOCK-" + order_id),
             "provider": "internal_hmac_mock", "manual_verification": "worker_internal_mock_delivery",
             "amount_cny": format(paid, ".2f")},
            load_orders=load_orders, save_orders=save_orders, admin_id=admin_id,
        )
        if not handled:
            raise AlipayNotificationError("mock_delivery_payment_handler_rejected")
        return {"ok": True, "replayed": False, "mock": True, "mock_delivery": True, "order_id": order_id}

    if os.getenv("ALIPAY_PAYMENT_MODE", "").strip().lower() != "mock":
        raise AlipayNotificationError("mock_mode_disabled")
    if order.get("payment_method") != "alipay_mock":
        raise AlipayNotificationError("wrong_mock_payment_method")
    if str(params.get("trade_status") or "") != "MOCK_SUCCESS":
        raise AlipayNotificationError("mock_trade_not_successful")
    if str(params.get("out_trade_no") or "") != order_id:
        raise AlipayNotificationError("order_id_mismatch")
    try:
        paid = Decimal(str(params.get("total_amount"))).quantize(Decimal("0.01"))
        expected = Decimal(str(order.get("amount_cny"))).quantize(Decimal("0.01"))
        expires = int(params.get("expires_at_ms"))
        order_expires = int(order.get("expires_at_ms") or 0)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AlipayNotificationError("invalid_mock_payload") from exc
    if not paid.is_finite() or paid != expected:
        raise AlipayNotificationError("amount_mismatch")
    if expires != order_expires or expires < int(time.time() * 1000):
        raise AlipayNotificationError("mock_order_expired")
    token = os.getenv("ALIPAY_NOTIFY_TOKEN", "").strip()
    if not token:
        raise AlipayNotificationError("mock_token_missing")
    message = f"{order_id}|{format(paid, '.2f')}|{expires}".encode("utf-8")
    expected_sig = hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()
    supplied_sig = str(params.get("mock_signature") or params.get("sig") or "")
    if not supplied_sig or not hmac.compare_digest(supplied_sig, expected_sig):
        raise AlipayNotificationError("mock_signature_invalid")

    from payment_flow import _lock
    with _lock:
        orders = load_orders()
        target = next((x for x in orders if isinstance(x, dict)
                       and str(x.get("order_id") or "") == order_id), None)
        if target is None:
            raise AlipayNotificationError("order_not_found")
        if target.get("payment_status") == "mock_paid":
            return {"ok": True, "replayed": True, "mock": True, "order_id": order_id}
        if target.get("payment_status") not in ("pending", ""):
            raise AlipayNotificationError("order_not_pending")
        target.update({
            "payment_status": "mock_paid", "status": "mock_paid",
            "payment_verification": "mock_only_no_real_money_no_delivery",
            "mock_paid_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "mock_event_signature": supplied_sig,
        })
        # Explicitly leave procurement and delivery untouched.
        save_orders(orders)

    try:
        bot.send_message(int(target.get("user_id")),
            "🧪 模拟支付流程已跑通。\n"
            f"订单：{order_id}\n金额：¥{format(paid, '.2f')}\n\n"
            "这是测试结果，不代表真实支付宝收款；不会扣款，也不会采购或发货。")
    except Exception:
        pass
    try:
        if str(admin_id or "").isdigit():
            bot.send_message(int(admin_id), f"🧪 支付宝 Mock 测试完成：{order_id} · ¥{format(paid, '.2f')}；未产生真实收款或发货。")
    except Exception:
        pass
    return {"ok": True, "replayed": False, "mock": True, "order_id": order_id}


def process_vmq_notification(params, load_orders, save_orders, bot, admin_id=None, now_ms=None, on_order_paid=None):
    """Verify a szvone Android monitor event and settle one unique personal-QR order."""
    import re
    if not isinstance(params, dict) or params.get("event_type") != "vmq_android_push":
        raise AlipayNotificationError("invalid_vmq_event")
    try:
        from pathlib import Path
        token = Path("/root/.config/fakabot/vmq_notify_token").read_text(encoding="utf-8").strip()
    except OSError:
        token = os.getenv("VMQ_NOTIFY_TOKEN", "").strip()
    if not token:
        raise AlipayNotificationError("vmq_token_missing")
    protocol = str(params.get("vmq_protocol") or "")
    pay_id = str(params.get("payId") or params.get("out_trade_no") or "").strip()
    price_text = str(params.get("price") or params.get("total_amount") or "").strip()
    sign = str(params.get("vmq_signature") or "").strip().lower()
    payment_type = str(params.get("vmq_type") or "").strip()
    timestamp = str(params.get("vmq_timestamp") or "").strip()
    if payment_type != "2":
        raise AlipayNotificationError("vmq_not_alipay_payment")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", price_text) or not sign:
        raise AlipayNotificationError("vmq_invalid_amount_or_signature")
    try:
        paid = Decimal(price_text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise AlipayNotificationError("vmq_invalid_amount") from exc
    if not paid.is_finite() or paid <= 0:
        raise AlipayNotificationError("vmq_invalid_amount")
    if protocol == "szvone_appPush":
        if not re.fullmatch(r"\d{13}", timestamp):
            raise AlipayNotificationError("vmq_timestamp_invalid")
        current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        if abs(current_ms - int(timestamp)) > 50000:
            raise AlipayNotificationError("vmq_timestamp_expired")
        signed_text = payment_type + price_text + timestamp + token
        expected = hashlib.md5(signed_text.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(sign, expected):
            raise AlipayNotificationError("vmq_signature_invalid")
    elif protocol == "vmq_json":
        # JSON fork compatibility: accept its documented price+token signature,
        # or the stronger payId+price+token variant. Order uniqueness is mandatory
        # because the legacy price-only signature does not cryptographically bind payId.
        candidates = [hashlib.md5((price_text + token).encode("utf-8")).hexdigest()]
        if pay_id:
            candidates.append(hashlib.md5((pay_id + price_text + token).encode("utf-8")).hexdigest())
        if timestamp and re.fullmatch(r"\d{13}", timestamp):
            if abs(int(now_ms if now_ms is not None else time.time() * 1000) - int(timestamp)) > 50000:
                raise AlipayNotificationError("vmq_timestamp_expired")
            candidates.append(hashlib.md5((payment_type + price_text + timestamp + token).encode("utf-8")).hexdigest())
        if not any(hmac.compare_digest(sign, candidate) for candidate in candidates):
            raise AlipayNotificationError("vmq_signature_invalid")
    else:
        raise AlipayNotificationError("vmq_protocol_unsupported")

    orders = load_orders()
    if not isinstance(orders, list):
        raise AlipayNotificationError("order_store_unavailable")
    payment_ref = str(params.get("trade_no") or params.get("notify_id") or
                      ("VMQ-" + (pay_id or timestamp or format(paid, ".2f"))))
    replay = next((item for item in orders if isinstance(item, dict)
                   and item.get("payment_method") in ("alipay_qr", "alipay_gateway")
                   and (item.get("payment_method") != "alipay_gateway" or
                        item.get("payment_verification") in ("stateless_gateway", "stateless_gateway_test_only"))
                   and item.get("payment_status") in ("paid", "completed", "delivered")
                   and str(item.get("txid") or "") == payment_ref), None)
    if replay:
        return {"ok": True, "replayed": True, "order_id": str(replay.get("order_id") or ""),
                "event_id": payment_ref, "provider": "vmq_android_monitor"}
    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    matches = []
    for item in orders:
        if not isinstance(item, dict) or item.get("payment_method") not in ("alipay_qr", "alipay_gateway"):
            continue
        if item.get("payment_method") == "alipay_gateway" and item.get("payment_verification") not in ("stateless_gateway", "stateless_gateway_test_only"):
            continue
        if item.get("payment_status") not in ("pending", ""):
            continue
        try:
            expected_amount = Decimal(str(item.get("amount_cny"))).quantize(Decimal("0.01"))
            expiry = int(item.get("expires_at_ms") or 0)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if expected_amount == paid and expiry >= current_ms:
            matches.append(item)
    if len(matches) != 1:
        raise AlipayNotificationError("vmq_order_amount_ambiguous_or_not_found")
    order = matches[0]
    order_id = str(order.get("order_id") or "")
    if pay_id and pay_id != order_id:
        raise AlipayNotificationError("vmq_pay_id_mismatch")
    if not re.fullmatch(r"AL[A-F0-9]{12}", order_id):
        raise AlipayNotificationError("vmq_order_id_invalid")
    from payment_flow import handle_confirmed_payment
    handled = handle_confirmed_payment(
        bot, order_id,
        {"txid": payment_ref, "provider": "vmq_android_monitor",
         "manual_verification": "vmq_signed_android_notification",
         "amount_cny": format(paid, ".2f")},
        load_orders=load_orders, save_orders=save_orders, admin_id=admin_id,
        on_order_paid=on_order_paid,
    )
    return {"ok": bool(handled), "replayed": not bool(handled),
            "order_id": order_id, "event_id": payment_ref, "provider": "vmq_android_monitor"}


def process_alipay_notification(params, alipay_public_key_pem, expected_app_id,
                                expected_seller_id, load_orders, save_orders,
                                bot, admin_id=None, supplier_client_factory=None, on_order_paid=None):
    """Verify then apply one provider notification to the existing order flow."""
    orders = load_orders()
    if not isinstance(orders, list):
        raise AlipayNotificationError("order_store_unavailable")
    order_id = str(params.get("out_trade_no") or "")
    order = next((item for item in orders if isinstance(item, dict)
                  and str(item.get("order_id") or "") == order_id), None)
    if order is None:
        raise AlipayNotificationError("order_not_found")
    if str(params.get("mock") or "") == "1":
        return _process_mock_notification(params, order, order_id, load_orders, save_orders, bot, admin_id)
    if order.get("payment_method") not in ("alipay_qr", "alipay_gateway"):
        raise AlipayNotificationError("wrong_payment_method")
    # Always authenticate and match the provider payload before acknowledging a replay.
    event = verify_alipay_notification(params, alipay_public_key_pem,
        expected_app_id, expected_seller_id, order)
    if order.get("payment_status") in ("paid", "completed", "delivered"):
        return {"ok": True, "replayed": True, "order_id": order_id, "event_id": event["event_id"]}
    if int(order.get("expires_at_ms") or 0) < int(time.time() * 1000):
        raise AlipayNotificationError("order_expired")
    from payment_flow import handle_confirmed_payment
    handled = handle_confirmed_payment(
        bot, order_id,
        {"txid": event["payment_ref"], "provider": "alipay",
         "manual_verification": "provider_rsa2_notification",
         "amount_cny": event["amount_cny"]},
        load_orders=load_orders, save_orders=save_orders,
        admin_id=admin_id, supplier_client_factory=supplier_client_factory,
        on_order_paid=on_order_paid,
    )
    return {"ok": bool(handled), "replayed": not bool(handled),
            "order_id": order_id, "event_id": event["event_id"]}


def process_alipay_fund_order_changed(event, alipay_public_key_pem, expected_app_id,
                                     load_orders=None):
    """Verify and log an Alipay fund-order status message; never infer incoming payment."""
    import base64, json
    wrapped = event if isinstance(event, dict) else {}
    outer = wrapped.get("outer") if isinstance(wrapped.get("outer"), dict) else wrapped
    if str(outer.get("msg_method") or "") != "alipay.fund.trans.order.changed":
        raise AlipayNotificationError("fund_message_method_mismatch")
    if str(outer.get("sign_type") or "") != "RSA2":
        raise AlipayNotificationError("fund_signature_type_mismatch")
    if str(outer.get("app_id") or "") != str(expected_app_id or ""):
        raise AlipayNotificationError("fund_app_id_mismatch")
    signature = str(outer.get("sign") or "")
    if not signature:
        raise AlipayNotificationError("fund_signature_missing")
    try:
        key_data = alipay_public_key_pem.encode() if isinstance(alipay_public_key_pem, str) else alipay_public_key_pem
        public_key = serialization.load_pem_public_key(key_data)
        public_key.verify(base64.b64decode(signature, validate=True),
                          _canonical_message(outer), padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise AlipayNotificationError("fund_signature_verification_failed") from exc
    try:
        biz = wrapped.get("biz_content") if isinstance(wrapped.get("biz_content"), dict) else json.loads(str(outer.get("biz_content") or ""))
        if not isinstance(biz, dict):
            raise ValueError("invalid_biz_content")
    except (ValueError, TypeError) as exc:
        raise AlipayNotificationError("fund_biz_content_invalid") from exc
    amount = str(biz.get("trans_amount") or "")
    trade_no = str(biz.get("pay_fund_order_id") or biz.get("order_id") or "")
    remark = str(biz.get("remark") or biz.get("summary") or "")
    paid_at = str(biz.get("pay_date") or biz.get("trans_date") or outer.get("utc_timestamp") or "")
    status = str(biz.get("status") or "")
    action = str(biz.get("action_type") or "")
    out_biz_no = str(biz.get("out_biz_no") or "")
    matched_order = bool(__import__("re").fullmatch(r"AL[A-F0-9]{12}", remark))
    print("[ALIPAY_FUND_ORDER_CHANGED] " + json.dumps({
        "verified": True, "notify_id": str(outer.get("notify_id") or ""),
        "amount": amount, "alipay_trade_no": trade_no, "remark": remark,
        "transaction_time": paid_at, "status": status, "action_type": action,
        "out_biz_no": out_biz_no, "remark_is_order_id": matched_order,
    }, ensure_ascii=False, separators=(",", ":")), flush=True)
    # IMPORTANT: this API reports merchant-initiated fund transfers (outbound),
    # not incoming customer payments. A matching remark is logged as a candidate
    # only; it must not trigger fulfillment or mark an order paid.
    return {"ok": True, "event_type": "alipay_fund_order_changed",
            "remark_order_candidate": remark if matched_order else None,
            "amount": amount, "payment_ref": trade_no, "status": status}

[executed on device: iZj6cakb7vguedvzvbqrvbZ (14411471-502a-43de-a2d7-530edf7bf831)]
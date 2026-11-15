# -*- coding: utf-8 -*-
"""Client and authenticated webhook receiver for the stateless payment gateway."""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape

log = logging.getLogger("fakabot.payment_gateway")
_EVENT_LOCK = threading.RLock()


def _money(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid_amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("invalid_amount")
    return format(amount, ".2f")


def create_order(amount, business_id):
    """Create a 60-second Alipay matching order on the US gateway."""
    token = os.getenv("PAYMENT_GATEWAY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("支付网关 API_TOKEN 尚未配置")
    base = os.getenv("PAYMENT_GATEWAY_API_URL", "https://pay-gateway.lynli.me").strip().rstrip("/")
    callback = os.getenv("PAYMENT_GATEWAY_CALLBACK_URL", "https://api.lynli.me/webhook/alipay_success").strip()
    from urllib.parse import urlsplit
    base_parts = urlsplit(base)
    local_http = base_parts.scheme == "http" and base_parts.hostname in ("127.0.0.1", "localhost")
    if (not base.startswith("https://") and not local_http) or not callback.startswith("https://"):
        raise RuntimeError("支付网关必须使用 HTTPS（仅允许本机 SSH 隧道使用 HTTP），回调地址必须使用 HTTPS")
    amount_text = _money(amount)
    body = json.dumps({
        "amount": amount_text,
        "channel": "alipay",
        "business_id": str(business_id),
        "callback_url": callback,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base + "/api/create_order", data=body,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read(65536).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError("支付网关创建订单失败：HTTP " + str(exc.code)) from exc
    except Exception as exc:
        raise RuntimeError("支付网关连接失败：" + type(exc).__name__) from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("支付网关未能创建订单")
    if result.get("channel") != "alipay" or _money(result.get("amount")) != amount_text:
        raise RuntimeError("支付网关返回的渠道或金额不匹配")
    if not str(result.get("order_id") or "").strip():
        raise RuntimeError("支付网关未返回网关订单号")
    return result


class WebhookError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)


def _accept_event(payload, load_orders, save_orders, flush_orders):
    if not isinstance(payload, dict):
        raise WebhookError(400, "invalid_json_object")
    if (payload.get("event") != "payment.success" or payload.get("status") != "paid"
            or payload.get("channel") != "alipay"):
        raise WebhookError(400, "invalid_payment_event")
    gateway_order_id = str(payload.get("order_id") or "").strip()
    business_id = str(payload.get("business_id") or "").strip()
    tx_id = str(payload.get("tx_id") or "").strip()[:160]
    try:
        paid_amount = _money(payload.get("amount"))
        paid_time = int(payload.get("paid_time") or time.time())
    except (ValueError, TypeError, OverflowError):
        raise WebhookError(400, "invalid_payment_fields")
    if not gateway_order_id or not business_id or not tx_id:
        raise WebhookError(400, "missing_payment_fields")

    with _EVENT_LOCK:
        orders = load_orders()
        target = next((row for row in orders if isinstance(row, dict)
                       and str(row.get("order_id") or "") == business_id
                       and str(row.get("gateway_order_id") or "") == gateway_order_id), None)
        if target is None:
            raise WebhookError(404, "business_order_not_found")
        if target.get("payment_method") != "alipay_gateway":
            raise WebhookError(409, "payment_method_mismatch")
        if _money(target.get("amount_cny")) != paid_amount:
            raise WebhookError(409, "amount_mismatch")
        if target.get("gateway_webhook_received"):
            if (str(target.get("gateway_tx_id") or "") != tx_id
                    or str(target.get("gateway_paid_amount") or "") != paid_amount):
                raise WebhookError(409, "conflicting_duplicate_event")
            # A previous archive flush may have timed out; retry the flush before ACK.
            try:
                flush_orders()
            except Exception as exc:
                log.warning("duplicate webhook archive flush failed: %s", type(exc).__name__)
                raise WebhookError(503, "archive_temporarily_unavailable")
            return {"duplicate": True, "business_id": business_id}

        expiry = int(target.get("expires_at_ms") or 0)
        if target.get("payment_status") != "pending" or (expiry and int(time.time() * 1000) > expiry):
            if target.get("payment_status") == "pending":
                target.update({"payment_status": "expired", "status": "expired",
                               "expired_at": datetime.now(timezone.utc).isoformat(),
                               "expiry_notice_status": "pending"})
                save_orders(orders)
                try:
                    flush_orders()
                except Exception:
                    pass
            raise WebhookError(410, "business_order_expired_or_already_processed")

        paid_iso = datetime.fromtimestamp(paid_time, timezone.utc).isoformat()
        target.update({
            "gateway_webhook_received": True,
            "gateway_webhook_received_at": datetime.now(timezone.utc).isoformat(),
            "gateway_paid_amount": paid_amount,
            "gateway_tx_id": tx_id,
            "gateway_paid_time": paid_time,
            "gateway_paid_at": paid_iso,
            "txid": tx_id,
            "payment_status": "gateway_confirmed",
            "status": "payment_confirmed_pending_fulfillment",
            "payment_verification": "stateless_gateway_webhook",
        })
        save_orders(orders)
        try:
            flush_orders()
        except Exception as exc:
            log.warning("webhook archive flush failed: %s", type(exc).__name__)
            raise WebhookError(503, "archive_temporarily_unavailable")
    return {"duplicate": False, "business_id": business_id}


def start_webhook_server(load_orders, save_orders, flush_orders):
    """Expose the callback only on loopback; Nginx publishes the HTTPS route."""
    secret = os.getenv("PAYMENT_GATEWAY_WEBHOOK_TOKEN", "").strip()
    if not secret:
        raise RuntimeError("支付网关 Webhook 校验密钥尚未配置")

    class Handler(BaseHTTPRequestHandler):
        server_version = "FakabotPaymentWebhook/1.0"

        def log_message(self, fmt, *args):
            log.info("webhook_http %s", fmt % args)

        def _reply(self, status, data):
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                return self._reply(200, {"ok": True, "service": "fakabot-payment-webhook"})
            return self._reply(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            if self.path != "/webhook/alipay_success":
                return self._reply(404, {"ok": False, "error": "not_found"})
            supplied = self.headers.get("X-Payment-Gateway-Token", "")
            if not supplied or not hmac.compare_digest(supplied, secret):
                return self._reply(401, {"ok": False, "error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16384:
                    return self._reply(413, {"ok": False, "error": "invalid_body_size"})
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = _accept_event(payload, load_orders, save_orders, flush_orders)
                return self._reply(200, {"ok": True, **result})
            except WebhookError as exc:
                return self._reply(exc.status, {"ok": False, "error": str(exc)})
            except Exception as exc:
                log.exception("payment webhook internal error: %s", type(exc).__name__)
                return self._reply(500, {"ok": False, "error": "internal_error"})

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    port = int(os.getenv("PAYMENT_GATEWAY_WEBHOOK_PORT", "8011"))
    server = Server(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="payment-gateway-webhook", daemon=True)
    thread.start()
    return server


def gateway_order_worker_loop(bot, load_orders, save_orders, flush_orders, admin_id="", audit_channel_id="", on_order_paid=None):
    """Expire 60-second orders, fulfill accepted events idempotently, and archive deliveries."""
    from payment_flow import handle_confirmed_payment

    channel_id = str(audit_channel_id or os.getenv("PAYMENT_AUDIT_CHANNEL_ID", "") or "").strip()
    while True:
        try:
            orders = load_orders()
            now_ms = int(time.time() * 1000)
            changed = False
            for row in orders:
                if not isinstance(row, dict) or row.get("payment_method") != "alipay_gateway":
                    continue
                if (row.get("gateway_order_id") and row.get("payment_status") == "pending"
                        and int(row.get("expires_at_ms") or 0) > 0
                        and int(row.get("expires_at_ms") or 0) <= now_ms):
                    row.update({"payment_status": "expired", "status": "expired",
                                "expired_at": datetime.now(timezone.utc).isoformat(),
                                "expiry_notice_status": "pending"})
                    changed = True
            if changed:
                save_orders(orders)
                flush_orders()

            # Notify each expired buyer once; failed Telegram sends are retried.
            for row in orders:
                if (isinstance(row, dict) and row.get("payment_method") == "alipay_gateway"
                        and row.get("payment_status") == "expired"
                        and row.get("expiry_notice_status") != "sent"):
                    uid = str(row.get("user_id") or "")
                    try:
                        if uid.isdigit():
                            bot.send_message(int(uid),
                                "⌛ 支付订单已超过 60 秒有效期，系统已自动关闭。\n"
                                f"订单号：{row.get('order_id')}\n请重新下单。")
                        row["expiry_notice_status"] = "sent"
                        save_orders(orders)
                    except Exception as exc:
                        log.warning("expiry notice failed for %s: %s", row.get("order_id"), type(exc).__name__)

            # Fulfillment is deliberately outside the HTTP request. The accepted
            # payment event is already snapshotted to the private Telegram archive.
            for row in orders:
                if (not isinstance(row, dict) or row.get("payment_method") != "alipay_gateway"
                        or not row.get("gateway_webhook_received")
                        or row.get("payment_status") != "gateway_confirmed"):
                    continue
                order_id = str(row.get("order_id") or "")
                transfer = {
                    "txid": str(row.get("gateway_tx_id") or ""),
                    "provider": "stateless_payment_gateway",
                    "manual_verification": "authenticated_gateway_webhook",
                    "gateway_order_id": str(row.get("gateway_order_id") or ""),
                }
                try:
                    handled = handle_confirmed_payment(
                        bot, order_id, transfer, load_orders, save_orders,
                        admin_id=admin_id,
                        trusted_external_gateway=(row.get("sandbox_test_order") is not True),
                        on_order_paid=on_order_paid,
                    )
                    if handled:
                        # Do not rely only on the periodic flush loop after delivery.
                        flush_orders()
                except Exception as exc:
                    log.exception("gateway fulfillment failed for %s: %s", order_id, type(exc).__name__)

            # Archive only after delivery content has been committed and the buyer
            # notification has succeeded. Failed channel sends remain retryable.
            latest = load_orders()
            for row in latest:
                if (not isinstance(row, dict) or row.get("payment_method") not in ("alipay_gateway", "usdt_trc20")
                        or row.get("procurement_status") != "delivered"
                        or row.get("delivery_notification_status") != "sent"
                        or row.get("payment_audit_status") == "sent"):
                    continue
                if not channel_id or not channel_id.lstrip("-").isdigit():
                    log.warning("payment audit channel ID is not configured")
                    break
                try:
                    chat = bot.get_chat(int(channel_id))
                    if getattr(chat, "type", None) != "channel" or getattr(chat, "username", None):
                        raise RuntimeError("付款存档目标必须是私密 Telegram 频道")
                    is_usdt = row.get("payment_method") == "usdt_trc20"
                    transfer_meta = row.get("payment_transfer") or {}
                    if row.get("gateway_paid_time"):
                        paid_time = int(row["gateway_paid_time"])
                        paid_text = datetime.fromtimestamp(paid_time, timezone.utc).astimezone().isoformat(timespec="seconds")
                    elif transfer_meta.get("block_timestamp_ms"):
                        paid_text = datetime.fromtimestamp(int(transfer_meta["block_timestamp_ms"]) / 1000, timezone.utc).astimezone().isoformat(timespec="seconds")
                    else:
                        paid_text = str(row.get("paid_at") or row.get("created_at") or "—")
                    delivered_text = str(row.get("delivered_at") or datetime.now(timezone.utc).isoformat())
                    amount_value = (row.get("amount_usdt") or row.get("amount") or "") if is_usdt else (row.get("gateway_paid_amount") or row.get("amount_cny") or "")
                    amount_label = f"{escape(str(amount_value))} USDT" if is_usdt else f"¥{escape(str(amount_value))}"
                    tx_value = row.get("gateway_tx_id") or row.get("txid") or "—"
                    title_text = "USDT 链上到账" if is_usdt else "支付宝到账"
                    text = (
                        f"✅ <b>{title_text} · 自动发货存档</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"业务订单：<code>{escape(str(row.get('order_id') or ''))}</code>\n"
                        f"网关订单：<code>{escape(str(row.get('gateway_order_id') or '—'))}</code>\n"
                        f"商品：{escape(str(row.get('product_name') or '商品'))}\n"
                        f"金额：<b>{amount_label}</b>\n"
                        f"交易号：<code>{escape(str(tx_value))}</code>\n"
                        f"到账时间：{escape(paid_text)}\n"
                        f"发货时间：{escape(delivered_text)}\n"
                        f"用户 ID：<code>{escape(str(row.get('user_id') or ''))}</code>\n"
                        "状态：已完成发货"
                    )
                    sent = bot.send_message(int(channel_id), text, parse_mode="HTML", disable_web_page_preview=True)
                    row["payment_audit_status"] = "sent"
                    row["payment_audit_message_id"] = getattr(sent, "message_id", None)
                    row["payment_audit_sent_at"] = datetime.now(timezone.utc).isoformat()
                    save_orders(latest)
                    flush_orders()
                except Exception as exc:
                    log.warning("payment audit send failed for %s: %s", row.get("order_id"), type(exc).__name__)
        except Exception as exc:
            log.exception("gateway order worker iteration failed: %s", type(exc).__name__)
        time.sleep(2)

[executed on device: iZj6cakb7vguedvzvbqrvbZ (14411471-502a-43de-a2d7-530edf7bf831)]
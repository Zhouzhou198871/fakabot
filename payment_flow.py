# -*- coding: utf-8 -*-
"""TRC20 USDT order creation and payment confirmation integration."""
import os, uuid, threading, io
import qrcode
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from payment_listener import PaymentWatcher

class PaymentConfigurationError(RuntimeError):
    pass

_lock = threading.RLock()
ORDER_TTL_SECONDS = max(300, int(os.getenv("PAYMENT_ORDER_TTL_SECONDS", "1200")))

def create_payment_order(user_id, product, load_orders, save_orders):
    receive_address = os.getenv("TRON_RECEIVE_ADDRESS", "TTT5MV8xeZqKaPDxUcDjR8bPFz2ce5kmBr").strip()
    if not receive_address:
        raise PaymentConfigurationError("收款地址尚未配置，请联系客服。")
    try:
        base = Decimal(str(product.get("price"))).quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentConfigurationError("商品价格配置无效，请联系客服。")
    if not base.is_finite() or base <= 0:
        raise PaymentConfigurationError("商品价格配置无效，请联系客服。")
    with _lock:
        orders = load_orders()
        if not isinstance(orders, list): raise RuntimeError("订单归档格式错误")
        active = {str(x.get("amount_raw")) for x in orders if isinstance(x, dict) and x.get("payment_status") == "pending" and x.get("status") == "pending_payment"}
        amount = None
        for offset in range(1, 1000):
            candidate = base + Decimal(offset) / Decimal(1000000)
            raw = str(int(candidate * Decimal(1000000)))
            if raw not in active:
                amount = candidate
                break
        if amount is None: raise RuntimeError("当前同价待支付订单过多")
        now = datetime.now(timezone.utc)
        oid = "QT" + uuid.uuid4().hex[:12].upper()
        order = {"order_id": oid, "user_id": str(user_id), "product_id": str(product.get("id") or ""), "product_name": str(product.get("name") or "商品"), "amount": float(amount), "amount_usdt": float(amount), "amount_raw": str(int(amount * Decimal(1000000))), "created_at": now.isoformat(), "expires_at_ms": int((now.timestamp() + ORDER_TTL_SECONDS) * 1000), "payment_status": "pending", "status": "pending_payment", "procurement_status": "blocked_supplier_session"}
        orders.append(order)
        save_orders(orders)
    minutes = max(1, ORDER_TTL_SECONDS // 60)
    message = ("💳 <b>USDT（TRC20）支付订单</b>\n" + "━━━━━━━━━━━━━━━━━━\n" + f"订单号：<code>{oid}</code>\n商品：{order['product_name']}\n应付金额：<code>{amount:.6f} USDT</code>\n收款网络：<b>TRON / TRC20</b>\n收款地址：<code>{receive_address}</code>\n\n请在 {minutes} 分钟内支付准确金额。链上确认后系统会更新订单。\n⚠️ 请勿使用其他网络或少付/多付。")
    return order, message

def create_payment_qr(address):
    """Return a Telegram-uploadable PNG QR containing the TRON address."""
    image = qrcode.make(str(address))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    buffer.name = "trc20-payment-qr.png"
    return buffer

def handle_confirmed_payment(bot, order_id, transfer, load_orders, save_orders, admin_id=""):
    with _lock:
        orders = load_orders()
        target = next((x for x in orders if isinstance(x, dict) and str(x.get("order_id")) == str(order_id)), None)
        if not target or target.get("payment_status") in ("paid", "completed", "delivered"): return False
        target.update({"payment_status": "paid", "status": "paid", "paid_at": datetime.now(timezone.utc).isoformat(), "txid": str(transfer.get("txid") or ""), "procurement_status": "blocked_supplier_session"})
        target["payment_transfer"] = {k: transfer.get(k) for k in ("from", "to", "amount_raw", "block_timestamp_ms", "contract")}
        save_orders(orders)
    uid = str(target.get("user_id") or "")
    text = ("✅ <b>链上付款已确认</b>\n" + f"订单：<code>{target.get('order_id')}</code>\n金额：<code>{float(target.get('amount') or 0):.6f} USDT</code>\n交易：<code>{target.get('txid') or '—'}</code>\n\n上游采购授权尚未完成，商品暂未发货；请勿重复付款。")
    try: bot.send_message(int(uid), text, parse_mode="HTML")
    except Exception: pass
    if str(admin_id).isdigit():
        try: bot.send_message(int(admin_id), "🔔 已确认付款，但采购会话尚未授权。\n" + f"订单：{target.get('order_id')} · 用户：{uid} · TX：{target.get('txid')}")
        except Exception: pass
    return True

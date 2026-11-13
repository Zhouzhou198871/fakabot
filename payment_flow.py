# -*- coding: utf-8 -*-
"""TRC20 USDT order creation, confirmation, and guarded supplier delivery."""
import os, uuid, threading, io
import qrcode
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from payment_listener import PaymentWatcher

class PaymentConfigurationError(RuntimeError):
    pass

_lock = threading.RLock()
ORDER_TTL_SECONDS = max(300, int(os.getenv("PAYMENT_ORDER_TTL_SECONDS", "1200")))

USDT_CNY_RATE = Decimal("8")

def price_cny(usdt_price):
    return (Decimal(str(usdt_price)) * USDT_CNY_RATE).quantize(Decimal("0.01"))

def create_alipay_order(user_id, product, load_orders, save_orders):
    if (product.get("supplier_verified") is not True or product.get("procurement_ready") is not True
        or not str(product.get("supplier_product_id") or "").strip()
        or not str(product.get("supplier_url") or "").strip()):
        raise PaymentConfigurationError("该商品尚未完成上游商品核验与自动采购验证，暂不接受付款。")
    try:
        base = Decimal(str(product.get("price")))
        cost = Decimal(str(product.get("supplier_price_cny"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentConfigurationError("商品价格配置无效，请联系客服。")
    if not base.is_finite() or base <= 0 or not cost.is_finite() or cost <= 0:
        raise PaymentConfigurationError("商品价格配置无效，请联系客服。")
    if base.quantize(Decimal("0.0001")) != (cost / Decimal("4")).quantize(Decimal("0.0001")):
        raise PaymentConfigurationError("商品售价与上游人民币成本及利润规则不一致，暂不接受付款。")
    cny = price_cny(base)
    oid = "AL" + uuid.uuid4().hex[:12].upper()
    try:
        from alipay_gateway import build_page_pay_url
        payment_url = build_page_pay_url(oid, cny, str(product.get("name") or "Fakabot商品"))
    except Exception as exc:
        raise PaymentConfigurationError(f"支付宝官方支付网关尚未配置：{exc}") from exc
    with _lock:
        orders = load_orders()
        if not isinstance(orders, list): raise RuntimeError("订单归档格式错误")
        now = datetime.now(timezone.utc)
        order = {
            "order_id": oid, "user_id": str(user_id), "product_id": str(product.get("id") or ""),
            "product_name": str(product.get("name") or "商品"),
            "supplier_product_id": str(product.get("supplier_product_id") or ""),
            "supplier_category_id": str(product.get("category_id") or ""),
            "supplier_model_id": str(product.get("supplier_model_id") or ""),
            "supplier_email": str(product.get("supplier_email") or ""), "supplier_quantity": 1,
            "supplier_price_cny": format(cost, ".2f"),
            "supplier_verified": product.get("supplier_verified") is True,
            "procurement_ready": product.get("procurement_ready") is True,
            "payment_method": "alipay_gateway", "payment_url": payment_url,
            "amount": float(cny), "amount_cny": format(cny, ".2f"), "amount_usdt": str(base),
            "exchange_rate": "8", "created_at": now.isoformat(),
            "expires_at_ms": int((now.timestamp() + ORDER_TTL_SECONDS) * 1000),
            "payment_status": "pending", "status": "pending_payment",
            "procurement_status": "awaiting_customer_payment", "payment_verification": "provider_notification_required"
        }
        orders.append(order); save_orders(orders)
    message = (f"🟦 <b>支付宝官方沙箱支付</b>\n━━━━━━━━━━━━━━━━━━\n订单号：<code>{oid}</code>\n"
               f"商品：{order['product_name']}\n应付金额：<b>¥{cny:.2f}</b>\n汇率：1 USDT = 8 CNY\n\n"
               "点击下方按钮进入支付宝收银台；支付结果由支付宝服务器异步通知，验证通过后自动确认订单。")
    return order, message


def create_payment_order(user_id, product, load_orders, save_orders):
    # Never accept customer funds for catalog rows that have not been verified
    # against the live supplier catalog and procurement path.
    if (
        product.get("supplier_verified") is not True
        or product.get("procurement_ready") is not True
        or not str(product.get("supplier_product_id") or "").strip()
        or not str(product.get("supplier_url") or "").strip()
    ):
        raise PaymentConfigurationError("该商品尚未完成上游商品核验与自动采购验证，暂不接受付款。")
    receive_address = os.getenv("TRON_RECEIVE_ADDRESS", "TTT5MV8xeZqKaPDxUcDjR8bPFz2ce5kmBr").strip()
    if not receive_address:
        raise PaymentConfigurationError("收款地址尚未配置，请联系客服。")
    try:
        base = Decimal(str(product.get("price"))).quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentConfigurationError("商品价格配置无效，请联系客服。")
    if not base.is_finite() or base <= 0:
        raise PaymentConfigurationError("商品价格配置无效，请联系客服。")
    try:
        supplier_cost = Decimal(str(product.get("supplier_price_cny"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentConfigurationError("上游采购成本未核实，暂不接受付款。")
    expected_sell_price = (supplier_cost / Decimal("4")).quantize(Decimal("0.0001"))
    if not supplier_cost.is_finite() or supplier_cost <= 0 or base != expected_sell_price:
        raise PaymentConfigurationError("商品售价与上游人民币成本及利润规则不一致，暂不接受付款。")
    with _lock:
        orders = load_orders()
        if not isinstance(orders, list):
            raise RuntimeError("订单归档格式错误")
        active = {
            str(x.get("amount_raw")) for x in orders
            if isinstance(x, dict)
            and x.get("payment_status") == "pending"
            and x.get("status") == "pending_payment"
        }
        amount = None
        for offset in range(1, 1000):
            candidate = base + Decimal(offset) / Decimal(1000000)
            raw = str(int(candidate * Decimal(1000000)))
            if raw not in active:
                amount = candidate
                break
        if amount is None:
            raise RuntimeError("当前同价待支付订单过多")
        now = datetime.now(timezone.utc)
        oid = "QT" + uuid.uuid4().hex[:12].upper()
        order = {
            "order_id": oid,
            "user_id": str(user_id),
            "product_id": str(product.get("id") or ""),
            "product_name": str(product.get("name") or "商品"),
            "supplier_product_id": str(product.get("supplier_product_id") or ""),
            "supplier_category_id": str(product.get("category_id") or ""),
            "supplier_model_id": str(product.get("supplier_model_id") or ""),
            "supplier_email": str(product.get("supplier_email") or ""),
            "supplier_quantity": 1,
            "supplier_price_cny": format(supplier_cost, ".2f"),
            "supplier_verified": product.get("supplier_verified") is True,
            "procurement_ready": product.get("procurement_ready") is True,
            "amount": float(amount),
            "amount_usdt": float(amount),
            "amount_raw": str(int(amount * Decimal(1000000))),
            "created_at": now.isoformat(),
            "expires_at_ms": int((now.timestamp() + ORDER_TTL_SECONDS) * 1000),
            "payment_status": "pending",
            "status": "pending_payment",
            "procurement_status": "awaiting_customer_payment",
        }
        orders.append(order)
        save_orders(orders)
    minutes = max(1, ORDER_TTL_SECONDS // 60)
    message = (
        "💳 <b>USDT（TRC20）支付订单</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"订单号：<code>{oid}</code>\n"
        f"商品：{order['product_name']}\n"
        f"应付金额：<code>{amount:.6f} USDT</code>\n"
        f"固定汇率参考：<code>¥{price_cny(base):.2f} CNY</code>（1 USDT = 8 CNY）\n"
        "收款网络：<b>TRON / TRC20</b>\n"
        f"收款地址：<code>{receive_address}</code>\n\n"
        f"请在 {minutes} 分钟内支付准确金额。链上确认后系统会更新订单。\n"
        "⚠️ 请勿使用其他网络或少付/多付。"
    )
    return order, message


def create_payment_qr(address):
    """Return a Telegram-uploadable PNG QR containing the TRON address."""
    image = qrcode.make(str(address))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    buffer.name = "trc20-payment-qr.png"
    return buffer


def _save_order_updates(order_id, updates, load_orders, save_orders):
    with _lock:
        orders = load_orders()
        target = next(
            (x for x in orders if isinstance(x, dict) and str(x.get("order_id")) == str(order_id)),
            None,
        )
        if not target:
            return None
        target.update(updates)
        save_orders(orders)
        return dict(target)


def _notify(bot, user_id, text):
    """Send long delivery text in Telegram-safe chunks; report failures to caller."""
    if not str(user_id).isdigit():
        return False
    text = str(text or "")
    chunks = []
    while text:
        if len(text) <= 3500:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, 3500)
        if cut < 1000:
            cut = 3500
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    try:
        for chunk in chunks or [""]:
            # Supplier content is untrusted text; disable HTML parsing so credentials
            # containing angle brackets/underscores cannot break Telegram delivery.
            bot.send_message(int(user_id), chunk, parse_mode=None)
        return True
    except Exception:
        return False


def _notify_admin(bot, admin_id, text):
    try:
        if str(admin_id).isdigit():
            bot.send_message(int(admin_id), text)
    except Exception:
        pass


def handle_confirmed_payment(
    bot, order_id, transfer, load_orders, save_orders, admin_id="",
    supplier_client_factory=None,
):
    """Mark an exact on-chain payment once; optionally procure once, never retry blindly."""
    auto_procurement = os.getenv("SUPPLIER_AUTOPROCUREMENT_ENABLED", "0") == "1"
    with _lock:
        orders = load_orders()
        target = next(
            (x for x in orders if isinstance(x, dict) and str(x.get("order_id")) == str(order_id)),
            None,
        )
        if not target or target.get("payment_status") in ("paid", "completed", "delivered"):
            return False
        target.update({
            "payment_status": "paid",
            "status": "paid",
            "paid_at": datetime.now(timezone.utc).isoformat(),
            "txid": str(transfer.get("txid") or ""),
            "procurement_status": "procurement_processing" if auto_procurement else "blocked_supplier_session",
        })
        target["payment_transfer"] = {
            k: transfer.get(k)
            for k in ("from", "to", "amount_raw", "block_timestamp_ms", "contract")
        }
        target["payment_transfer"]["provider"] = transfer.get("provider") or target.get("payment_method") or "trc20_usdt"
        target["payment_transfer"]["manual_verification"] = transfer.get("manual_verification") or ""
        save_orders(orders)
        target = dict(target)

    uid = str(target.get("user_id") or "")
    if not auto_procurement or target.get("supplier_verified") is not True or target.get("procurement_ready") is not True:
        reason = (
            "上游自动采购开关尚未启用" if not auto_procurement else
            "该订单缺少付款前的上游实时核验记录"
        )
        amount_text = (
            f"金额：¥{target.get('amount_cny')} CNY\n"
            if target.get("payment_method") == "alipay_qr"
            else f"金额：{float(target.get('amount') or 0):.6f} USDT\n"
        )
        text = (
            "✅ 付款已确认\n"
            f"订单：{target.get('order_id')}\n"
            + amount_text
            + f"支付参考：{target.get('txid') or '—'}\n\n"
            + f"{reason}，商品暂未发货；请勿重复付款。"
        )
        _notify(bot, uid, text)
        _notify_admin(
            bot, admin_id,
            f"🔔 已确认付款，但{reason}。\n"
            f"订单：{target.get('order_id')} · 用户：{uid} · TX：{target.get('txid')}",
        )
        return True

    # Checkout-time live preflight is required; after verified payment, submit once only.
    try:
        supplier_id = str(target.get("supplier_product_id") or "").strip()
        if not supplier_id:
            raise RuntimeError("订单缺少已快照的上游商品 ID")
        if supplier_client_factory is None:
            from supplier_client import DahaoshangClient
            supplier_client_factory = DahaoshangClient
        client = supplier_client_factory()
        result = client.buy_with_balance(
            supplier_id,
            quantity=int(target.get("supplier_quantity") or 1),
            model_id=target.get("supplier_model_id") or None,
            email=target.get("supplier_email") or "",
            expected_price_cny=target.get("supplier_price_cny"),
        )
        delivered = _save_order_updates(
            order_id,
            {
                "supplier_order_id": str(result.get("supplier_order_id") or ""),
                "supplier_order_no": str(result.get("supplier_order_no") or ""),
                "delivery_content": str(result.get("content") or ""),
                "procurement_status": "delivered",
                "status": "delivered",
                "delivered_at": datetime.now(timezone.utc).isoformat(),
            },
            load_orders,
            save_orders,
        )
        if not delivered or not delivered.get("delivery_content"):
            raise RuntimeError("上游采购结果未能安全写入订单归档")
        sent = _notify(
            bot, uid,
            "✅ 付款已确认，上游采购成功。\n"
            f"订单：{delivered.get('order_id')}\n"
            f"上游订单：{delivered.get('supplier_order_no') or delivered.get('supplier_order_id')}\n\n"
            "📦 商品交付内容：\n" + delivered["delivery_content"],
        )
        _save_order_updates(
            order_id,
            {"delivery_notification_status": "sent" if sent else "pending",
             "delivery_notification_attempted_at": datetime.now(timezone.utc).isoformat()},
            load_orders, save_orders,
        )
        if not sent:
            _notify_admin(bot, admin_id,
                "⚠️ 上游已采购并保存交付内容，但 Telegram 发货通知失败；等待自动补发。\n"
                f"订单：{order_id}")
        _notify_admin(
            bot, admin_id,
            "✅ 自动采购与交付成功。\n"
            f"订单：{delivered.get('order_id')} · 上游订单：{delivered.get('supplier_order_id')}",
        )
        return True
    except Exception as exc:
        # A timeout or lost response may occur after the supplier created an
        # order. Never retry a purchase automatically; reconcile manually.
        supplier_order_id = getattr(exc, "order_id", "")
        state = "supplier_order_pending_delivery" if supplier_order_id else "needs_manual_reconciliation"
        updates = {
            "procurement_status": state,
            "procurement_error_type": type(exc).__name__,
            "procurement_error_at": datetime.now(timezone.utc).isoformat(),
        }
        if supplier_order_id:
            updates["supplier_order_id"] = str(supplier_order_id)
        _save_order_updates(order_id, updates, load_orders, save_orders)
        _notify(
            bot, uid,
            "✅ 链上付款已确认。\n"
            f"订单：{target.get('order_id')}\n"
            "上游订单状态正在核对，暂未确认交付内容。客服会继续处理，请勿重复付款。",
        )
        _notify_admin(
            bot, admin_id,
            "⚠️ 付款已确认，但上游采购/交付需要人工核对。\n"
            f"订单：{target.get('order_id')} · 错误类型：{type(exc).__name__}"
            + (f" · 上游订单：{supplier_order_id}" if supplier_order_id else ""),
        )
        return True


def reconcile_pending_supplier_deliveries(
    bot, load_orders, save_orders, admin_id="", supplier_client_factory=None,
):
    """Poll only already-created supplier orders; never submit a second purchase."""
    orders = load_orders()
    if not isinstance(orders, list):
        return 0
    # Retry Telegram delivery for content already procured and safely archived.
    notification_pending = [
        dict(row) for row in orders
        if isinstance(row, dict)
        and row.get("payment_status") == "paid"
        and row.get("procurement_status") == "delivered"
        and row.get("delivery_notification_status") == "pending"
        and str(row.get("delivery_content") or "").strip()
    ]
    for row in notification_pending:
        order_id = str(row.get("order_id") or "")
        sent = _notify(
            bot, row.get("user_id"),
            "✅ 付款已确认，上游采购成功。\n"
            f"订单：{order_id}\n"
            f"上游订单：{row.get('supplier_order_no') or row.get('supplier_order_id') or '—'}\n\n"
            "📦 商品交付内容：\n" + str(row.get("delivery_content")),
        )
        if sent:
            _save_order_updates(order_id, {
                "delivery_notification_status": "sent",
                "delivery_notification_attempted_at": datetime.now(timezone.utc).isoformat(),
            }, load_orders, save_orders)
    pending = [
        dict(row) for row in orders
        if isinstance(row, dict)
        and row.get("payment_status") == "paid"
        and row.get("procurement_status") == "supplier_order_pending_delivery"
        and str(row.get("supplier_order_id") or "").strip()
        and not str(row.get("delivery_content") or "").strip()
    ]
    if not pending:
        return 0
    try:
        if supplier_client_factory is None:
            from supplier_client import DahaoshangClient
            supplier_client_factory = DahaoshangClient
        client = supplier_client_factory()
    except Exception:
        return 0

    delivered_count = 0
    for row in pending:
        order_id = str(row.get("order_id") or "")
        supplier_order_id = str(row.get("supplier_order_id") or "")
        try:
            result = client.get_order(supplier_order_id)
            content = str(result.get("content") or "").strip()
            if not content:
                continue
            delivered = _save_order_updates(
                order_id,
                {
                    "delivery_content": content,
                    "supplier_status": result.get("supplier_status"),
                    "procurement_status": "delivered",
                    "status": "delivered",
                    "delivered_at": datetime.now(timezone.utc).isoformat(),
                    "procurement_reconciled_at": datetime.now(timezone.utc).isoformat(),
                },
                load_orders,
                save_orders,
            )
            if not delivered or not delivered.get("delivery_content"):
                continue
            sent = _notify(
                bot, delivered.get("user_id"),
                "✅ 上游订单已完成发货。\n"
                f"订单：{delivered.get('order_id')}\n"
                f"上游订单：{supplier_order_id}\n\n"
                "📦 商品交付内容：\n" + delivered["delivery_content"],
            )
            _save_order_updates(order_id, {
                "delivery_notification_status": "sent" if sent else "pending",
                "delivery_notification_attempted_at": datetime.now(timezone.utc).isoformat(),
            }, load_orders, save_orders)
            if not sent:
                _notify_admin(bot, admin_id,
                    "⚠️ 延迟交付内容已保存，但 Telegram 发货通知失败；等待自动补发。\n"
                    f"订单：{order_id}")
            _notify_admin(
                bot, admin_id,
                "✅ 延迟交付已自动补齐。\n"
                f"订单：{order_id} · 上游订单：{supplier_order_id}",
            )
            delivered_count += 1
        except Exception:
            # Pending upstream fulfillment and transient network errors are
            # retried on the next polling cycle. No purchase endpoint is called.
            continue
    return delivered_count
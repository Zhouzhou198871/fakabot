
# -*- coding: utf-8 -*-
"""TRC20 USDT order creation, confirmation, and guarded supplier delivery."""
import os, uuid, threading, io, hmac, hashlib, fcntl
import qrcode
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from contextlib import contextmanager
from payment_listener import PaymentWatcher

class PaymentConfigurationError(RuntimeError):
    pass

_lock = threading.RLock()
ORDER_TTL_SECONDS = max(300, int(os.getenv("PAYMENT_ORDER_TTL_SECONDS", "1200")))

USDT_CNY_RATE = Decimal("8")

def price_cny(usdt_price):
    return (Decimal(str(usdt_price)) * USDT_CNY_RATE).quantize(Decimal("0.01"))

def create_alipay_order(user_id, product, load_orders, save_orders, mode_override=None):
    if (product.get("supplier_verified") is not True or product.get("procurement_ready") is not True
        or not str(product.get("supplier_product_id") or "").strip()
        or not str(product.get("supplier_url") or "").strip()):
        raise PaymentConfigurationError("该商品尚未完成商品信息核验，暂不接受付款。")
    try:
        base = Decimal(str(product.get("price")))
        cost = Decimal(str(product.get("supplier_price_cny"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentConfigurationError("商品价格配置无效，请联系客服。")
    if not base.is_finite() or base <= 0 or not cost.is_finite() or cost <= 0:
        raise PaymentConfigurationError("商品价格配置无效，请联系客服。")
    if base.quantize(Decimal("0.0001")) != (cost / Decimal("4")).quantize(Decimal("0.0001")):
        raise PaymentConfigurationError("商品售价配置不一致，暂不接受付款。")
    cny = price_cny(base)
    oid = "AL" + uuid.uuid4().hex[:12].upper()
    now = datetime.now(timezone.utc)
    mode = str(mode_override or os.getenv("ALIPAY_PAYMENT_MODE", "mock")).strip().lower()
    ttl_seconds = 60 if mode == "stateless_gateway" else ORDER_TTL_SECONDS
    expires_at_ms = int((now.timestamp() + ttl_seconds) * 1000)
    alipay_qr_code = ""
    if mode == "mock":
        # Mock is deliberately isolated: it never creates a provider payment request
        # and its confirmation path must never trigger procurement or delivery.
        token = os.getenv("ALIPAY_NOTIFY_TOKEN", "").strip()
        if not token:
            raise PaymentConfigurationError("收款验证凭据未配置，暂不能创建验证订单。")
        amount_text = format(cny, ".2f")
        expires_text = str(expires_at_ms)
        message = f"{oid}|{amount_text}|{expires_text}".encode("utf-8")
        signature = hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()
        mock_base = os.getenv("ALIPAY_MOCK_BASE_URL",
            "https://fakabot-alipay-notify.chulingli1988.workers.dev/mock").strip().rstrip("/")
        payment_url = mock_base + "?" + urlencode({
            "order_id": oid, "total_amount": amount_text, "expires_at_ms": expires_text, "sig": signature
        })
        payment_method = "alipay_mock"
        payment_verification = "mock_only_no_real_money_no_delivery"
    elif mode == "stateless_gateway":
        payment_url = "personal_qr"
        payment_method = "alipay_gateway"
        payment_verification = "stateless_gateway_webhook"
    elif mode == "personal_qr":
        qr_file = os.getenv("ALIPAY_PERSONAL_QR_FILE", "/root/fakabot/alipay_receive_qr.png")
        if not os.path.isfile(qr_file):
            raise PaymentConfigurationError("支付宝个人收款码尚未上传，请先配置收款码。")
        payment_url = "personal_qr"
        payment_method = "alipay_qr"
        payment_verification = "personal_qr_manual_confirmation_pending_server_listener"
    elif mode == "real":
        from alipay_gateway import create_precreate_qr
        try:
            alipay_qr_code, precreate_result = create_precreate_qr(
                oid, cny, str(product.get("name") or "晴天商城商品"))
        except Exception as exc:
            raise PaymentConfigurationError(f"支付宝官方二维码创建失败：{exc}") from exc
        # Store the exact QR payload returned by Alipay after RSA2 response verification.
        payment_url = alipay_qr_code
        payment_method = "alipay_gateway"
        payment_verification = "provider_notification_required"
    else:
        raise PaymentConfigurationError("ALIPAY_PAYMENT_MODE 仅允许 mock、personal_qr 或 real。")
    with _lock:
        orders = load_orders()
        if not isinstance(orders, list): raise RuntimeError("订单归档格式错误")
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
            "payment_method": payment_method, "payment_url": payment_url,
            "alipay_qr_code": (alipay_qr_code if payment_method == "alipay_gateway" else ""),
            "amount": float(cny), "amount_cny": format(cny, ".2f"), "amount_usdt": str(base),
            "exchange_rate": "8", "created_at": now.isoformat(),
            "expires_at_ms": expires_at_ms,
            "payment_status": "pending", "status": "pending_payment",
            "procurement_status": "awaiting_customer_payment", "payment_verification": payment_verification
        }
        orders.append(order); save_orders(orders)
    if mode == "stateless_gateway":
        message = (f"🟦 <b>支付宝扫码付款</b>\n━━━━━━━━━━━━━━━━━━\n订单号：<code>{oid}</code>\n"
                   f"商品：{order['product_name']}\n应付金额：<b>¥{cny:.2f}</b>\n\n"
                   "请扫描下方个人收款码，并在 60 秒内按准确金额付款。到账通知核验成功后自动处理订单。")
    elif payment_method == "alipay_mock":
        message = (f"🧪 <b>支付宝沙箱模拟支付</b>\n━━━━━━━━━━━━━━━━━━\n订单号：<code>{oid}</code>\n"
                   f"商品：{order['product_name']}\n验证金额：<b>¥{cny:.2f}</b>\n\n"
                   "此页面仅用于核对收款通知，不会扣款，也不会发货。")
    elif payment_method == "alipay_qr":
        message = (f"🟦 <b>支付宝个人收款码</b>\n━━━━━━━━━━━━━━━━━━\n订单号：<code>{oid}</code>\n"
                   f"商品：{order['product_name']}\n应付金额：<b>¥{cny:.2f}</b>\n\n"
                   "请在收银台扫描个人收款码并按订单金额付款。到账核验完成前请勿重复付款。")
    else:
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
        raise PaymentConfigurationError("该商品尚未完成商品信息核验，暂不接受付款。")
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
        raise PaymentConfigurationError("商品成本信息未核实，暂不接受付款。")
    expected_sell_price = (supplier_cost / Decimal("4")).quantize(Decimal("0.0001"))
    if not supplier_cost.is_finite() or supplier_cost <= 0 or base != expected_sell_price:
        raise PaymentConfigurationError("商品售价配置不一致，暂不接受付款。")
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
            "payment_method": "usdt_trc20",
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



def create_custom_topup_order(user_id, requested_amount, load_orders, save_orders):
    """Create an exact-amount TRC20 wallet top-up order with a unique six-decimal suffix."""
    try:
        base = Decimal(str(requested_amount))
        if not base.is_finite() or base < Decimal("10") or base.as_tuple().exponent < -6:
            raise InvalidOperation
        base = base.quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentConfigurationError("自定义充值金额无效，最低 10 USDT，最多 6 位小数。")
    with _lock:
        orders = load_orders()
        if not isinstance(orders, list):
            raise RuntimeError("订单归档格式错误")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        active = {str(o.get("amount_raw")) for o in orders
                  if isinstance(o, dict)
                  and o.get("payment_method") in ("usdt_trc20", "usdt_wallet_topup")
                  and (o.get("payment_status") == "pending" and o.get("status") == "pending_payment"
                       or o.get("payment_status") == "expired"
                       and now_ms - int(o.get("expires_at_ms", 0) or 0) <= 24 * 60 * 60 * 1000)}
        amount = None
        # Deterministically scan available suffixes; never reuse a live pending amount.
        start = int(uuid.uuid4().hex[:6], 16) % 999999 + 1
        for offset in range(999999):
            suffix = ((start - 1 + offset) % 999999) + 1
            candidate = base + Decimal(suffix) / Decimal(1000000)
            raw = str(int(candidate * Decimal(1000000)))
            if raw not in active:
                amount = candidate
                break
        if amount is None:
            raise RuntimeError("当前金额可用的唯一尾数已耗尽，请稍后重试")
        now = datetime.now(timezone.utc)
        oid = "TU" + uuid.uuid4().hex[:12].upper()
        order = {
            "order_id": oid, "user_id": str(user_id), "product_id": "wallet_topup",
            "product_name": "USDT 账户余额充值", "payment_method": "usdt_wallet_topup",
            "amount": float(amount), "amount_usdt": format(amount, ".6f"),
            "amount_raw": str(int(amount * Decimal(1000000))),
            "requested_amount_usdt": format(base, ".6f"), "exchange_rate": "8",
            "amount_cny": format(price_cny(amount), ".2f"),
            "created_at": now.isoformat(),
            "expires_at_ms": int((now.timestamp() + ORDER_TTL_SECONDS) * 1000),
            "payment_status": "pending", "status": "pending_payment",
            "procurement_status": "not_applicable", "wallet_topup": True,
        }
        orders.append(order)
        save_orders(orders)
    return order


def create_payment_qr(address):
    """Return a Telegram-uploadable PNG QR containing the TRON address."""
    image = qrcode.make(str(address))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    buffer.name = "trc20-payment-qr.png"
    return buffer


def create_alipay_payment_qr(payment_url):
    """Return a Telegram-uploadable PNG QR for the signed Alipay cashier URL."""
    image = qrcode.make(str(payment_url))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    buffer.name = "alipay-cashier-qr.png"
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
    """Send ordinary non-delivery notices as plain text."""
    if not str(user_id).isdigit():
        return False
    try:
        bot.send_message(int(user_id), str(text or ""), parse_mode=None)
        return True
    except Exception:
        return False


def _delivery_identity(bot, order):
    import html
    uid = str(order.get("user_id") or "")
    nickname = str(order.get("nickname") or order.get("first_name") or "用户").strip()
    username = str(order.get("username") or "").strip().lstrip("@")
    try:
        chat = bot.get_chat(int(uid))
        full_name = " ".join(x for x in (
            str(getattr(chat, "first_name", "") or "").strip(),
            str(getattr(chat, "last_name", "") or "").strip(),
        ) if x).strip()
        nickname = full_name or nickname
        username = str(getattr(chat, "username", "") or username).strip().lstrip("@")
    except Exception:
        pass
    return html.escape(nickname or "用户"), html.escape(username or "无"), uid


def _delivery_time(order):
    raw = order.get("delivered_at") or order.get("paid_at") or order.get("created_at")
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else datetime.now().astimezone()
        if value.tzinfo is None:
            value = value.astimezone()
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _split_delivery_content(content, limit=2800):
    content = str(content or "")
    chunks = []
    while content:
        if len(content) <= limit:
            chunks.append(content)
            break
        cut = content.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(content[:cut])
        content = content[cut:].lstrip("\n")
    return chunks or [""]


def _notify_delivery(bot, order):
    """Send the customer delivery card in HTML, with credentials in a non-link PRE block."""
    import html
    import re
    nickname, username, uid = _delivery_identity(bot, order)
    if not uid.isdigit():
        return False
    product_name = str(order.get("product_name") or "商品")
    product_name = re.sub(r"(?i)\bGraph\s*API\b", "Graph 数据功能", product_name)
    product_name = re.sub(r"(?i)(微软邮箱验证)\s*\+\s*api\b", r"\1+自动数据接入", product_name)
    product_name = re.sub(r"(?i)\bapi\b", "数据接入功能", product_name)
    product = html.escape(product_name)
    order_id = html.escape(str(order.get("order_id") or "—"))
    when = html.escape(_delivery_time(order))
    chunks = _split_delivery_content(order.get("delivery_content"))
    try:
        for index, chunk in enumerate(chunks):
            prefix = (
                "🎉 <b>订单发货成功</b>\n\n"
                f"📦 <b>商品名称：</b>{product}\n"
                f"🔢 <b>本店订单：</b><code>{order_id}</code>\n"
                f"🕒 <b>发货时间：</b>{when}\n\n"
                "🔑 <b>您的专属账号数据（请点击下方内容复制）：</b>\n"
            ) if index == 0 else "🔑 <b>账号数据（续）：</b>\n"
            suffix = (
                "\n\n⚠️ <b>使用说明：</b>\n"
                "1. 请复制上方卡密内容，使用 Telegram 客户端导入。\n"
                "2. 若需要验证码，请直接点击获取。\n"
                "3. 请妥善保管您的账号信息，如有问题请及时联系客服（<code>@qingtian_Boss</code>）。"
            ) if index == len(chunks) - 1 else ""
            message = prefix + "<pre>" + html.escape(chunk, quote=False) + "</pre>" + suffix
            bot.send_message(int(uid), message, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        return False


def _notify_archive_delivery(bot, order):
    """Publish the store-facing order/delivery record to the private archive channel."""
    import html
    chat_id = str(os.getenv("TELEGRAM_ARCHIVE_CHAT_ID", "-1004459109788")).strip()
    nickname, username, uid = _delivery_identity(bot, order)
    if not chat_id.lstrip("-").isdigit() or not uid.isdigit():
        return False
    product = html.escape(str(order.get("product_name") or "商品"))
    order_id = html.escape(str(order.get("order_id") or "—"))
    amount_value = order.get("amount_cny")
    if amount_value in (None, ""):
        raw_amount = order.get("amount_usdt") or order.get("amount") or "0"
        try:
            amount_value = format(price_cny(Decimal(str(raw_amount))), ".2f")
        except Exception:
            amount_value = str(raw_amount)
    amount = html.escape(str(amount_value))
    username_label = f"@{username}" if username != "无" else "无"
    when = html.escape(_delivery_time(order))
    content_chunks = _split_delivery_content(order.get("delivery_content"))
    try:
        for index, chunk in enumerate(content_chunks):
            header = (
                "💰 <b>新订单支付成功通知</b>\n\n"
                f"👤 <b>购买用户：</b><a href=\"tg://user?id={uid}\">{nickname}</a>\n"
                f"🔗 <b>用户名：</b>{username_label}\n"
                f"📦 <b>商品名称：</b>{product}\n"
                f"💵 <b>实付金额：</b>¥{amount}\n"
                f"🔢 <b>本店订单：</b><code>{order_id}</code>\n"
                f"🕒 <b>时间：</b>{when}\n\n"
                "🔑 <b>交付内容：</b>\n"
            ) if index == 0 else "🔑 <b>交付内容（续）：</b>\n"
            bot.send_message(int(chat_id), header + "<pre>" + html.escape(chunk, quote=False) + "</pre>",
                             parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as exc:
        print("订单归档频道推送失败：", type(exc).__name__, flush=True)
        return False


def _notify_usdt_receipt(bot, target, transfer):
    """Send the structured, HTML-escaped receipt notice for a matched TRC20 order."""
    import html
    uid = str(target.get("user_id") or "").strip()
    if not uid.isdigit():
        return False
    nickname = str(target.get("nickname") or target.get("name") or target.get("username") or "用户")
    try:
        chat = bot.get_chat(int(uid))
        first = str(getattr(chat, "first_name", "") or "").strip()
        last = str(getattr(chat, "last_name", "") or "").strip()
        resolved = (first + " " + last).strip() or str(getattr(chat, "username", "") or "").strip()
        if resolved:
            nickname = resolved
    except Exception:
        pass
    raw = str(transfer.get("amount_raw") or "")
    try:
        amount_value = Decimal(raw) / Decimal("1000000") if raw.isdigit() else Decimal(str(target.get("amount_usdt") or target.get("amount") or "0"))
        amount = format(amount_value.normalize(), "f")
        if not amount:
            amount = str(target.get("amount_usdt") or target.get("amount") or "—")
    except Exception:
        amount = str(target.get("amount_usdt") or target.get("amount") or "—")
    block_ms = transfer.get("block_timestamp_ms")
    try:
        current_time = datetime.fromtimestamp(int(block_ms) / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S") if block_ms else datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        current_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    tx_hash = str(transfer.get("txid") or "—")
    from_address = str(transfer.get("from") or "—")
    message = (
        "✅ <b>USDT 充值到账通知</b>\n\n"
        f"👤 <b>充值用户：</b><a href=\"tg://user?id={uid}\">{html.escape(nickname)}</a>\n"
        f"💰 <b>到账金额：</b>{html.escape(amount)} USDT\n"
        f"🕒 <b>到账时间：</b>{html.escape(current_time)}\n"
        f"🔗 <b>交易哈希：</b><code>{html.escape(tx_hash)}</code>\n"
        f"📥 <b>发送地址：</b><code>{html.escape(from_address)}</code>\n\n"
        "<i>系统已自动确认，当前正在为您处理入账。</i>"
    )
    try:
        bot.send_message(int(uid), message, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        return False


def _notify_admin(bot, admin_id, text):
    try:
        if str(admin_id).isdigit():
            bot.send_message(int(admin_id), text)
    except Exception:
        pass


def _notify_archive(bot, text):
    """Copy a delivered real supplier item to the configured private archive channel."""
    chat_id = str(os.getenv("TELEGRAM_ARCHIVE_CHAT_ID", "-1004459109788")).strip()
    try:
        if not chat_id.lstrip("-").isdigit():
            return False
        bot.send_message(int(chat_id), text, disable_web_page_preview=True)
        return True
    except Exception as exc:
        print("交付内容归档频道推送失败：", type(exc).__name__, flush=True)
        return False


@contextmanager
def _payment_confirmation_process_lock():
    """Claim payment settlement once across threads and multiple bot processes."""
    lock_dir = "/root/fakabot/data"
    os.makedirs(lock_dir, mode=0o700, exist_ok=True)
    lock_path = os.path.join(lock_dir, "payment_confirmation.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def handle_confirmed_payment(
    bot, order_id, transfer, load_orders, save_orders, admin_id="",
    supplier_client_factory=None, trusted_external_gateway=False, settle_topup=None,
    on_order_paid=None,
):
    """Mark an exact on-chain payment once; optionally procure once, never retry blindly."""
    alipay_sandbox_config = ("sandbox" in os.getenv("ALIPAY_GATEWAY", "").strip().lower()
                             and not trusted_external_gateway)
    sandbox_gateway = False
    procurement_enabled = os.getenv("SUPPLIER_AUTOPROCUREMENT_ENABLED", "0") == "1"
    # Never spend real supplier balance for a sandbox Alipay payment.
    auto_procurement = procurement_enabled and not sandbox_gateway
    with _lock, _payment_confirmation_process_lock():
        orders = load_orders()
        target = next(
            (x for x in orders if isinstance(x, dict) and str(x.get("order_id")) == str(order_id)),
            None,
        )
        if not target or target.get("payment_status") in ("paid", "completed", "delivered"):
            return False
        # The Alipay sandbox switch must never suppress independently verified TRON USDT payments.
        sandbox_gateway = alipay_sandbox_config and target.get("payment_method") in ("alipay_qr", "alipay_gateway", "alipay_mock")
        target.update({
            "payment_status": "paid",
            "status": "paid",
            "paid_at": datetime.now(timezone.utc).isoformat(),
            "txid": str(transfer.get("txid") or ""),
            "procurement_status": ("blocked_sandbox_gateway" if sandbox_gateway else
                                   "procurement_processing" if auto_procurement else
                                   "blocked_supplier_session"),
        })
        target["payment_transfer"] = {
            k: transfer.get(k)
            for k in ("from", "to", "amount_raw", "block_timestamp_ms", "contract")
        }
        target["payment_transfer"]["provider"] = transfer.get("provider") or target.get("payment_method") or "trc20_usdt"
        target["payment_transfer"]["manual_verification"] = transfer.get("manual_verification") or ""
        if target.get("payment_method") == "usdt_wallet_topup":
            if not callable(settle_topup):
                raise RuntimeError("USDT 余额充值结算处理器未配置，拒绝确认订单")
            target["wallet_topup"] = True
            settle_topup(target, orders, transfer)
        else:
            save_orders(orders)
        target = dict(target)
        if target.get("payment_method") != "usdt_wallet_topup" and callable(on_order_paid):
            try:
                on_order_paid(dict(target))
            except Exception as counter_exc:
                # Counter repair must never interrupt an already-confirmed payment or delivery.
                print("⚠️ 已付款订单累计计数同步失败：", type(counter_exc).__name__, flush=True)

    uid = str(target.get("user_id") or "")
    if target.get("payment_method") == "usdt_trc20":
        _notify_usdt_receipt(bot, target, transfer)
    if target.get("payment_method") == "usdt_wallet_topup":
        return True
    # Sandbox-only delivery stub; production gateways and non-Alipay orders are excluded.
    upstream_mock = os.getenv("UPSTREAM_MOCK_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
    alipay_order = target.get("payment_method") in ("alipay_qr", "alipay_gateway")
    explicit_test_order = (target.get("sandbox_test_order") is True
                           and target.get("upstream_mock_mode") is True
                           and target.get("payment_verification") == "stateless_gateway_test_only")
    if (upstream_mock and sandbox_gateway and alipay_order) or explicit_test_order:
        test_code = os.getenv("UPSTREAM_MOCK_DELIVERY_CODE", "TEST-CODE-123456").strip() or "TEST-CODE-123456"
        print(f"【测试模式已开启】上游采购拦截：订单 {order_id}；未调用真实供应商接口", flush=True)
        delivered = _save_order_updates(order_id, {
            "delivery_content": test_code, "procurement_status": "delivered",
            "status": "delivered", "payment_status": "paid", "upstream_mock_mode": True,
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        }, load_orders, save_orders)
        if not delivered:
            print(f"【测试模式】测试卡密写入订单失败：{order_id}", flush=True)
            return False
        print(f"【测试模式】测试卡密已写入订单：{order_id}", flush=True)
        sent = _notify(bot, uid, "✅ 付款状态已确认\n" +
                       f"订单：{order_id}\n\n📦 测试卡密：\n" + test_code)
        _save_order_updates(order_id, {
            "delivery_notification_status": "sent" if sent else "pending",
            "delivery_notification_attempted_at": datetime.now(timezone.utc).isoformat(),
        }, load_orders, save_orders)
        print(f"【测试模式】测试卡密 Telegram 发货{'成功' if sent else '失败'}：{order_id}", flush=True)
        if not sent:
            _notify_admin(bot, admin_id, f"⚠️ 沙箱测试卡密发货通知失败：{order_id}")
        return True

    if not auto_procurement or target.get("supplier_verified") is not True or target.get("procurement_ready") is not True:
        reason = (
            "当前订单暂无法自动发货，请联系客服核对" if sandbox_gateway else
            "自动发货服务暂未启用" if not auto_procurement else
            "该订单缺少付款前的商品核验记录"
        )
        amount_text = (
            f"金额：¥{target.get('amount_cny')} CNY\n"
            if target.get("payment_method") in ("alipay_qr", "alipay_gateway")
            else f"金额：{float(target.get('amount') or 0):.6f} USDT\n"
        )
        text = (
            "✅ 付款已确认\n"
            f"订单：{target.get('order_id')}\n"
            + amount_text
            + f"支付参考：{target.get('txid') or '—'}\n\n"
            + f"{reason}，商品暂未发货；请勿重复付款。"
        )
        if target.get("payment_method") != "usdt_trc20":
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
        sent = _notify_delivery(bot, delivered)
        archived = _notify_archive_delivery(bot, delivered)
        _save_order_updates(
            order_id,
            {"delivery_notification_status": "sent" if sent else "pending",
             "archive_delivery_notification_status": "sent" if archived else "pending",
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
            "订单状态正在核对，暂未确认交付内容。客服会继续处理，请勿重复付款。",
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
        sent = _notify_delivery(bot, row)
        if sent:
            _save_order_updates(order_id, {
                "delivery_notification_status": "sent",
                "delivery_notification_attempted_at": datetime.now(timezone.utc).isoformat(),
            }, load_orders, save_orders)
    archive_pending = [
        dict(row) for row in orders
        if isinstance(row, dict)
        and row.get("payment_status") == "paid"
        and row.get("procurement_status") == "delivered"
        and row.get("archive_delivery_notification_status") == "pending"
        and str(row.get("delivery_content") or "").strip()
    ]
    for row in archive_pending:
        order_id = str(row.get("order_id") or "")
        archived = _notify_archive_delivery(bot, row)
        if archived:
            _save_order_updates(order_id, {
                "archive_delivery_notification_status": "sent",
                "archive_delivery_notification_attempted_at": datetime.now(timezone.utc).isoformat(),
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
            sent = _notify_delivery(bot, delivered)
            archived = _notify_archive_delivery(bot, delivered)
            _save_order_updates(order_id, {
                "delivery_notification_status": "sent" if sent else "pending",
                "archive_delivery_notification_status": "sent" if archived else "pending",
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

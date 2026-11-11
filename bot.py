# -*- coding: utf-8 -*-
"""FakaBot: Telegram storefront + confirmed TRC20 USDT payment + auto delivery.

Required environment:
  BOT_TOKEN, ADMIN_ID, TRON_RECEIVE_ADDRESS, TRONGRID_API_KEY

Optional:
  PRODUCT_PRICE_USDT=10
  PAYMENT_TTL_MINUTES=15
  TRONGRID_BASE_URL=https://api.trongrid.io
  TRON_USDT_CONTRACT=TR7NHqjeKQxGTCi8q8ZY4p4L8otSzgjLj6t
  PAYMENT_POLL_SECONDS=8

No wallet private key is used or needed: this service only verifies deposits.
"""

import os
import json
import html
import time
import uuid
import random
import logging
import threading
import tempfile
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from filelock import FileLock

from payment_listener import PaymentWatcher, USDT_DECIMALS

TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
PRODUCT_PRICE = Decimal(os.getenv("PRODUCT_PRICE_USDT", "10"))
PAYMENT_TTL_MINUTES = int(os.getenv("PAYMENT_TTL_MINUTES", "15"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STOCK_FILE = os.path.join(BASE_DIR, "stock.txt")
LOCK_FILE = os.path.join(BASE_DIR, ".fakabot-data.lock")

CHANNEL_URL = "https://t.me/qingtian_vip"
SUPPORT_URL = "https://t.me/qingtian_Boss"
RECEIVE_ADDRESS = os.getenv("TRON_RECEIVE_ADDRESS", "")

DEFAULT_CONFIG = {
    "welcome_tip": "点击下方按钮即可挑选心仪商品，系统全自动发货。",
    "buy_button_text": "🛒 立即购买"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8")
    ]
)
logger = logging.getLogger("fakabot")
DATA_LOCK = threading.RLock()
FILE_LOCK = FileLock(LOCK_FILE, timeout=20)
ADMIN_EDIT_STATE = {}

if not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
    raise RuntimeError("请设置 BOT_TOKEN。")
if ADMIN_ID <= 0 or not RECEIVE_ADDRESS:
    raise RuntimeError("请正确设置 ADMIN_ID 和 TRON_RECEIVE_ADDRESS。")
if PRODUCT_PRICE <= 0 or PAYMENT_TTL_MINUTES < 1:
    raise RuntimeError("商品价格和支付有效期配置无效。")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True, num_threads=8)


# -------------------- durable storage --------------------

def atomic_write_text(path, content):
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_json(path, expected, default):
    if not os.path.exists(path):
        return default.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, expected):
            raise ValueError("JSON root type mismatch")
        return value
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        backup = path + ".corrupt"
        if not os.path.exists(backup):
            try:
                os.replace(path, backup)
            except OSError:
                logger.exception("Failed to back up corrupt JSON: %s", path)
        raise RuntimeError(f"{path} 数据损坏，已尝试备份；请检查备份文件后再启动。")


def ensure_files():
    with FILE_LOCK:
        for path, value in (
            (USERS_FILE, {}),
            (ORDERS_FILE, []),
            (CONFIG_FILE, DEFAULT_CONFIG),
        ):
            if not os.path.exists(path):
                atomic_write_json(path, value)
        if not os.path.exists(STOCK_FILE):
            atomic_write_text(STOCK_FILE, "")


def read_json(path, expected, default):
    with FILE_LOCK:
        return load_json(path, expected, default)


def read_stock():
    with open(STOCK_FILE, "r", encoding="utf-8") as f:
        return [line.rstrip("\r\n") for line in f if line.strip()]


def write_stock(items):
    if any("\n" in x or "\r" in x for x in items):
        raise ValueError("每条卡密必须独占一行")
    atomic_write_text(STOCK_FILE, "".join(x + "\n" for x in items))


def esc(value):
    return html.escape(str(value), quote=True)


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat(timespec="seconds")


def is_admin(uid):
    return int(uid) == ADMIN_ID


def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.exception("Telegram send_message failed; chat_id=%s", chat_id)
        return None


# -------------------- users / UI --------------------

def get_user(user):
    uid = str(user.id)
    with FILE_LOCK:
        users = load_json(USERS_FILE, dict, {})
        record = users.get(uid) or {
            "user_id": user.id,
            "balance": "0.00",
            "first_seen": iso_now()
        }
        record["first_name"] = user.first_name or ""
        record["username"] = user.username or ""
        record.setdefault("first_seen", iso_now())
        record.setdefault("balance", "0.00")
        users[uid] = record
        atomic_write_json(USERS_FILE, users)
        return record


def companion_days(first_seen):
    try:
        dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (utc_now() - dt.astimezone(timezone.utc)).days)
    except Exception:
        return 0


def get_config():
    config = read_json(CONFIG_FILE, dict, DEFAULT_CONFIG.copy())
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)
    return config


def main_keyboard(config):
    kb = types.InlineKeyboardMarkup(row_width=2)
    label = str(config.get("buy_button_text") or DEFAULT_CONFIG["buy_button_text"])[:64]
    kb.row(
        types.InlineKeyboardButton(label, callback_data="buy"),
        types.InlineKeyboardButton("👤 历史订单", callback_data="orders")
    )
    kb.row(
        types.InlineKeyboardButton("📢 官方频道", url=CHANNEL_URL),
        types.InlineKeyboardButton("💬 联系客服", url=SUPPORT_URL)
    )
    return kb


def welcome_text(user, record, config):
    try:
        balance = Decimal(str(record.get("balance", "0"))).quantize(Decimal("0.01"))
    except InvalidOperation:
        balance = Decimal("0.00")
    tip = esc(config.get("welcome_tip", DEFAULT_CONFIG["welcome_tip"]))
    return (
        f"✨ <b>尊敬的 {esc(user.first_name or '用户')}，欢迎光临！</b>\n\n"
        "📌 <b>您的专属账户信息</b>\n"
        f" ├ 🆔 <b>用户 ID：</b> <code>{user.id}</code>\n"
        f" ├ 💰 <b>账户余额：</b> <b>{balance}</b> USDT\n"
        f" └ ⏳ <b>相伴天数：</b> 已陪伴您 <b>{companion_days(record['first_seen'])}</b> 天\n\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🛒 <b>自助选购</b>：{tip}\n"
        "🛡 <b>安全提示</b>：请妥善保管您的账号密码，虚拟商品售出概不退换。\n\n"
        f"📢 <b>内置节点频道</b>：<a href=\"{CHANNEL_URL}\">点击加入</a>\n"
        f"💬 <b>专属客服</b>：<a href=\"{SUPPORT_URL}\">@qingtian_Boss</a>\n\n"
        '<tg-emoji emoji-id="5368324170671202286">✨</tg-emoji>'
    )


@bot.message_handler(commands=["start"])
def start(message):
    try:
        record = get_user(message.from_user)
        bot.send_message(
            message.chat.id,
            welcome_text(message.from_user, record, get_config()),
            reply_markup=main_keyboard(get_config()),
            disable_web_page_preview=True
        )
    except Exception:
        logger.exception("/start failed")
        safe_send(message.chat.id, "系统暂时繁忙，请稍后重试。")


# -------------------- payment order creation --------------------

def amount_to_raw(amount):
    scaled = amount * (Decimal(10) ** USDT_DECIMALS)
    if scaled != scaled.to_integral_value():
        raise ValueError("支付金额超过 USDT 支持的小数精度")
    return int(scaled)


def raw_to_amount(raw):
    return (Decimal(int(raw)) / (Decimal(10) ** USDT_DECIMALS)).quantize(
        Decimal("0.000001")
    )


def create_payment_order(user):
    uid = str(user.id)
    with FILE_LOCK:
        orders = load_json(ORDERS_FILE, list, [])
        for old in orders:
            if old.get("user_id") == user.id and old.get("payment_status") == "pending":
                if utc_now().timestamp() * 1000 < int(old.get("expires_at_ms", 0)):
                    return None, old

        # Use a random 1..999999 micro-USDT suffix to distinguish concurrent payments.
        used = {
            int(o["amount_raw"]) for o in orders
            if o.get("payment_status") == "pending"
            and int(o.get("expires_at_ms", 0)) > int(utc_now().timestamp() * 1000)
        }
        base_raw = amount_to_raw(PRODUCT_PRICE)
        for _ in range(10000):
            suffix = random.randint(1, 999999)
            amount_raw = base_raw + suffix
            if amount_raw not in used:
                break
        else:
            raise RuntimeError("无法生成唯一支付金额，请稍后重试")

        created = utc_now()
        expires = created + timedelta(minutes=PAYMENT_TTL_MINUTES)
        order = {
            "order_id": uuid.uuid4().hex[:16].upper(),
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "created_at": created.isoformat(timespec="seconds"),
            "created_at_ms": int(created.timestamp() * 1000),
            "expires_at": expires.isoformat(timespec="seconds"),
            "expires_at_ms": int(expires.timestamp() * 1000),
            "product": "自动发卡商品",
            "price_base": str(PRODUCT_PRICE),
            "amount_raw": str(amount_raw),
            "amount": str(raw_to_amount(amount_raw)),
            "payment_status": "pending",
            "status": "pending_payment",
            "txid": None,
            "delivery_status": "not_started"
        }
        orders.append(order)
        atomic_write_json(ORDERS_FILE, orders)
        return order, None


def payment_message(order):
    return (
        "💳 <b>USDT 订单已创建</b>\n\n"
        f"🧾 订单号：<code>{esc(order['order_id'])}</code>\n"
        f"💰 请准确转账：<b>{esc(order['amount'])} USDT</b>\n"
        "🌐 网络：<b>TRON / TRC20</b>\n"
        f"🏦 收款地址：\n<code>{esc(RECEIVE_ADDRESS)}</code>\n\n"
        f"⏳ 有效期：<b>{PAYMENT_TTL_MINUTES} 分钟</b>\n"
        f"⌛ 截止时间（UTC）：<code>{esc(order['expires_at'])}</code>\n\n"
        "⚠️ 请使用 TRC20 网络，并转入上方显示的精确金额。"
        "少转、多转、使用其他网络或超时到账都不会自动发货。\n"
        "链上确认后机器人会自动发送卡密。"
    )


@bot.callback_query_handler(func=lambda c: c.data == "buy")
def buy_callback(call):
    try:
        bot.answer_callback_query(call.id)
        get_user(call.from_user)
        order, existing = create_payment_order(call.from_user)
        if existing:
            bot.send_message(
                call.message.chat.id,
                "⚠️ <b>你已有待支付订单。</b>\n\n" + payment_message(existing)
            )
            return
        bot.send_message(call.message.chat.id, payment_message(order))
    except Exception:
        logger.exception("创建支付订单失败")
        safe_send(call.message.chat.id, "创建订单失败，请稍后重试或联系客服。")


# -------------------- chain payment and delivery --------------------

def update_order(order_id, mutator):
    with FILE_LOCK:
        orders = load_json(ORDERS_FILE, list, [])
        for order in orders:
            if order.get("order_id") == order_id:
                mutator(order)
                atomic_write_json(ORDERS_FILE, orders)
                return dict(order)
    return None


def on_payment_confirmed(order_id, transfer):
    """Called only for a confirmed, exact-amount, in-window transfer."""
    def mark_paid(order):
        if order.get("payment_status") != "pending":
            return
        order["payment_status"] = "paid"
        order["status"] = "paid"
        order["paid_at"] = iso_now()
        order["txid"] = transfer["txid"]
        order["chain_transfer"] = transfer

    order = update_order(order_id, mark_paid)
    if not order or order.get("payment_status") != "paid":
        return
    logger.info("Payment confirmed: order=%s txid=%s", order_id, transfer["txid"])
    deliver_paid_order(order_id)


def deliver_paid_order(order_id):
    """At-most-once delivery attempt: persist 'sending' before Telegram send."""
    with FILE_LOCK:
        orders = load_json(ORDERS_FILE, list, [])
        order = next((o for o in orders if o.get("order_id") == order_id), None)
        if not order or order.get("payment_status") != "paid":
            return
        if order.get("delivery_status") != "not_started":
            return

        stock = read_stock()
        if not stock:
            order["delivery_status"] = "blocked_no_stock"
            order["delivery_error"] = "库存为空"
            atomic_write_json(ORDERS_FILE, orders)
            safe_send(ADMIN_ID, f"🚨 订单 {esc(order_id)} 已付款，但库存为空，请人工处理。")
            return

        card = stock[0]
        order["delivery_status"] = "sending"
        order["delivery_started_at"] = iso_now()
        order["product"] = card
        write_stock(stock[1:])
        atomic_write_json(ORDERS_FILE, orders)
        target_user_id = int(order["user_id"])

    # Do not automatically resend after an ambiguous Telegram timeout:
    # this avoids duplicate card delivery. Admin must reconcile 'sending' orders.
    sent = safe_send(
        target_user_id,
        "🎉 <b>支付确认成功，自动发货完成！</b>\n\n"
        f"🧾 订单号：<code>{esc(order_id)}</code>\n"
        f"💰 实付：<b>{esc(order['amount'])} USDT</b>\n"
        f"📦 卡密：\n<code>{esc(card)}</code>\n\n请妥善保存。"
    )

    if sent:
        def mark_delivered(o):
            o["delivery_status"] = "delivered"
            o["status"] = "delivered"
            o["delivered_at"] = iso_now()
        update_order(order_id, mark_delivered)
        logger.info("Delivered order %s", order_id)
    else:
        logger.error("Delivery uncertain for order %s; manual reconciliation required", order_id)
        safe_send(
            ADMIN_ID,
            f"⚠️ 订单 {esc(order_id)} 已扣库存，Telegram 发送结果不确定。"
            "为防重复发卡，系统未自动重发，请人工核查用户消息和库存。"
        )


def on_late_payment(order_id, transfer):
    def mark_late(order):
        if order.get("payment_status") == "expired":
            order["payment_status"] = "paid_late"
            order["status"] = "paid_late_review"
            order["late_paid_at"] = iso_now()
            order["txid"] = transfer["txid"]
            order["chain_transfer"] = transfer
    order = update_order(order_id, mark_late)
    if order and order.get("payment_status") == "paid_late":
        safe_send(
            ADMIN_ID,
            f"⚠️ <b>检测到超时到账</b>\n订单：<code>{esc(order_id)}</code>\n"
            f"交易：<code>{esc(transfer['txid'])}</code>\n请人工退款或处理，不会自动发货。"
        )


def on_order_expired(order_id):
    with FILE_LOCK:
        orders = load_json(ORDERS_FILE, list, [])
        order = next((o for o in orders if o.get("order_id") == order_id), None)
    if order:
        safe_send(int(order["user_id"]), f"⌛ 订单 <code>{esc(order_id)}</code> 已过期，请重新下单。")


# -------------------- orders --------------------

def user_orders_text(user_id):
    orders = read_json(ORDERS_FILE, list, [])
    mine = [o for o in orders if int(o.get("user_id", 0)) == int(user_id)]
    mine.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    if not mine:
        return "📭 暂无订单。"
    rows = ["📜 <b>最近 5 笔订单</b>\n"]
    for o in mine[:5]:
        rows.append(
            f"🧾 <code>{esc(o.get('order_id'))}</code>\n"
            f"💰 {esc(o.get('amount', '-'))} USDT\n"
            f"📌 状态：<b>{esc(o.get('status', '-'))}</b>\n"
            f"🕒 <code>{esc(o.get('created_at', '-'))}</code>\n"
        )
    return "\n".join(rows)


@bot.callback_query_handler(func=lambda c: c.data == "orders")
def orders_callback(call):
    try:
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, user_orders_text(call.from_user.id))
    except Exception:
        logger.exception("历史订单查询失败")
        safe_send(call.message.chat.id, "读取订单失败，请稍后重试。")


@bot.message_handler(commands=["orders"])
def orders_command(message):
    safe_send(message.chat.id, user_orders_text(message.from_user.id))


# -------------------- admin --------------------

def admin_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("✏️ 修改自定义提示语", callback_data="admin_tip"),
        types.InlineKeyboardButton("🔘 修改购买按钮文字", callback_data="admin_button"),
        types.InlineKeyboardButton("📦 查看库存", callback_data="admin_stock")
    )
    return kb


@bot.message_handler(commands=["admin"])
def admin_command(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔ 无权操作。")
        return
    try:
        stock_count = len(read_stock())
        safe_send(
            message.chat.id,
            f"🛠 <b>管理员面板</b>\n库存：<b>{stock_count}</b> 条",
            reply_markup=admin_keyboard()
        )
    except Exception:
        logger.exception("管理员面板加载失败")


@bot.callback_query_handler(func=lambda c: c.data in ("admin_tip", "admin_button", "admin_stock"))
def admin_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "无权操作", show_alert=True)
        return
    if call.data == "admin_stock":
        bot.answer_callback_query(call.id)
        safe_send(call.message.chat.id, f"📦 当前库存：<b>{len(read_stock())}</b> 条")
        return
    ADMIN_EDIT_STATE[call.from_user.id] = (
        "welcome_tip" if call.data == "admin_tip" else "buy_button_text"
    )
    bot.answer_callback_query(call.id)
    safe_send(call.message.chat.id, "请发送新内容，发送 /cancel 取消。")


@bot.message_handler(commands=["cancel"])
def cancel_command(message):
    if is_admin(message.from_user.id):
        ADMIN_EDIT_STATE.pop(message.from_user.id, None)
        safe_send(message.chat.id, "已取消。")


@bot.message_handler(commands=["addstock"])
def addstock_command(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔ 无权操作。")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        safe_send(message.chat.id, "用法：<code>/addstock 卡密内容</code>")
        return
    with FILE_LOCK:
        stock = read_stock()
        stock.append(parts[1].strip())
        write_stock(stock)
    safe_send(message.chat.id, f"✅ 补货成功，库存 {len(stock)} 条。")


@bot.message_handler(
    func=lambda m: m.from_user is not None
    and is_admin(m.from_user.id)
    and m.from_user.id in ADMIN_EDIT_STATE,
    content_types=["text"]
)
def admin_config_input(message):
    field = ADMIN_EDIT_STATE.get(message.from_user.id)
    value = (message.text or "").strip()
    if not value or len(value) > 500 or (field == "buy_button_text" and len(value) > 64):
        safe_send(message.chat.id, "内容为空或长度超限，请重新发送。")
        return
    with FILE_LOCK:
        config = load_json(CONFIG_FILE, dict, DEFAULT_CONFIG.copy())
        config[field] = value
        atomic_write_json(CONFIG_FILE, config)
    ADMIN_EDIT_STATE.pop(message.from_user.id, None)
    safe_send(message.chat.id, "✅ 配置已保存，立即生效。")


# -------------------- startup --------------------

def main():
    ensure_files()
    watcher = PaymentWatcher(
        orders_file=ORDERS_FILE,
        lock_file=LOCK_FILE,
        on_payment=on_payment_confirmed,
        on_late_payment=on_late_payment,
        on_expired=on_order_expired,
    )
    watcher.start()
    logger.info("FakaBot started; TRC20 receiving address=%s", RECEIVE_ADDRESS)

    try:
        bot.infinity_polling(
            timeout=30,
            long_polling_timeout=30,
            skip_pending=False
        )
    except KeyboardInterrupt:
        logger.info("Stopping FakaBot...")
    except Exception:
        logger.exception("Polling stopped unexpectedly")
        raise
    finally:
        watcher.stop()


if __name__ == "__main__":
    main()

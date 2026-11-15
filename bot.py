
# -*- coding: utf-8 -*-
import telebot, time, json, os, re, html, io, hashlib, zipfile, copy, unicodedata, inspect, threading, fcntl, sys
from datetime import datetime, timedelta
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from telebot import types
from products.catalog import (
    APPLE_PRODUCTS, TG_PRODUCTS, INSTAGRAM_PRODUCTS, AI_PRODUCTS, SALES_POLICY,
    PRODUCT_CATALOGS, UPSTREAM_CATEGORIES, UPSTREAM_PRODUCT_CATALOGS,
    UPSTREAM_CATEGORY_BY_KEY, get_all_products,
    refresh_supplier_catalog, start_supplier_catalog_refresh,
)
from payment_flow import PaymentConfigurationError, PaymentWatcher, create_payment_order, create_alipay_order, create_custom_topup_order, price_cny, create_payment_qr, create_alipay_payment_qr, handle_confirmed_payment, reconcile_pending_supplier_deliveries
from alipay_gateway import build_checkout_url
from stateless_payment_gateway import (
    create_order as create_gateway_order, start_webhook_server, gateway_order_worker_loop,
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
# 兼容旧版管理员配置：部署环境可提供 ADMIN_IDS。
# Existing notification/payment callbacks accept one primary admin ID.
ADMIN_ID = (os.getenv("ADMIN_ID", "").strip()
            or os.getenv("ADMIN_IDS", "").split(",")[0].strip()
            or "7867116527")
if not TOKEN:
    raise RuntimeError("缺少环境变量 BOT_TOKEN，请在服务器环境中配置机器人 Token。")
# Keep the default worker pool small for low-memory VPS instances.
_worker_threads = max(2, min(8, int(os.getenv("BOT_WORKER_THREADS", "4"))))
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=_worker_threads)

# Critical production alerts are rate-limited and sent asynchronously so an
# unhealthy Telegram/archive dependency cannot deadlock business callbacks.
_CRITICAL_ALERT_ADMIN_ID = os.getenv("CRITICAL_ALERT_ADMIN_ID", "7867116527").strip()
_CRITICAL_ALERT_LOCK = threading.Lock()
_CRITICAL_ALERT_LAST = {}
_ACTION_DEBOUNCE_LOCK = threading.Lock()
_ACTION_DEBOUNCE_LAST = {}

def _is_expected_business_rejection(exc):
    """Return True for normal payment/order validation outcomes, not outages."""
    parts = [type(exc).__name__, str(exc or "")]
    response = getattr(exc, "response", None)
    if response is not None:
        parts.append(str(getattr(response, "text", "") or ""))
        parts.append(str(getattr(response, "status_code", "") or ""))
    message = " ".join(parts).casefold()
    expected_markers = (
        "order_not_found", "business_order_not_found", "订单不存在",
        "余额不足", "insufficient balance", "商品已售罄", "商品已缺货",
        "out of stock", "sold out", "already paid", "订单已支付",
        "duplicate notification", "重复回调", "invalid order state",
    )
    return any(marker in message for marker in expected_markers)


def _notify_admin_critical(category, exc, context=""):
    if _is_expected_business_rejection(exc):
        print("BUSINESS_REJECTION_IGNORED", str(category), type(exc).__name__, flush=True)
        return False
    admin_id = str(_CRITICAL_ALERT_ADMIN_ID or "").strip()
    if not admin_id.isdigit():
        print("CRITICAL_ALERT_ADMIN_NOT_CONFIGURED", category, flush=True)
        return False
    now = time.monotonic()
    with _CRITICAL_ALERT_LOCK:
        last = _CRITICAL_ALERT_LAST.get(str(category), 0.0)
        if now - last < 60:
            return False
        _CRITICAL_ALERT_LAST[str(category)] = now
    message = str(exc or "UnknownError")
    message = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)(token|secret|password|private[_ -]?key)([=: ]+)[^\s,;]+", r"\1\2[REDACTED]", message)
    text = ("🚨 系统严重异常告警\n"
            f"模块：{str(category)[:80]}\n"
            f"上下文：{str(context)[:160]}\n"
            f"异常：{type(exc).__name__}: {message[:500]}")
    def deliver():
        try:
            bot.send_message(int(admin_id), text, disable_web_page_preview=True)
        except Exception as send_exc:
            print("CRITICAL_ALERT_DELIVERY_FAILED", str(category), type(send_exc).__name__, flush=True)
    threading.Thread(target=deliver, name="critical-admin-alert", daemon=True).start()
    return True

def _action_debounce_allowed(user_id, action, interval=3.0):
    key = (str(user_id), str(action))
    now = time.monotonic()
    with _ACTION_DEBOUNCE_LOCK:
        previous = _ACTION_DEBOUNCE_LAST.get(key, 0.0)
        if now - previous < interval:
            return False
        _ACTION_DEBOUNCE_LAST[key] = now
        # Bound memory growth while preserving active users' recent debounce keys.
        if len(_ACTION_DEBOUNCE_LAST) > 5000:
            cutoff = now - 60.0
            for old_key, stamp in list(_ACTION_DEBOUNCE_LAST.items()):
                if stamp < cutoff:
                    _ACTION_DEBOUNCE_LAST.pop(old_key, None)
        return True

def _thread_exception_alert(args):
    try:
        _notify_admin_critical("后台线程", args.exc_value, args.thread.name if args.thread else "unknown")
    finally:
        threading.__excepthook__(args)

threading.excepthook = _thread_exception_alert

def _main_exception_alert(exc_type, exc_value, exc_traceback):
    _notify_admin_critical("主进程未捕获异常", exc_value, getattr(exc_type, "__name__", "Exception"))
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = _main_exception_alert
USERS_FILE = "/root/fakabot/users.json"  # logical key only; business data is stored in Telegram

EMOJI = {
    "usdt": "5001485391807645910", "tg": "5001558608115140737", "gear": "6244525025500601411",
    "gift": "6246956045709612135", "diamond": "6244585885187184999", "star": "6244603610517215237",
    "apple": "5001358570013329603", "ai": "6289722534019475465", "video": "5001404418789214347",
}

# Storefront categories deliberately use native Unicode emoji only.
# Do not attach unverified icon_custom_emoji_id values to category buttons.
STOREFRONT_CATEGORY_EMOJI_IDS = {}


def premium_emoji(key, fallback="⭐"):
    """Telegram Premium custom emoji HTML entity."""
    emoji_id = EMOJI.get(key)
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def clean_button_text(value):
    """按钮标题只保留文字；会员表情由 icon_custom_emoji_id 显示。"""
    value = str(value)
    value = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u20E3]+', '', value)
    return re.sub(r'\s+', ' ', value).strip()

def storefront_button(text, **kwargs):
    """Storefront buttons use Telegram's native custom-emoji icon field."""
    return premium_button(text, **kwargs)


def premium_button(text, **kwargs):
    """统一给内联按钮配置对应的 Telegram Premium 自定义表情图标。"""
    explicit_icon_key = kwargs.pop("emoji_key", None)
    label = clean_button_text(text)
    # Detect category icon from the original label before decorative emoji are stripped.
    probe = (str(text) + " " + str(kwargs.get("callback_data", ""))).lower()
    if explicit_icon_key in EMOJI:
        key = explicit_icon_key
    elif any(x in probe for x in ("充值", "支付", "usdt", "pay_")):
        key = "usdt"
    elif "上一页" in probe:
        key = "star"
    elif "下一页" in probe:
        key = "diamond"
    elif "🍎" in probe or any(x in probe for x in ("苹果", "apple", "list_apple")):
        key = "apple"
    elif "✈" in probe or any(x in probe for x in ("tg账号", "telegram", "list_tg")):
        key = "tg"
    elif "🤖" in probe or any(x in probe for x in ("ai", "claude", "chatgpt")):
        key = "ai"
    elif any(x in probe for x in ("影音", "视频", "游戏")):
        key = "video"
    elif "用户中心" in probe or "account center" in probe:
        # Account-center controls use a readable native emoji, never the dark custom gear.
        return types.InlineKeyboardButton(text=(str(text) if any(ch in str(text) for ch in "👤⚙") else "👤 " + label), **kwargs)
    elif "账单明细" in probe or "ledger" in probe:
        return types.InlineKeyboardButton(text=(str(text) if "🧾" in str(text) or "📊" in str(text) else "🧾 " + label), **kwargs)
    elif "我的订单" in probe or "my orders" in probe:
        return types.InlineKeyboardButton(text=(str(text) if "📦" in str(text) else "📦 " + label), **kwargs)
    elif any(x in probe for x in ("客服", "帮助", "指南", "⬅️ 返回", "更多")):
        key = "gear"
    elif any(x in probe for x in ("商城", "账号铺", "商品")):
        key = "diamond"
    elif any(x in probe for x in ("订单", "记录", "明细")):
        key = "star"
    else:
        key = "star"
    # Optional button icons must never prevent the main menu from rendering.
    try:
        return types.InlineKeyboardButton(
            text=label or "打开",
            icon_custom_emoji_id=EMOJI[key],
            **kwargs
        )
    except TypeError:
        return types.InlineKeyboardButton(text=label or "打开", **kwargs)


_EMOJI_CLUSTER_RE = re.compile(
    r'[\U0001F000-\U0001FAFF\u2600-\u27BF](?:\uFE0F|\uFE0E)?(?:\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF](?:\uFE0F|\uFE0E)?)*'
    r'(?:\uFE0F|\uFE0E|\u20E3|\U0001F3FB-\U0001F3FF)*'
)
def premiumize_text(value):
    """Convert every decorative Unicode emoji into custom entities without nesting existing entities."""
    value = str(value or "")
    protected = []
    def hold(match):
        protected.append(match.group(0))
        return f"__PREMIUM_ENTITY_{len(protected)-1}__"
    value = re.sub(r'<tg-emoji\b[^>]*>.*?</tg-emoji>', hold, value, flags=re.DOTALL)
    def replace(match):
        emoji = match.group(0)
        key = "gift" if "🎁" in emoji else "diamond"
        return f'<tg-emoji emoji-id="{EMOJI[key]}">{emoji}</tg-emoji>'
    value = _EMOJI_CLUSTER_RE.sub(replace, value)
    for index, entity in enumerate(protected):
        value = value.replace(f"__PREMIUM_ENTITY_{index}__", entity)
    return value


# ================= 完整真实数据区 =================
# ================================================


REFERRAL_RATE = 0.05
REFERRAL_PAGE_SIZE = 8
WITHDRAW_MINIMUM = 10.0
WITHDRAWALS_FILE = "/root/fakabot/withdrawals.json"
_withdraw_lock = threading.Lock()
ORDERS_FILE = "/root/fakabot/orders.json"

ARCHIVE_CHAT_ID = os.getenv("TELEGRAM_ARCHIVE_CHAT_ID", "-1004459109788").strip()
ARCHIVE_STORE = None
PAYMENT_WATCHER = None
ALIPAY_QR_FILE = "/root/fakabot/alipay_receive_qr.jpg"
ALIPAY_QR_UPLOAD_PENDING = set()
BOT_PROFILE_PHOTO_FILE = "/root/fakabot/bot_profile_photo.jpg"
BOT_PROFILE_PHOTO_UPLOAD_PENDING = set()

def _archive_key(path):
    name = os.path.basename(str(path))
    keys = {"users.json": "users", "orders.json": "orders", "withdrawals.json": "withdrawals"}
    if name not in keys:
        raise ValueError("未登记的业务数据类型，拒绝写入本地文件")
    return keys[name]

def load_json_file(path, default):
    if ARCHIVE_STORE is None:
        raise RuntimeError("Telegram 归档存储尚未初始化，拒绝读取本地业务数据")
    return ARCHIVE_STORE.load(_archive_key(path), default)

def save_json_file(path, value):
    if ARCHIVE_STORE is None:
        raise RuntimeError("Telegram 归档存储未初始化，拒绝写入本地业务数据")
    key = _archive_key(path)
    ARCHIVE_STORE.save(key, value)
    # A memory save is not a commit. Orders must reach the Telegram snapshot
    # before the caller can continue to supplier procurement or customer delivery.
    if key == "orders":
        try:
            ARCHIVE_STORE.flush("订单状态持久化提交")
        except Exception as exc:
            _notify_admin_critical("归档快照写入失败", exc, "orders flush")
            raise

def bind_referrer(uid, start_payload):
    """Bind an inviter once only. Existing bindings are never overwritten."""
    payload = str(start_payload or "").strip()
    inviter_id = payload[4:].strip() if payload.startswith("ref_") else payload
    uid, inviter_id = str(uid), str(inviter_id)
    if not inviter_id.isdigit() or inviter_id == uid:
        return False
    users = load_json_file(USERS_FILE, {})
    if not isinstance(users, dict) or inviter_id not in users or uid not in users:
        return False
    current = users[uid].get("referred_by")
    if current:
        return False
    users[uid]["referred_by"] = inviter_id
    users[uid]["referred_at"] = time.time()
    users[inviter_id].setdefault("referral_count", 0)
    users[inviter_id]["referral_count"] = int(users[inviter_id]["referral_count"] or 0) + 1
    save_json_file(USERS_FILE, users)
    return True

def _order_user_id(order):
    return str(order.get("user_id") or order.get("telegram_id") or order.get("buyer_id") or "")

def _order_is_paid(order):
    return (
        str(order.get("payment_status", "")).lower() in ("paid", "success", "completed", "confirmed")
        or str(order.get("status", "")).lower() in ("paid", "success", "completed", "delivered")
        or bool(order.get("paid_at") or order.get("paid_time") or order.get("confirmed_at"))
    )

def _order_amount(order):
    for key in ("amount", "price", "total", "paid_amount", "amount_usdt"):
        try:
            value = float(order.get(key))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        return int(order.get("amount_raw", 0)) / 1_000_000
    except (TypeError, ValueError):
        return 0.0

def referral_spend_map():
    orders = load_json_file(ORDERS_FILE, [])
    totals = {}
    if not isinstance(orders, list):
        return totals
    for order in orders:
        if isinstance(order, dict) and _order_is_paid(order):
            uid = _order_user_id(order)
            totals[uid] = totals.get(uid, 0.0) + _order_amount(order)
    return totals

def sync_referral_rewards():
    """Idempotently credit 5% for paid orders belonging to permanently bound invitees."""
    users = load_json_file(USERS_FILE, {})
    orders = load_json_file(ORDERS_FILE, [])
    if not isinstance(users, dict) or not isinstance(orders, list):
        return
    changed = False
    for order in orders:
        if not isinstance(order, dict) or not _order_is_paid(order):
            continue
        buyer_id = _order_user_id(order)
        buyer = users.get(buyer_id, {})
        inviter_id = str(buyer.get("referred_by") or "")
        if not inviter_id or inviter_id not in users:
            continue
        order_id = str(order.get("order_id") or order.get("id") or "")
        if not order_id:
            continue
        inviter = users[inviter_id]
        ledger = inviter.setdefault("referral_earnings", [])
        if any(str(x.get("order_id")) == order_id for x in ledger if isinstance(x, dict)):
            continue
        amount = round(_order_amount(order) * REFERRAL_RATE, 6)
        if amount <= 0:
            continue
        ledger.append({
            "order_id": order_id, "invitee_id": buyer_id,
            "spent": round(_order_amount(order), 6), "commission": amount,
            "created_at": order.get("paid_at") or order.get("paid_time") or order.get("confirmed_at") or time.time()
        })
        inviter["balance"] = round(float(inviter.get("balance", 0) or 0) + amount, 6)
        inviter["invite_bonus"] = round(float(inviter.get("invite_bonus", 0) or 0) + amount, 6)
        changed = True
    if changed:
        save_json_file(USERS_FILE, users)

def format_local_time(value):
    try:
        if isinstance(value, (int, float)):
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
        return str(value).replace("T", " ")[:16]
    except Exception:
        return "—"

def get_referral_page(uid, page=0):
    users = load_json_file(USERS_FILE, {})
    orders = load_json_file(ORDERS_FILE, [])
    rows = []
    for invitee_id, info in users.items():
        if str(info.get("referred_by") or "") != str(uid):
            continue
        invitee_name = str(info.get("name") or info.get("first_name") or info.get("username") or "Telegram User")
        if info.get("username"):
            invitee_name += f" (@{str(info['username']).lstrip('@')})"
        paid_orders = [
            order for order in orders
            if isinstance(order, dict) and _order_is_paid(order) and _order_user_id(order) == str(invitee_id)
        ] if isinstance(orders, list) else []
        if not paid_orders:
            rows.append({
                "nickname": invitee_name, "user_id": str(invitee_id),
                "date": info.get("referred_at") or info.get("join_time"),
                "product": "—", "order_id": "—", "amount": 0.0, "commission": 0.0,
                "rate": REFERRAL_RATE, "no_order": True
            })
        for order in paid_orders:
            amount = _order_amount(order)
            rows.append({
                "nickname": invitee_name, "user_id": str(invitee_id),
                "date": order.get("paid_at") or order.get("paid_time") or order.get("confirmed_at") or order.get("created_at"),
                "product": str(order.get("product_name") or order.get("product_title") or order.get("item_name") or order.get("product") or "商品信息未记录"),
                "order_id": str(order.get("order_id") or order.get("id") or "—"),
                "amount": amount, "commission": round(amount * REFERRAL_RATE, 6),
                "rate": REFERRAL_RATE, "no_order": False
            })
    rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    total = len(rows)
    pages = max(1, (total + REFERRAL_PAGE_SIZE - 1) // REFERRAL_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    selected = rows[page * REFERRAL_PAGE_SIZE:(page + 1) * REFERRAL_PAGE_SIZE]
    return selected, total, pages, page


def get_invite_kb(language="zh"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if language == "en":
        kb.row(
            types.InlineKeyboardButton("📋 Friends", callback_data="invite_friends_0"),
            types.InlineKeyboardButton("📊 Spending details", callback_data="invite_spending_0")
        )
        kb.row(
            types.InlineKeyboardButton("👤 User center", callback_data="ui_recharge"),
            types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
        )
    else:
        kb.row(
            types.InlineKeyboardButton("📋 好友名单", callback_data="invite_friends_0"),
            types.InlineKeyboardButton("📊 消费明细", callback_data="invite_spending_0")
        )
        kb.row(
            types.InlineKeyboardButton("👤 用户中心", callback_data="ui_recharge"),
            types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
        )
    return kb

def get_invite_friends_kb(page, pages, language="zh"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if language == "en":
        kb.row(premium_button("Previous", callback_data=f"invite_friends_{max(0,page-1)}"),
               premium_button("Next", callback_data=f"invite_friends_{min(pages-1,page+1)}"))
        kb.row(types.InlineKeyboardButton("👤 用户中心", callback_data="ui_recharge"),
               types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_invite"))
    else:
        kb.row(premium_button("上一页", callback_data=f"invite_friends_{max(0,page-1)}"),
               premium_button("下一页", callback_data=f"invite_friends_{min(pages-1,page+1)}"))
        kb.row(types.InlineKeyboardButton("👤 用户中心", callback_data="ui_recharge"),
               types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_invite"))
    return kb

def get_wallet_kb(language="zh"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if language == "en":
        kb.row(
            types.InlineKeyboardButton("💳 Bind address", callback_data="wallet_bind"),
            types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_recharge"),
        )
    else:
        kb.row(
            types.InlineKeyboardButton("💳 绑定地址", callback_data="wallet_bind"),
            types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_recharge"),
        )
    return kb


def valid_tron_address(address):
    return bool(re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", str(address or "").strip()))


def get_user(uid, name=""):
    users = load_json_file(USERS_FILE, {})
    uid_str = str(uid)
    changed = False
    if uid_str not in users:
        users[uid_str] = {"name": name, "join_time": time.time(), "balance": 0.0, "orders_count": 0, "invited_count": 0, "invite_bonus": 0.0, "language": "zh"}
        changed = True
    users[uid_str].setdefault("language", "zh")
    if name and users[uid_str].get("name") != str(name):
        users[uid_str]["name"] = str(name)
        changed = True
    if changed:
        save_json_file(USERS_FILE, users)
    return users[uid_str]

def set_user_language(uid, language):
    users = load_json_file(USERS_FILE, {})
    users.setdefault(str(uid), {})["language"] = language
    save_json_file(USERS_FILE, users)

def enforce_two_column_keyboard(kb):
    """Keep product catalogs in their explicitly authored one-column layout."""
    if kb is None or not getattr(kb, "keyboard", None):
        return kb
    # Product rows must remain one item per line, with the three navigation
    # controls together on the final line. Do not globally flatten this keyboard.
    callbacks = [
        str(getattr(button, "callback_data", "") or "")
        for row in kb.keyboard for button in row
    ]
    # The /start main menu intentionally has seven buttons; do not auto-append
    # a synthetic "返回主菜单" button to this one keyboard.
    if {"cat_account", "ui_orders", "ui_topup", "ui_recharge", "ui_ledger", "ui_help", "show_support", "more_services"}.issubset(set(callbacks)):
        return kb
    if "wallet_manage" in callbacks and "ui_topup" in callbacks:
        return kb
    if (
        any(value.startswith("buy_") for value in callbacks)
        and any(value.startswith("products_") for value in callbacks)
    ) or any(value.startswith("list_up") or value.startswith("catpage_") for value in callbacks):
        # Storefront category and product pages own their row layout; preserve it.
        return kb
    buttons = [button for row in kb.keyboard for button in row]
    if not buttons:
        return kb
    if len(buttons) % 2:
        last_button = buttons[-1]
        last_text = str(getattr(last_button, "text", "") or "").lower()
        is_existing_back = "⬅️ 返回" in last_text
        if not is_existing_back:
            try:
                back_button = types.InlineKeyboardButton(
                    text="⬅️ 返回",
                    callback_data="ui_buy"
                )
            except TypeError:
                back_button = types.InlineKeyboardButton(
                    text="⬅️ 返回", callback_data="ui_buy"
                )
            buttons.append(back_button)
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    # pyTelegramBotAPI exposes keyboard as a read-only property; mutate its list in place.
    kb.keyboard[:] = rows
    return kb

def _alignment_display_width(value):
    """Estimate Telegram rendered columns: CJK/full-width glyphs count as two."""
    value = html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    width = 0
    for char in value:
        code = ord(char)
        if char == "\u200d" or char in ("\ufe0e", "\ufe0f", "\u20e3"):
            continue
        if unicodedata.combining(char) or 0x1F3FB <= code <= 0x1F3FF:
            continue
        east = unicodedata.east_asian_width(char)
        width += 2 if east in ("W", "F") or 0x1F000 <= code <= 0x1FAFF else 1
    return width


def _wrap_alignment_text(text, max_width):
    """Wrap visible copy to the keyboard width while preserving HTML markup and wording."""
    if max_width <= 0:
        return text
    token_re = re.compile(r"(<[^>]*>|&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);|\s+|[^\s<>&]+|.)", re.DOTALL)
    tokens = token_re.findall(text)
    out = []
    line_width = 0
    pending_space = ""

    def emit_word(word, raw_width):
        nonlocal line_width
        if not word:
            return
        if line_width and line_width + raw_width > max_width:
            out.append("\n")
            line_width = 0
        if raw_width <= max_width:
            out.append(word)
            line_width += raw_width
            return
        # A single unbroken token (e.g. a wallet address) is split only at
        # Unicode code-point boundaries; no characters are removed or rewritten.
        pieces = re.findall(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);|.", word, re.DOTALL)
        for piece in pieces:
            piece_width = _alignment_display_width(html.unescape(piece))
            if line_width and line_width + piece_width > max_width:
                out.append("\n")
                line_width = 0
            out.append(piece)
            line_width += piece_width

    for token in tokens:
        if token.startswith("<") and token.endswith(">"):
            out.append(token)
            continue
        if token.startswith("&") and token.endswith(";"):
            decoded = html.unescape(token)
            token_width = _alignment_display_width(decoded)
            if line_width and line_width + token_width > max_width:
                out.append("\n")
                line_width = 0
            out.append(token)
            line_width += token_width
            continue
        if token.isspace():
            if "\n" in token or "\r" in token:
                out.append(token)
                line_width = 0
                pending_space = ""
            else:
                pending_space += token
            continue
        token_width = _alignment_display_width(token)
        if pending_space:
            if line_width and line_width + _alignment_display_width(pending_space) + token_width > max_width:
                out.append("\n")
                line_width = 0
            else:
                out.append(pending_space)
                line_width += _alignment_display_width(pending_space)
            pending_space = ""
        emit_word(token, token_width)
    if pending_space:
        out.append(pending_space)
    return "".join(out)


def align_text_and_inline_keyboard(text, markup):
    """Preserve authored message text and inline keyboard labels verbatim."""
    return text, markup


def _is_button_icon_error(exc):
    message = str(exc).lower()
    return any(token in message for token in (
        "icon_custom_emoji_id", "button_icon_invalid",
        "button_icon_emoji_not_allowed", "button icon"
    ))


def _is_premium_entity_error(exc):
    message = str(exc).lower()
    return any(token in message for token in (
        "can't parse entities", "cannot parse entities",
        "unsupported start tag", "unsupported entity",
        "custom emoji entity", "emoji entity"
    ))


def _strip_premium_entities(value):
    return re.sub(
        r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>",
        r"\1",
        str(value or ""),
        flags=re.DOTALL,
    )


def _call_with_premium_fallback(api_call, args, kwargs, text_index):
    """Retry only optional Premium decorations; never let them break core bot replies."""
    current_args = list(args)
    current_kwargs = dict(kwargs)
    for _ in range(3):
        try:
            return api_call(*current_args, **current_kwargs)
        except Exception as exc:
            changed = False
            text_value = None
            if len(current_args) > text_index:
                text_value = current_args[text_index]
            elif "text" in current_kwargs:
                text_value = current_kwargs["text"]

            if (
                _is_premium_entity_error(exc)
                and isinstance(text_value, str)
                and "<tg-emoji" in text_value
            ):
                plain_text = _strip_premium_entities(text_value)
                if len(current_args) > text_index:
                    current_args[text_index] = plain_text
                else:
                    current_kwargs["text"] = plain_text
                changed = True

            markup = current_kwargs.get("reply_markup")
            if _is_button_icon_error(exc) and markup is not None:
                for row in getattr(markup, "keyboard", []):
                    for button in row:
                        if hasattr(button, "icon_custom_emoji_id") and button.icon_custom_emoji_id:
                            button.icon_custom_emoji_id = None
                            changed = True

            if not changed:
                raise
    raise RuntimeError("Telegram API repeatedly rejected optional Premium decorations")


def _bind_aligned_api_call(api_call, args, kwargs, text_field, premiumize=False, default_html=False):
    """Normalize positional and keyword Bot API arguments through one alignment path."""
    bound = inspect.signature(api_call).bind_partial(*args, **kwargs)
    markup = bound.arguments.get("reply_markup")
    text_value = bound.arguments.get(text_field)
    if isinstance(text_value, str):
        text_value, aligned_markup = align_text_and_inline_keyboard(text_value, markup)
        if aligned_markup is not None:
            markup = aligned_markup
        if premiumize:
            text_value = premiumize_text(text_value)
        bound.arguments[text_field] = text_value
    if markup is not None:
        bound.arguments["reply_markup"] = markup
    if default_html and bound.arguments.get("parse_mode") is None:
        bound.arguments["parse_mode"] = "HTML"
    return bound.args, bound.kwargs


_original_send_message = bot.send_message
def _send_message_with_premium_emoji(*args, **kwargs):
    args, kwargs = _bind_aligned_api_call(
        _original_send_message, args, kwargs, "text", premiumize=True, default_html=True
    )
    return _call_with_premium_fallback(_original_send_message, args, kwargs, 1)
bot.send_message = _send_message_with_premium_emoji

_original_edit_message_text = bot.edit_message_text
def _edit_message_text_two_columns(*args, **kwargs):
    args, kwargs = _bind_aligned_api_call(
        _original_edit_message_text, args, kwargs, "text", premiumize=True, default_html=True
    )
    return _call_with_premium_fallback(_original_edit_message_text, args, kwargs, 0)
bot.edit_message_text = _edit_message_text_two_columns

_original_send_photo = bot.send_photo
def _send_photo_aligned(*args, **kwargs):
    args, kwargs = _bind_aligned_api_call(
        _original_send_photo, args, kwargs, "caption", premiumize=False, default_html=False
    )
    return _original_send_photo(*args, **kwargs)
bot.send_photo = _send_photo_aligned

_original_edit_message_caption = bot.edit_message_caption
def _edit_message_caption_aligned(*args, **kwargs):
    args, kwargs = _bind_aligned_api_call(
        _original_edit_message_caption, args, kwargs, "caption", premiumize=False, default_html=False
    )
    return _original_edit_message_caption(*args, **kwargs)
bot.edit_message_caption = _edit_message_caption_aligned

def safe_edit(bot, chat_id, msg_id, text, kb=None, premiumize=True, align_copy=True):
    try:
        rendered_text = premiumize_text(text) if premiumize else str(text)
        if align_copy is True:
            bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                  text=rendered_text, parse_mode="HTML",
                                  reply_markup=kb)
        elif align_copy == "text":
            aligned_text, aligned_kb = align_text_and_inline_keyboard(rendered_text, kb)
            _original_edit_message_text(chat_id=chat_id, message_id=msg_id,
                                        text=aligned_text, parse_mode="HTML",
                                        reply_markup=aligned_kb)
        else:
            aligned_text, aligned_kb = align_text_and_inline_keyboard(rendered_text, kb)
            _original_edit_message_text(chat_id=chat_id, message_id=msg_id,
                                        text=aligned_text, parse_mode="HTML",
                                        reply_markup=aligned_kb)
    except Exception as exc:
        print("⚠️ 消息更新失败：", repr(exc), flush=True)
        if "message is not modified" not in str(exc).lower():
            try:
                fallback_text = premiumize_text(text) if premiumize else str(text)
                if align_copy is True:
                    bot.send_message(chat_id, fallback_text, parse_mode="HTML", reply_markup=kb)
                elif align_copy == "text":
                    aligned_text, aligned_kb = align_text_and_inline_keyboard(fallback_text, kb)
                    _original_send_message(chat_id, aligned_text, parse_mode="HTML", reply_markup=aligned_kb)
                else:
                    aligned_text, aligned_kb = align_text_and_inline_keyboard(fallback_text, kb)
                    _original_send_message(chat_id, aligned_text, parse_mode="HTML", reply_markup=aligned_kb)
            except Exception as send_exc:
                print("❌ 编辑失败后的补发也失败：", repr(send_exc), flush=True)

def get_main_kb():
    """晴天账号商务主菜单；固定四行双列，统一使用不重复的普通 Emoji。"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("🛍️ 账号商城", callback_data="cat_account"),
        types.InlineKeyboardButton("📦 我的订单", callback_data="ui_orders")
    )
    kb.row(
        types.InlineKeyboardButton("⭐ 邀请中心", callback_data="ui_invite"),
        types.InlineKeyboardButton("👤 用户中心", callback_data="ui_recharge")
    )
    kb.row(
        types.InlineKeyboardButton("🧾 账单明细", callback_data="ui_ledger"),
        types.InlineKeyboardButton("📘 服务指南", callback_data="ui_help")
    )
    kb.row(
        types.InlineKeyboardButton("💬 联系客服", callback_data="show_support"),
        types.InlineKeyboardButton("🧩 更多服务", callback_data="more_services")
    )
    return kb
def get_more_kb(language="zh"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if language == "en":
        kb.row(
            types.InlineKeyboardButton("💬 Node Community", url="https://t.me/qingtian_vip"),
            types.InlineKeyboardButton("📢 Proxy Channel", url="https://t.me/jiedian888")
        )
        kb.row(
            types.InlineKeyboardButton("🌐 English / 中文", callback_data="lang_zh"),
            types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
        )
    else:
        kb.row(
            types.InlineKeyboardButton("💬 节点交流群", url="https://t.me/qingtian_vip"),
            types.InlineKeyboardButton("📢 代理频道", url="https://t.me/jiedian888")
        )
        kb.row(
            types.InlineKeyboardButton("🌐 English / 中文", callback_data="lang_en"),
            types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
        )
    return kb

# Brand groups are navigation categories; sale items/prices are added only after stock is supplied.
BRAND_GROUPS = {
    "social": {"title": "社交账号", "brands": [("TikTok", "🎵"), ("Facebook", "📘"), ("Instagram", "📸"), ("Twitter / X", "𝕏"), ("Discord", "🎮")]},
    "email": {"title": "邮箱账号", "brands": [("Google 邮箱", "✉️"), ("微软邮箱", "🪟"), ("Yahoo 邮箱", "💜"), ("Mail.ru 邮箱", "📨")]},
    "video": {"title": "视频账号", "brands": [("YouTube", "▶️")]},
    "communication": {"title": "通讯账号", "brands": [("Teams", "👥"), ("WhatsApp", "💬"), ("LINE", "🟢"), ("TextNow", "📱")]},
    "commerce": {"title": "电商账号", "brands": [("Amazon", "📦"), ("Walmart", "🏬")]},
}

def get_storefront_categories():
    """Fixed local Russian mailbox category first, followed by supplier categories."""
    local_category = {
        "id": "local-russian-email",
        "key": "russianmail",
        "name": "俄罗斯邮箱",
        "alias": "russianemail",
        "icon": "📧",
        "display_icon": "📧",
        "product_count": 1,
        "is_local_virtual_category": True,
    }
    # Hide obsolete/duplicate categories by semantic alias and display name;
    # keep the TDATA Telegram category as the sole Telegram storefront entry.
    hidden_aliases = {"gv", "apisms", "tgapi"}
    hidden_name_tokens = ("google voice", "谷歌语音", "api手机短信接码", "telegram飞机-api接码")
    supplier_categories = []
    for category in UPSTREAM_CATEGORIES:
        if category.get("is_local_virtual_category") or str(category.get("id") or "") == "local-russian-email":
            continue
        alias = str(category.get("alias") or "").strip().casefold()
        name = str(category.get("name") or "").strip().casefold()
        if alias in hidden_aliases or any(token in name for token in hidden_name_tokens):
            continue
        supplier_categories.append(category)

    # Explicit hot-category priority; all unlisted categories retain upstream order.
    priority_aliases = {
        "telegram": 0,
        "appleid": 1,
        "38519c6ab0b84e08b22a9d23688424b8": 3,  # AI平台/ChatGPT
        "discord": 4,
    }
    def _category_priority(category):
        if str(category.get("key") or "") == "russianmail":
            return 2
        return priority_aliases.get(str(category.get("alias") or "").strip().casefold(), 1000)

    supplier_categories.sort(key=_category_priority)
    return supplier_categories[:2] + [local_category] + supplier_categories[2:]


def get_account_page_text(page=0):
    """Storefront introduction with live clean-category totals and current pagination."""
    categories = get_storefront_categories()
    page_size = 10
    total = len(categories)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(int(page), pages - 1))
    return (
        "<b>🛒 晴天商城</b>\n"
        "品类丰富 · 规格明细 · 人民币（¥）计价\n\n"
        "请选择下方分类浏览商品，或直接发送商品名称/编号/关键词进行搜索。\n\n"
        f"<b>全部品类：{total} 类</b>\n"
        f"<b>当前页：第 {page + 1}/{pages} 页</b> (每页 {page_size} 类)\n\n"
        "点击下方分类，查看商品规格、库存与价格。"
    )


def get_account_kb(page=0):
    """Compact, paginated storefront navigation; hide empty upstream categories."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    categories = get_storefront_categories()
    page_size = 10
    pages = max(1, (len(categories) + page_size - 1) // page_size)
    page = max(0, min(int(page), pages - 1))
    visible = categories[page * page_size:(page + 1) * page_size]
    buttons = []
    # Read administrator-managed category icons from the durable Telegram archive.
    _meta = ARCHIVE_STORE.load("meta", {}) if ARCHIVE_STORE is not None else {}
    _configured_icons = _meta.get("storefront_category_icons", {}) if isinstance(_meta, dict) else {}
    # Short, explicit display labels: never truncate upstream category names with ellipses.
    label_icon_by_alias = {
        "google": ("Google", "🔍"), "microsoft": ("Outlook", "🪟"),
        "yahoo": ("Yahoo", "💜"), "mail": ("其他邮箱", "📨"),
        "facebook": ("Facebook", "🔵"), "twitter": ("XX", "🧩"),
        "instagram": ("Instagram", "📸"), "discord": ("Discord", "🎮"),
        "tk": ("TikTok", "🎵"), "tgapi": ("Telegram", "📱"),
        "telegram": ("Telegram飞机", "✈️"), "appleid": ("苹果ID", "🍎"),
        "whatsapp": ("WhatsApp", "💬"), "linkedin": ("LinkedIn", "💼"),
        "microsoftteams": ("Teams", "💻"), "youtube": ("YouTube", "▶️"),
        "steam": ("Steam", "🕹️"), "other": ("其他账号", "🧩"),
        "russianemail": ("俄罗斯邮箱", "📧"),
    }
    for category in visible:
        category_key = str(category.get("key", ""))
        alias = str(category.get("alias") or "").strip().lower()
        source_name = str(category.get("name") or "未命名分类").strip()
        _short_label, category_icon = label_icon_by_alias.get(
            alias, ("", str(category.get("display_icon") or "🛍️"))
        )
        name = "Telegram飞机" if alias == "telegram" else source_name
        # Per-category icon configuration is keyed by the real catalog key,
        # never by parallel-array position or a guessed Premium ID.
        icon_config = (_configured_icons.get(category_key, {}) if isinstance(_configured_icons, dict) else {})
        if category.get("is_local_virtual_category"):
            icon_config = {}
        custom_emoji_id = str(icon_config.get("custom_emoji_id") or "").strip() if isinstance(icon_config, dict) else ""
        native_emoji = str(icon_config.get("emoji") or "").strip() if isinstance(icon_config, dict) else ""
        button_text = f"{name}" if custom_emoji_id else f"{native_emoji or category_icon} {name}"
        button_kwargs = {"text": button_text, "callback_data": f"list_{category_key}"}
        if custom_emoji_id:
            button_kwargs["icon_custom_emoji_id"] = custom_emoji_id
        buttons.append(types.InlineKeyboardButton(**button_kwargs))
    # Category grid is independent from navigation. Paired categories share
    # equal-width rows; a final odd category gets its own full-width row.
    for index in range(0, len(buttons) - 1, 2):
        kb.row(buttons[index], buttons[index + 1])
    if len(buttons) % 2:
        kb.row(buttons[-1])

    # Navigation is always separate: two-column paging row, then full-width Return.
    kb.row(
        types.InlineKeyboardButton("⬅️ 上一页", callback_data=f"catpage_{max(0, page - 1)}"),
        types.InlineKeyboardButton("➡️ 下一页", callback_data=f"catpage_{min(pages - 1, page + 1)}")
    )
    kb.row(types.InlineKeyboardButton("👤 用户中心", callback_data="ui_recharge"),
           types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"))
    return kb

def edit_account_page(chat_id, message_id, page):
    """Edit storefront landing page without character-level forced text wrapping."""
    page_text = get_account_page_text(page)
    page_keyboard = get_account_kb(page)
    try:
        _original_edit_message_text(
            chat_id=chat_id, message_id=message_id, text=page_text,
            parse_mode="HTML", reply_markup=page_keyboard
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            print("⚠️ 商城首页更新失败：", repr(exc), flush=True)
            _original_send_message(
                chat_id, page_text, parse_mode="HTML", reply_markup=page_keyboard
            )


def get_brand_group_kb(group_id):
    group = BRAND_GROUPS[group_id]
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for brand_index, (brand_name, brand_emoji) in enumerate(group["brands"]):
        probe = (group_id + " " + brand_name).lower()
        if "apple" in probe or "苹果" in probe:
            icon_key = "apple"
        elif "telegram" in probe or "tg" in probe:
            icon_key = "tg"
        elif "ai" in probe or "chatgpt" in probe:
            icon_key = "ai"
        elif any(word in probe for word in ("youtube", "视频", "steam", "游戏")):
            icon_key = "video"
        elif any(word in probe for word in ("邮箱", "google", "微软", "yahoo", "mail.ru")):
            icon_key = "gear"
        elif any(word in probe for word in ("amazon", "walmart", "电商")):
            icon_key = "diamond"
        elif "social" in probe or any(word in probe for word in ("tiktok", "facebook", "instagram", "twitter", "discord", "whatsapp", "line", "textnow")):
            icon_key = "star"
        else:
            icon_key = "diamond"
        buttons.append(premium_button(
            f"{brand_emoji} {brand_name}", emoji_key=icon_key,
            callback_data=f"brand_{group_id}_{brand_index}"
        ))
    back = types.InlineKeyboardButton("⬅️ 返回", callback_data="cat_account")
    for index in range(0, len(buttons) - 1, 2):
        kb.row(buttons[index], buttons[index + 1])
    if len(buttons) % 2:
        kb.row(buttons[-1], back)
    else:
        kb.row(types.InlineKeyboardButton("💬 联系客服", url="https://t.me/qingtian_Boss"), back)
    return kb

def get_product_catalog(cat_id):
    if cat_id == "russianmail":
        from products.local_inventory import get_local_products
        return get_local_products()
    upstream_meta = UPSTREAM_CATEGORY_BY_KEY.get(cat_id, {})
    if upstream_meta.get("is_local_virtual_category") or str(upstream_meta.get("id") or "") == "local-russian-email":
        return []
    # One Telegram storefront entry combines API-number and TDATA inventory.
    if cat_id == "up_tg":
        source_keys = [
            str(category.get("key") or "") for category in UPSTREAM_CATEGORIES
            if str(category.get("alias") or "").strip().lower() in {"tgapi", "telegram"}
        ]
        return [product for key in source_keys for product in UPSTREAM_PRODUCT_CATALOGS.get(key, [])]
    # Brand groups are navigation only. Never expose a brand placeholder as a sale item.
    if cat_id in BRAND_GROUPS:
        return [
            {"id": f"{cat_id}_{i}", "name": name, "brand_index": i, "is_brand_entry": True}
            for i, (name, _emoji) in enumerate(BRAND_GROUPS[cat_id]["brands"])
        ]
    return PRODUCT_CATALOGS.get(cat_id, [])


def get_product_page_text(cat_id, page=0):
    products = get_product_catalog(cat_id)
    total = len(products)
    page_size = 8
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(int(page), pages - 1))
    titles = {
        "apple": ("🍎", "苹果 ID 专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
        "tg": ("✈️", "TG 账号专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
        "instagram": ("📸", "Instagram / Threads 账号专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
        "ai": ("🤖", "AI 账号专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
        "social": ("👥", "社交账号专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
        "email": ("✉️", "邮箱账号专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
        "video": ("🎬", "视频账号专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
        "communication": ("💬", "通讯账号专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
        "commerce": ("🛒", "电商账号专区", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："),
    }
    upstream_category = UPSTREAM_CATEGORY_BY_KEY.get(cat_id)
    if cat_id == "russianmail":
        titles[cat_id] = ("📧", "俄罗斯邮箱", "本地现货库存 · 余额支付后自动发货 · 售价 ¥2.00 / 个。")
    if cat_id == "up_tg":
        titles[cat_id] = ("✈️", "Telegram飞机 - TDATA直登", "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情：")
    if upstream_category:
        titles[cat_id] = (
            upstream_category.get("display_icon", "🛍️"),
            upstream_category.get("name", "商品专区"),
            "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："
        )
    if cat_id in {"up11", "up_tg"}:
        stock = total
        current_page = page + 1
        total_pages = pages
        return (
            "<b>✈️ Telegram飞机 - TDATA直登</b>\n"
            f"当前库存：{stock} 个 | 当前第 {current_page}/{total_pages} 页\n\n"
            "仅展示当前有货的成品账号；地区、年限、邮箱及登录方式请以商品详情为准。请点击下方按钮查看详情："
        )
    icon, title, intro = titles.get(cat_id, ("🛍️", "商品专区", "仅展示当前有货的成品账号；请点击下方按钮查看详情："))
    return (
        f"<b>{html.escape(str(icon))} {html.escape(str(title))}</b>\n"
        f"当前库存：{total} 个 | 当前第 {page + 1}/{pages} 页\n\n"
        f"{html.escape(intro)}"
    )


def get_catalog_product_icon(product, cat_id):
    """Resolve a visible brand/platform icon for every sale item and brand entry."""
    explicit = str(product.get("display_icon") or "").strip()
    if explicit and explicit != "🛍️":
        return explicit
    upstream_category = UPSTREAM_CATEGORY_BY_KEY.get(cat_id)
    if upstream_category and str(upstream_category.get("alias") or "").lower() == "russianemail":
        return "📧"
    name = str(product.get("name", "")).lower()
    # Local catalog IDs are authoritative; do not let short brand fragments collide.
    local_icons = {"apple": "🍎", "tg": "✈️", "instagram": "📸", "ai": "🤖"}
    if cat_id in local_icons:
        return local_icons[cat_id]
    if "microsoft teams" in name or "teams" in name:
        return "👥"
    if "chatgpt" in name or "openai" in name:
        return "🤖"
    brand_icons = (
        (("instagram", "threads"), "📸"), (("facebook",), "📘"),
        (("twitter", "推特", " x "), "𝕏"), (("discord",), "🎮"),
        (("tiktok", "抖音"), "🎵"), (("telegram", "电报", "飞机"), "✈️"),
        (("google", "谷歌", "gmail"), "✉️"), (("microsoft", "微软", "outlook", "hotmail", "teams"), "🪟"),
        (("yahoo", "雅虎"), "💜"), (("mail.ru", "邮箱", "mail"), "📨"),
        (("whatsapp",), "💬"), (("line",), "🟢"), (("textnow",), "📱"),
        (("linkedin", "领英"), "💼"), (("youtube", "优酷"), "▶️"),
        (("netflix", "奈飞"), "🎬"), (("twitch",), "🟣"),
        (("steam",), "🎮"), (("amazon", "亚马逊", "aws"), "📦"),
        (("walmart", "沃尔玛"), "🏬"), (("apple", "苹果", "icloud", "小火箭"), "🍎"),
        (("google voice",), "📞"),
    )
    for keywords, icon in brand_icons:
        if any(word in name for word in keywords):
            return icon
    upstream = UPSTREAM_CATEGORY_BY_KEY.get(cat_id)
    if upstream:
        icon = str(upstream.get("display_icon") or "").strip()
        if icon and icon != "🛍️":
            return icon
    category_icons = {"social": "👥", "email": "✉️", "video": "🎬", "communication": "💬", "commerce": "🛒"}
    return category_icons.get(cat_id, "🧩")



def _customer_safe_product_name(value):
    """Translate technical supplier shorthand into clear customer-facing wording."""
    name = str(value or "商品")
    name = re.sub(r"(?i)\bGraph\s*API\b", "Graph 数据功能", name)
    name = re.sub(r"(?i)(微软邮箱验证)\s*\+\s*api\b", r"\1+自动数据接入", name)
    name = re.sub(r"(?i)\bapi\b", "数据接入功能", name)
    name = name.replace("接口", "数据功能")
    return name


def compact_product_button_label(product):
    """Extract a short SKU + region label without changing the product identity."""
    import re
    name = str(product.get("name") or "").strip()
    sku = str(product.get("supplier_product_id") or product.get("id") or "").strip()
    match_sku = re.search(r"\[货号\s*([^\]]+)\]", name)
    if match_sku:
        sku = match_sku.group(1).strip()
    region = ""
    # Prefer the first country/region segment after the first vertical separator.
    parts = [part.strip() for part in name.split("|")]
    for part in parts[1:]:
        match_region = re.search(r"(\+\s*\d{1,4}\s*[^|]+)", part)
        if match_region:
            region = re.sub(r"\s+", "", match_region.group(1)).strip()
            break
    if not region:
        match_region = re.search(r"(\+\s*\d{1,4}\s*[\u4e00-\u9fffA-Za-z][^|]*)", name)
        if match_region:
            region = re.sub(r"\s+", "", match_region.group(1)).strip()
    label = f"[货号{sku}] {region}" if region else f"[货号{sku}]"
    return _customer_safe_product_name(label)


def _supplier_detail_text(data):
    """Pick supplier description and convert its HTML markup to readable plain text."""
    import re
    if not isinstance(data, dict):
        return ""
    candidates = ("Description", "ProductDesc", "Detail", "Content", "Remark", "Notice", "Introduction", "DescriptionText")
    for key in candidates:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            value = re.sub(r"(?i)</(li|p|h[1-6]|div|tr)>|<br\s*/?>", "\n", value)
            value = re.sub(r"<li[^>]*>", "• ", value, flags=re.I)
            value = re.sub(r"<[^>]+>", "", value)
            value = html.unescape(value)
            lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
            return "\n".join(line for line in lines if line)
    return ""


def get_products_kb(cat_id, page=0):
    products = get_product_catalog(cat_id)
    total = len(products)
    page_size = 8
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(int(page), pages - 1))
    selected = products[page * page_size:(page + 1) * page_size]
    # Every product occupies one full-width row; category icon configuration is untouched.
    kb = types.InlineKeyboardMarkup(row_width=1)
    if cat_id in BRAND_GROUPS:
        for product in selected:
            kb.row(premium_button(
                text=f"{get_catalog_product_icon(product, cat_id)} {product['name']}",
                callback_data=f"brand_{cat_id}_{product['brand_index']}"
            ))
    else:
        for product in selected:
            callback = f"buy_{product['id']}_{page}"
            if product.get("is_upstream") and str(product.get("name", "")).startswith("[货号"):
                label = compact_product_button_label(product)
            else:
                product_name = _customer_safe_product_name(product.get("name", "商品"))
                brand_icon = get_catalog_product_icon(product, cat_id)
                label = f"{brand_icon} {product_name} | ¥{price_cny(product['price']):.2f}"
            kb.row(premium_button(text=label, callback_data=callback))
    if not selected:
        kb.row(types.InlineKeyboardButton("💬 联系客服", url="https://t.me/qingtian_Boss"),
               types.InlineKeyboardButton("⬅️ 返回", callback_data="cat_account"))
    # The ninth keyboard position is always a paired, two-column navigation row.
    # Use the existing verified Telegram Premium icon IDs via premium_button().
    if page > 0 and page < pages - 1:
        left = premium_button("上一页", callback_data=f"products_{cat_id}_{page - 1}", emoji_key="star")
        right = premium_button("下一页", callback_data=f"products_{cat_id}_{page + 1}", emoji_key="diamond")
    elif page == 0 and pages > 1:
        left = premium_button("⬅️ 返回", callback_data="cat_account", emoji_key="gear")
        right = premium_button("下一页", callback_data=f"products_{cat_id}_{page + 1}", emoji_key="diamond")
    elif page > 0 and page == pages - 1:
        left = premium_button("上一页", callback_data=f"products_{cat_id}_{page - 1}", emoji_key="star")
        right = premium_button("⬅️ 返回", callback_data="cat_account", emoji_key="gear")
    else:
        # A one-page catalogue has no page direction; keep return paired with
        # a harmless refresh of the current page rather than a single-column row.
        left = premium_button("⬅️ 返回", callback_data="cat_account", emoji_key="gear")
        right = premium_button("当前页", callback_data=f"products_{cat_id}_{page}", emoji_key="star")
    kb.row(left, right)
    return kb


def get_back_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton("💬 联系客服", url="https://t.me/qingtian_Boss"),
           types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"))
    return kb

def _paid_order_count_for_user(uid, orders=None):
    """Count distinct successful orders from the current Telegram archive state."""
    if orders is None:
        orders = load_json_file(ORDERS_FILE, [])
    if not isinstance(orders, list):
        return 0
    seen = set()
    for order in orders:
        if not isinstance(order, dict) or _order_user_id(order) != str(uid) or not _order_is_paid(order):
            continue
        # Wallet top-ups and sandbox-only checks are not merchandise purchases.
        if order.get("wallet_topup") is True or order.get("payment_method") == "usdt_wallet_topup":
            continue
        if order.get("sandbox_test_order") is True or order.get("product_id") in ("sandbox_callback_test", "alipay_001_test"):
            continue
        order_id = str(order.get("order_id") or order.get("id") or "").strip()
        if order_id:
            seen.add(order_id)
    return len(seen)


def _reconcile_user_order_count(uid):
    """Repair the denormalized counter from authoritative paid order records."""
    orders = load_json_file(ORDERS_FILE, [])
    count = _paid_order_count_for_user(uid, orders)
    users = load_json_file(USERS_FILE, {})
    key = str(uid)
    user = users.get(key)
    if isinstance(user, dict) and int(user.get("orders_count", 0) or 0) != count:
        user["orders_count"] = count
        save_json_file(USERS_FILE, users)
        try:
            if ARCHIVE_STORE is not None:
                ARCHIVE_STORE.flush("主页累计订单数按已支付订单实时校准")
        except Exception as exc:
            print("主页订单统计归档延迟：", type(exc).__name__, flush=True)
    return count


def generate_main_text(user, uid):
    """晴天账号商务欢迎页：紧凑展示账户金额与交易概况。"""
    balance = float(user.get("balance", 0) or 0)
    orders = _reconcile_user_order_count(uid)
    invite_bonus = float(user.get("invite_bonus", 0) or 0)
    display_name = html.escape(str(user.get("name") or "尊贵用户"))
    blue_name = f'<a href="tg://user?id={int(uid)}">{display_name}</a>'
    language = str(user.get("language", "zh") or "zh")
    if language == "en":
        return (
            f'<b>Qingtian Account Store | Account Services</b>\n'
            f'Welcome, {blue_name}\n'
            f'<b>Account Overview</b>\n'
            f'User ID: <code>{html.escape(str(uid))}</code>\n'
            f'Balance: <b>¥{price_cny(balance):.2f}</b>\n'
            f'Total Orders: {orders}\n'
            f'Referral Rewards: <b>¥{price_cny(invite_bonus):.2f}</b>\n'
            f'————————————\n'
            f'<b>Featured Accounts · Easy Shopping</b>\n'
            f'Prices are shown on product pages. Send a product ID or keyword to search.'
        )
    return (
        f'<b>晴天号铺 | 账号服务</b>\n'
        f'欢迎您，{blue_name}\n'
        f'<b>账户概览</b>\n'
        f'用户编号：<code>{html.escape(str(uid))}</code>\n'
        f'账户余额：<b>¥{price_cny(balance):.2f}</b>\n'
        f'累计订单：{orders} 笔\n'
        f'邀请奖励：<b>¥{price_cny(invite_bonus):.2f}</b>\n'
        f'<a href="https://t.me/setlanguage/zh-hans">点这里切换【简体中文】</a>\n'
        f'————————————\n'
        f'<b>精选账号 · 轻松选购</b>\n'
        f'价格以商品页为准，发送商品编号或关键词可快速搜索。'
    )

@bot.message_handler(commands=['ping'])
def cmd_ping(message):
    """Small live-response probe for checking Telegram polling and outbound replies."""
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    bot.send_message(
        message.chat.id,
        f"晴天号铺在线。\n响应时间：<code>{now}</code>\n主菜单交互已就绪。",
        reply_markup=get_main_kb(),
    )


@bot.message_handler(commands=['start'])
def cmd_start(message):
    try:
        display_name = " ".join(part for part in [message.from_user.first_name, message.from_user.last_name] if part).strip() or "老板"
        user = get_user(message.from_user.id, display_name)
        if message.from_user.username:
            users = load_json_file(USERS_FILE, {})
            users.setdefault(str(message.from_user.id), {})["username"] = message.from_user.username
            save_json_file(USERS_FILE, users)
        payload = (message.text or "").split(maxsplit=1)
        if len(payload) > 1:
            start_payload = payload[1].strip().split()[0]
            language_payloads = {
                "lang_zh": ("zh", "✅ 已为您切换为【简体中文】。请继续操作。"),
                "language_zh": ("zh", "✅ 已为您切换为【简体中文】。请继续操作。"),
                "lang_en": ("en", "✅ English has been enabled. Please continue."),
                "language_en": ("en", "✅ English has been enabled. Please continue."),
            }
            if start_payload in language_payloads:
                language, confirmation = language_payloads[start_payload]
                set_user_language(message.from_user.id, language)
                bot.send_message(message.chat.id, confirmation)
                return
            bind_referrer(message.from_user.id, payload[1])
        response_text = generate_main_text(user, message.from_user.id)
        bot.send_message(message.chat.id, response_text, parse_mode="HTML", reply_markup=get_main_kb(), link_preview_options=types.LinkPreviewOptions(is_disabled=True))
    except Exception as exc:
        print("❌ /start 处理失败：", repr(exc), flush=True)
        bot.send_message(message.chat.id, "☀️ 晴天号铺正在恢复数据服务，请稍后重试。基础在线检测：/ping")

@bot.callback_query_handler(func=lambda call: call.data == "lang_home_toggle")
def toggle_home_language(call):
    user = get_user(call.from_user.id)
    language = "en" if str(user.get("language", "zh") or "zh") != "en" else "zh"
    set_user_language(call.from_user.id, language)
    user = get_user(call.from_user.id)
    bot.answer_callback_query(call.id, "Language: English" if language == "en" else "已切换为简体中文")
    safe_edit(bot, call.message.chat.id, call.message.message_id, generate_main_text(user, call.from_user.id), get_main_kb())


@bot.callback_query_handler(func=lambda call: call.data == "ui_buy")
def show_main_menu(call):
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    text = generate_main_text(user, call.from_user.id)
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_main_kb())

@bot.callback_query_handler(func=lambda call: call.data == "more_services")
def show_more_menu(call):
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    language = user.get("language", "zh")
    if language == "en":
        text = (
            "☀️ <b>QINGTIAN DIGITAL SERVICES</b>\n"
            "<i>Your trusted gateway to digital products and online services.</i>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💎 <b>Explore Our Services</b>\n"
            "• <b>Node Community</b> — connect with users, exchange setup tips and stay informed about service updates.\n"
            "• <b>Proxy Channel</b> — follow announcements, availability and product updates.\n"
            "• <b>Account Store</b> — discover new digital account products and store notices.\n\n"
            "🛡 <b>Service Commitment</b>\n"
            "We aim to provide clear product information, convenient self-service purchasing and responsive after-sales assistance. Please review each product description before ordering.\n\n"
            "🤝 <b>Need a hand?</b>\n"
            "Our support team can help with order questions, product guidance and basic troubleshooting. Choose a service below to continue."
        )
    else:
        text = (
            "🔥 <b>官方服务矩阵</b>\n\n"
            "💬 <b>节点交流群</b>：经验交流 / 配置建议\n"
            "📢 <b>代理频道</b>：公告 / 动态 / 通知\n"
            "🛍 <b>账号铺频道</b>：商品资讯 / 上新提醒\n\n"
            "🛡 <b>服务保障</b>\n"
            "持续完善自助选购与售后渠道。下单前请仔细阅读商品说明。\n"
            "如遇订单或使用问题，请点击下方【联系客服】获取协助。"
        )
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_more_kb(language))

@bot.callback_query_handler(func=lambda call: call.data in ("lang_en", "lang_zh"))
def switch_language(call):
    language = "en" if call.data == "lang_en" else "zh"
    user = get_user(call.from_user.id)
    set_user_language(call.from_user.id, language)
    bot.answer_callback_query(call.id, "Language: English" if language == "en" else "已切换为简体中文")
    if language == "en":
        text = (
            "☀️ <b>QINGTIAN DIGITAL SERVICES</b>\n"
            "<i>Your trusted gateway to digital products and online services.</i>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💎 <b>Explore Our Services</b>\n"
            "• <b>Node Community</b> — connect with users, exchange setup tips and stay informed about service updates.\n"
            "• <b>Proxy Channel</b> — follow announcements, availability and product updates.\n"
            "• <b>Account Store</b> — discover new digital account products and store notices.\n\n"
            "🛡 <b>Service Commitment</b>\n"
            "We aim to provide clear product information, convenient self-service purchasing and responsive after-sales assistance. Please review each product description before ordering.\n\n"
            "🤝 <b>Need a hand?</b>\n"
            "Our support team can help with order questions, product guidance and basic troubleshooting. Choose a service below to continue."
        )
    else:
        text = (
            "☀️ <b>晴天 · 数字生活服务中心</b>\n"
            "<i>让数字商品选购更简单，让每一次服务都有清晰指引。</i>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💎 <b>官方服务矩阵</b>\n"
            "• <b>节点交流群</b> — 与用户交流使用经验，获取配置建议及社群动态。\n"
            "• <b>代理资讯频道</b> — 关注服务公告、产品动态与重要通知。\n"
            "• <b>账号铺频道</b> — 浏览数字账号商品资讯、上新消息与使用提醒。\n\n"
            "🛡 <b>服务与保障</b>\n"
            "我们持续完善商品信息展示、自助选购流程与售后沟通渠道。下单前请仔细阅读商品规格、适用范围及使用说明；如遇订单或使用问题，可通过官方客服获得协助。\n\n"
            "🤝 <b>专属服务支持</b>\n"
            "无论是商品咨询、订单查询，还是基础使用指引，都可以进入客服页面联系人工团队。请选择下方对应入口。"
        )
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_more_kb(language))

@bot.callback_query_handler(func=lambda call: call.data == "show_support")
def show_support(call):
    bot.answer_callback_query(call.id)
    text = (
        "<b>✨ 晴天官方客服</b>\n"
        "为您提供清晰、及时的订单与商品咨询支持。\n\n"
        "🤝 <b>客服账号：</b>@qingtian_Boss\n"
        "💬 <b>沟通方式：</b>一对一私信\n"
        "📦 <b>主营业务：</b>苹果ID / TG会员 / 高速节点\n"
        "⚡ <b>发货服务：</b>7x24小时全自动发货\n\n"
        "📋 <b>服务支持：</b>\n"
        "商品规格与购买咨询、订单状态、支付发货、使用指引及售后问题。联系订单问题时请附上订单编号；请勿向任何人提供账户密码或短信验证码。\n\n"
        "<a href=\"https://t.me/qingtian_Boss\">https://t.me/qingtian_Boss</a>"
    )
    kb = types.InlineKeyboardMarkup(keyboard=[[
        types.InlineKeyboardButton(text="⬅️ 返回", callback_data="ui_buy"),
        types.InlineKeyboardButton(text="💬 客服", url="https://t.me/qingtian_Boss")
    ]])
    try:
        # Bypass global alignment/premiumization on this page to preserve authored HTML and rows.
        _original_send_message(
            chat_id=call.message.chat.id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb
        )
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
    except Exception as exc:
        print("❌ 客服页面发送失败：", repr(exc), flush=True)


@bot.callback_query_handler(func=lambda call: call.data == "cat_account")
def show_account_menu(call):
    bot.answer_callback_query(call.id)
    edit_account_page(call.message.chat.id, call.message.message_id, 0)

@bot.callback_query_handler(func=lambda call: call.data.startswith("brands_"))
def show_brand_group(call):
    bot.answer_callback_query(call.id)
    group_id = call.data.replace("brands_", "", 1)
    group = BRAND_GROUPS.get(group_id)
    if not group:
        bot.answer_callback_query(call.id, "分类不存在", show_alert=True)
        return
    safe_edit(
        bot, call.message.chat.id, call.message.message_id,
        get_product_page_text(group_id, 0), get_products_kb(group_id, 0)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("brand_"))
def show_brand_notice(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split("_")
    group_id = parts[1] if len(parts) > 1 else ""
    group = BRAND_GROUPS.get(group_id)
    if not group:
        return
    try:
        brand_index = int(parts[2])
        brand_name = group["brands"][brand_index][0]
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "品牌入口无效，请返回重试。", show_alert=True)
        return
    if group_id == "social":
        # Every social platform gets its own landing page. Only render purchasable
        # SKUs that actually exist in the catalog; never invent prices or stock.
        social_catalogs = {
            "Instagram": ("instagram", len(INSTAGRAM_PRODUCTS), "Instagram / Threads"),
        }
        kb = types.InlineKeyboardMarkup(row_width=2)
        if brand_name in social_catalogs:
            catalog_id, product_count, display_name = social_catalogs[brand_name]
            text = (
                f"🏷️ <b>{html.escape(display_name)} 商品分类</b>\n\n"
                f"📦 已登记商品：<b>{product_count} 种</b>\n"
                "👇 点击下方进入完整商品列表，选择具体规格后再购买。"
            )
            kb.add(premium_button(
                f"查看全部商品（{product_count}）",
                callback_data=f"products_{catalog_id}_0"
            ))
        else:
            text = (
                f"🏷️ <b>{html.escape(brand_name)} 商品分类</b>\n\n"
                "✅ 平台分类入口已开放。\n"
                "⏳ 当前仓库尚未录入该平台可核验的商品 SKU、价格和库存。\n\n"
                "为避免错价或无货订单，暂不开放购买；录入真实商品资料后会自动显示在这里。"
            )
            kb.add(premium_button("联系客服 / 提交商品资料", url="https://t.me/qingtian_Boss"))
        kb.row(types.InlineKeyboardButton("⬅️ 返回", callback_data="brands_social"),
               types.InlineKeyboardButton("📂 全部分类", callback_data="cat_account"))
        safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)
        return

    # Non-social brand groups keep their own explicit landing page.
    text = (
        f"🏷️ <b>{html.escape(brand_name)}</b>\n\n"
        "该平台目前尚未录入可核对的商品明细。\n"
        "为避免展示错误货号或价格，请联系客服确认上架信息。"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(premium_button("联系客服", url="https://t.me/qingtian_Boss"),
           types.InlineKeyboardButton("⬅️ 返回", callback_data=f"brands_{group_id}"))
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("catpage_"))
def show_account_category_page(call):
    bot.answer_callback_query(call.id)
    try:
        page = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        page = 0
    edit_account_page(call.message.chat.id, call.message.message_id, page)

@bot.callback_query_handler(func=lambda call: call.data.startswith("list_"))
def show_category_list(call):
    bot.answer_callback_query(call.id)
    cat_id = call.data.replace("list_", "")
    titles = {
        "apple": f"🍎 <b>苹果ID 专区（{len(APPLE_PRODUCTS)} 种账号）</b>",
        "tg": f"✈️ <b>TG账号 专区（{len(TG_PRODUCTS)} 种账号）</b>",
        "ai": f"🤖 <b>AI 专区（{len(AI_PRODUCTS)} 种账号）</b>",
        "video": "🎬 <b>视频账号</b>",
    }
    safe_edit(
        bot, call.message.chat.id, call.message.message_id,
        get_product_page_text(cat_id, 0), get_products_kb(cat_id, 0)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("products_"))
def show_product_page(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split("_")
    if len(parts) != 3:
        bot.answer_callback_query(call.id, "商品分页参数无效，请重新打开分类。", show_alert=True)
        return
    cat_id = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        page = 0
    if cat_id not in PRODUCT_CATALOGS and cat_id not in BRAND_GROUPS and cat_id not in {"up_tg", "russianmail"}:
        bot.answer_callback_query(call.id, "商品分类不存在。", show_alert=True)
        return
    safe_edit(
        bot, call.message.chat.id, call.message.message_id,
        get_product_page_text(cat_id, page), get_products_kb(cat_id, page)
    )


def _build_detail_payment_keyboard(prod_id, back_cat, return_page):
    """Canonical 2x2 payment layout used by every product detail branch."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(premium_button("💰 余额支付", callback_data=f"pay_balance_{prod_id}"),
           premium_button("🪙 USDT支付", callback_data=f"pay_tron_{prod_id}"))
    kb.row(premium_button("💳 支付宝", callback_data=f"pay_alipay_{prod_id}"),
           types.InlineKeyboardButton("⬅️ 返回", callback_data=f"products_{back_cat}_{int(return_page)}"))
    return kb


def _build_product_detail_view(product, prod_id, back_cat, return_page, verified, supplier_detail=None):
    """Single detail renderer and 2x2 payment keyboard for every non-local product."""
    import re
    supplier_detail = supplier_detail if isinstance(supplier_detail, dict) else {}
    raw_name = _customer_safe_product_name(product.get("name") or "商品")
    safe_name = html.escape(raw_name)
    category_key = str(product.get("category_key", ""))
    category_meta = UPSTREAM_CATEGORY_BY_KEY.get(category_key, {})
    source_alias = str(category_meta.get("alias") or "").strip().lower()
    is_tdata = source_alias == "telegram"
    supplier_id = product.get("supplier_product_id") or prod_id
    stock = int(product.get("supplier_stock", 0) or 0)
    price = price_cny(product.get("price", 0))
    description = _supplier_detail_text(supplier_detail)
    description_lines = []
    for line in description.splitlines():
        if re.search(r"(?i)测试|\btest\b|\bmock\b|\bdemo\b|示例|\bTODO\b|大号商|上游|供应商|供货商|代发|采购|供应链|接口|supplier|upstream|vendor", line):
            continue
        if line.strip():
            description_lines.append(line.strip())
    description = _customer_safe_product_name("\n".join(description_lines))

    if is_tdata:
        sku_match = re.search(r"\[货号\s*([^\]]+)\]", raw_name)
        sku = sku_match.group(1).strip() if sku_match else str(supplier_id)
        region = ""
        for part in raw_name.split("|")[1:]:
            match = re.search(r"\+\s*\d{1,4}\s*[^|]+", part)
            if match:
                region = re.sub(r"\s+", "", match.group(0)).strip()
                break
        age_match = re.search(r"注册\s*\d+\s*天\s*\+?", raw_name)
        age = re.sub(r"\s+", "", age_match.group(0)) if age_match else ""
        title_parts = [f"✈️ [货号{html.escape(sku)}] Telegram飞机"]
        if region:
            title_parts.append(html.escape(region))
        if age:
            title_parts.append(html.escape(age))
        lines = ["<b>✈️ 商品详情</b>", " | ".join(title_parts),
                 f"💰 <b>售价：</b>¥{price:.2f}", f"📦 <b>库存：</b>{stock}",
                 f"🆔 <b>货号：</b>{html.escape(str(supplier_id))}"]
        notes = []
        if re.search(r"(?i)TDATA", raw_name + " " + description):
            notes.extend(["• 格式：TDATA + SESSION + JSON", "• 限制：必须使用 Windows 电脑端登录，切勿手机直接登"])
        if re.search(r"(?i)3\s*小时", description):
            notes.append("• 售后：下单后 3 小时内有效")
        remark_match = re.search(r"(?im)^.*(?:备注|2FA|2fa\.txt).*$", description)
        if remark_match:
            notes.append("• 备注：" + html.escape(remark_match.group(0).strip()))
        if notes:
            lines.extend(["", "📝 <b>核心说明：</b>", *notes])
        reminders = []
        if re.search(r"注册\s*\d+\s*天", raw_name):
            reminders.append("• 账号注册时间以商品标题标注为准。")
        if reminders:
            lines.extend(["", "⚠️ <b>重要提醒：</b>", *reminders])
        video_urls = re.findall(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=[\w-]+|youtu\.be/[\w-]+)(?:[^\s<>]*)", description, flags=re.I)
        if not video_urls:
            video_urls = [str(supplier_detail.get(k)).strip() for k in ("VideoUrl", "VideoURL", "TutorialUrl", "TutorialURL") if isinstance(supplier_detail.get(k), str) and supplier_detail.get(k).strip()]
        if video_urls:
            video_url = html.escape(video_urls[0], quote=True)
            lines.extend(["", f"🔗 <b>视频教程：</b><a href=\"{video_url}\">点击观看</a>"])
        lines.extend(["", "<i>💡 数字商品交付后，除未按约交付或与描述不符外，不支持退换。请核对地区、年限及登录方式后再下单。</i>"])
    else:
        lines = ["<b>🛍️ 商品详情</b>", safe_name,
                 f"💰 <b>售价：</b>¥{price:.2f}", f"📦 <b>库存：</b>{stock}",
                 f"🆔 <b>货号：</b>{html.escape(str(supplier_id))}"]
        if description:
            lines.extend(["", "📝 <b>核心说明：</b>", html.escape(description[:1200])])
        lines.extend(["", "<i>💡 数字商品交付后，除未按约交付或与描述不符外，不支持退换。请核对商品规格后再下单。</i>"])

    if not verified:
        lines.extend(["", "⚠️ 当前暂不可购买，请联系客服。"])
    detail = "\n".join(lines)
    kb = _build_detail_payment_keyboard(prod_id, back_cat, return_page)
    return detail, kb


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def buy_item(call):
    # Product IDs may themselves contain underscores; only the final segment is the page.
    payload = call.data[4:]
    prod_id, separator, page_text = payload.rpartition("_")
    if not separator:
        prod_id, page_text = payload, "0"
    try:
        return_page = max(0, int(page_text))
    except ValueError:
        return_page = 0
    all_products = get_all_products()
    p = next((x for x in all_products if x["id"] == prod_id), None)
    if not p:
        bot.answer_callback_query(call.id, "商品不存在或已下架，请刷新商品列表。", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    if p.get("is_local_inventory"):
        from products.local_inventory import stock_count, get_email_domains
        available = stock_count()
        domains = get_email_domains(3)
        suffix_text = ", ".join(domains) if domains else "暂无可售邮箱"
        detail = (
            "📧 <b>俄罗斯邮箱账号（单个）</b>\n"
            "💰 本店售价：<code>¥2.00</code>\n"
            f"📦 本地现货：<code>{available}</code> 个\n"
            f"📧 包含后缀：{html.escape(suffix_text)}\n"
            "⚡ 余额支付成功后，系统自动从本地库存取出 1 个邮箱地址私聊发货。\n"
        )
        kb = _build_detail_payment_keyboard(prod_id, "russianmail", return_page)
        safe_edit(bot, call.message.chat.id, call.message.message_id, detail, kb)
        return

    source_category = str(p.get("category_key", "up0"))
    source_meta = UPSTREAM_CATEGORY_BY_KEY.get(source_category, {})
    source_alias = str(source_meta.get("alias") or "").strip().lower()
    back_cat = "up_tg" if source_alias in {"tgapi", "telegram"} else source_category
    verified = False
    supplier_detail = {}
    if p.get("is_upstream"):
        try:
            preflight_live_supplier_product(p)
            verified = True
            from supplier_client import DahaoshangClient
            supplier_detail = DahaoshangClient().get_product(p.get("supplier_product_id")) or {}
        except Exception as exc:
            print("上游商品详情实时核验失败：", type(exc).__name__, flush=True)
            _notify_admin_critical("大号商商品详情核验失败", exc, str(p.get("id") or "unknown product"))
    else:
        verified = p.get("supplier_verified") is True and p.get("procurement_ready") is True
    detail, kb = _build_product_detail_view(p, prod_id, back_cat, return_page, verified, supplier_detail)
    safe_edit(bot, call.message.chat.id, call.message.message_id, detail, kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_") and not call.data.startswith(("pay_tron_", "pay_alipay_", "pay_balance_")))
def pay_item(call):
    # Legacy second-step payment menu is intentionally retired; payment choices
    # are now shown directly on the product detail page.
    bot.answer_callback_query(call.id, "支付入口已更新，请重新打开商品详情页。", show_alert=True)


_balance_purchase_lock = threading.RLock()


@contextmanager
def _balance_purchase_process_lock():
    """Serialize wallet debit + order archival across threads and bot processes."""
    lock_dir = "/root/fakabot/data"
    os.makedirs(lock_dir, mode=0o700, exist_ok=True)
    lock_path = os.path.join(lock_dir, "balance_purchase.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        os.chmod(lock_path, 0o600)
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    error = TimeoutError("余额购买文件锁等待超过 10 秒")
                    _notify_admin_critical("购买文件锁超时", error, lock_path)
                    raise error
                time.sleep(0.1)
        yield
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _pay_local_russian_email(call, product):
    """Debit wallet and deliver exactly one locally reserved credential; no supplier calls."""
    from decimal import Decimal
    from products.local_inventory import reserve_one, finalize_reservation, release_reservation
    uid = str(call.from_user.id)
    order_id = "LM" + __import__("uuid").uuid4().hex[:12].upper()
    price = Decimal("0.2500")  # ¥2.00 at the shop's fixed 8 CNY / USDT rate
    amount_cny = Decimal("2.00")
    with _balance_purchase_lock, _balance_purchase_process_lock():
        users_before = load_json_file(USERS_FILE, {})
        orders_before = load_json_file(ORDERS_FILE, [])
        users = copy.deepcopy(users_before)
        orders = copy.deepcopy(orders_before)
        user = users.get(uid)
        if not isinstance(user, dict):
            bot.send_message(call.message.chat.id, "账户资料不存在，请先进入用户中心。")
            return
        callback_key = str(call.id)
        if any(isinstance(row, dict) and row.get("balance_purchase_callback_id") == callback_key for row in orders):
            bot.send_message(call.message.chat.id, "该次余额支付已受理，请到「我的订单」查看状态。")
            return
        cutoff = time.time() - 90
        for row in orders:
            if (isinstance(row, dict) and str(row.get("user_id")) == uid
                    and row.get("product_id") == "russianmail_single"
                    and row.get("payment_method") == "internal_balance"
                    and row.get("payment_status") in ("paid", "completed", "delivered")):
                try:
                    created = datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if created >= cutoff:
                    bot.send_message(call.message.chat.id, "这件商品刚刚已有订单受理，请先查看订单状态，避免重复下单。")
                    return
        balance = Decimal(str(user.get("balance") or "0"))
        if balance < price:
            bot.send_message(call.message.chat.id,
                f"❌ 余额不足，未扣款。\n商品售价：¥2.00（0.2500 USDT）\n"
                f"当前余额：¥{price_cny(balance):.2f}（{balance:.4f} USDT）\n"
                f"还需充值：¥{price_cny(price - balance):.2f}")
            return
        card_content = reserve_one(order_id)
        if not card_content:
            bot.send_message(call.message.chat.id, "❌ 本地库存暂时不足，未扣余额。")
            return
        now = datetime.now().astimezone().isoformat()
        user["balance"] = float(balance - price)
        user.setdefault("purchase_ledger", []).append({
            "order_id": order_id, "type": "purchase_debit",
            "amount_usdt": "-0.2500", "amount_cny": "-2.00",
            "created_at": now, "payment_method": "internal_balance",
        })
        orders.append({
            "order_id": order_id, "user_id": uid,
            "product_id": "russianmail_single",
            "product_name": "俄罗斯邮箱账号（单个）",
            "payment_method": "internal_balance",
            "amount": 2.0, "amount_cny": "2.00", "amount_usdt": "0.2500",
            "exchange_rate": "8", "created_at": now,
            "paid_at": now, "delivered_at": now,
            "payment_status": "paid", "status": "delivered",
            "procurement_status": "delivered",
            "payment_verification": "local_inventory_atomic_debit",
            "balance_purchase_callback_id": callback_key,
            "delivery_content": card_content,
            "delivery_source": "local_russian_email_inventory",
        })
        user["orders_count"] = _paid_order_count_for_user(uid, orders)
        try:
            if ARCHIVE_STORE is None:
                raise RuntimeError("Telegram 归档存储未初始化")
            ARCHIVE_STORE.save_many({"users": users, "orders": orders})
            ARCHIVE_STORE.flush("俄罗斯邮箱本地库存余额扣款与订单归档")
        except Exception as exc:
            release_reservation(order_id)
            try:
                if ARCHIVE_STORE is not None:
                    ARCHIVE_STORE.save_many({"users": users_before, "orders": orders_before})
                    ARCHIVE_STORE.flush("俄罗斯邮箱本地库存购买失败回滚")
            except Exception:
                pass
            bot.send_message(call.message.chat.id, "❌ 订单归档失败，未完成扣款；库存已释放，请稍后重试。")
            print("本地俄罗斯邮箱订单归档失败：", type(exc).__name__, flush=True)
            return
        if not finalize_reservation(order_id):
            print("本地邮箱库存已预留但完成标记失败：", order_id, flush=True)
    try:
        bot.send_message(
            uid,
            "✅ <b>购买成功，已自动发货</b>\n"
            f"商品：📧 俄罗斯邮箱账号（单个）\n订单号：<code>{order_id}</code>\n"
            "实付：<b>¥2.00</b>\n\n"
            f"<b>账号卡密：</b>\n<pre>{html.escape(card_content)}</pre>\n"
            "请及时保存账号信息。需要帮助请联系 @qingtian_Boss",
            parse_mode="HTML",
        )
    except Exception as exc:
        print("本地俄罗斯邮箱私聊发货失败：", order_id, type(exc).__name__, flush=True)
        bot.send_message(call.message.chat.id, f"订单 {order_id} 已扣款并锁定卡密，但私聊发送失败，请联系客服补发；请勿重复购买。")
        return
    bot.send_message(call.message.chat.id, f"✅ 购买成功！订单号：{order_id}，卡密已私聊发送。")
    bot.answer_callback_query(call.id, "购买成功，已发货。")


def _customer_payment_block_reason(product):
    """Return a plain customer-facing reason when this item cannot accept payment."""
    if product.get("is_local_inventory"):
        return ""
    if not product.get("is_upstream"):
        if product.get("supplier_verified") is not True or product.get("procurement_ready") is not True:
            return "该商品暂不可购买，请联系客服。"
    stock = product.get("supplier_stock")
    if stock is not None:
        try:
            if int(stock) <= 0:
                return "抱歉，该商品暂时缺货，请选择其他商品。"
        except (TypeError, ValueError):
            return "商品库存暂时无法确认，请稍后重试。"
    return ""


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_balance_"))
def pay_balance_item(call):
    """Debit the customer's USDT wallet once, then use the production procurement/delivery path."""
    prod_id = call.data[len("pay_balance_"):]
    uid = str(call.from_user.id)
    product = next((x for x in get_all_products() if str(x.get("id")) == prod_id), None)
    if not product:
        bot.answer_callback_query(call.id, "商品不存在或已下架，请刷新商品列表。", show_alert=True)
        return
    if product.get("is_local_inventory"):
        _pay_local_russian_email(call, product)
        return
    try:
        preflight_live_supplier_product(product)
        blocked_reason = _customer_payment_block_reason(product)
        if blocked_reason:
            bot.answer_callback_query(call.id, blocked_reason, show_alert=True)
            return
        from decimal import Decimal
        cost = Decimal(str(product.get("supplier_price_cny"))).quantize(Decimal("0.01"))
        price = Decimal(str(product.get("price"))).quantize(Decimal("0.0001"))
        expected_price = (cost / Decimal("4")).quantize(Decimal("0.0001"))
        if price != expected_price:
            raise PaymentConfigurationError("商品售价与上游实时成本不匹配，已停止扣款")
        amount_cny = price_cny(price)
    except Exception as exc:
        bot.answer_callback_query(call.id, "商品信息核验失败，未扣余额。", show_alert=True)
        print("余额支付预检失败：", type(exc).__name__, flush=True)
        return

    bot.answer_callback_query(call.id, "正在校验余额并创建订单…")
    with _balance_purchase_lock, _balance_purchase_process_lock():
        users_before = load_json_file(USERS_FILE, {})
        orders_before = load_json_file(ORDERS_FILE, [])
        users = copy.deepcopy(users_before)
        orders = copy.deepcopy(orders_before)
        user = users.get(uid)
        if not isinstance(user, dict):
            bot.send_message(call.message.chat.id, "账户资料不存在，请先进入用户中心。")
            return
        from decimal import Decimal
        balance = Decimal(str(user.get("balance") or "0"))
        if balance < price:
            current_cny = price_cny(balance)
            bot.send_message(
                call.message.chat.id,
                "❌ 余额不足，未扣款。\n"
                f"商品售价：¥{amount_cny:.2f}（{price:.4f} USDT）\n"
                f"当前余额：¥{current_cny:.2f}（{balance:.4f} USDT）\n"
                f"还需充值：¥{price_cny(price - balance):.2f}"
            )
            return
        # A Telegram callback may be delivered more than once. Refuse a second debit
        # for the same callback update ID.
        callback_key = str(call.id)
        if any(isinstance(row, dict) and row.get("balance_purchase_callback_id") == callback_key
               for row in orders):
            bot.send_message(call.message.chat.id, "该次余额支付已受理，请到「我的订单」查看状态。")
            return
        # Suppress rapid duplicate taps that arrive as separate Telegram callback IDs.
        recent_cutoff = time.time() - 90
        for row in orders:
            if not isinstance(row, dict) or str(row.get("user_id")) != uid:
                continue
            if str(row.get("product_id")) != str(product.get("id") or prod_id):
                continue
            if row.get("payment_method") != "internal_balance":
                continue
            if row.get("payment_status") not in ("paid", "completed", "delivered"):
                continue
            procurement_state = str(row.get("procurement_status") or "")
            if procurement_state in ("procurement_processing", "supplier_order_pending_delivery", "needs_manual_reconciliation"):
                bot.send_message(call.message.chat.id, "这件商品已有订单正在处理中，请先联系客服处理，系统已阻止重复扣款。")
                return
            try:
                created = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if created >= recent_cutoff:
                bot.send_message(call.message.chat.id, "这件商品刚刚已有余额订单受理，请先查看订单状态，避免重复下单。")
                return
        order_id = "WB" + __import__("uuid").uuid4().hex[:12].upper()
        now = datetime.now().astimezone().isoformat()
        order = {
            "order_id": order_id,
            "user_id": uid,
            "product_id": str(product.get("id") or prod_id),
            "product_name": str(product.get("name") or "商品"),
            "supplier_product_id": str(product.get("supplier_product_id") or ""),
            "supplier_category_id": str(product.get("category_id") or ""),
            "supplier_model_id": str(product.get("supplier_model_id") or ""),
            "supplier_email": str(product.get("supplier_email") or ""),
            "supplier_quantity": int(product.get("supplier_quantity") or 1),
            "supplier_price_cny": format(cost, ".2f"),
            "supplier_verified": True,
            "procurement_ready": True,
            "payment_method": "internal_balance",
            "amount": float(amount_cny),
            "amount_cny": format(amount_cny, ".2f"),
            "amount_usdt": format(price, ".4f"),
            "exchange_rate": "8",
            "created_at": now,
            "payment_status": "pending",
            "status": "pending_payment",
            "procurement_status": "awaiting_balance_debit",
            "payment_verification": "internal_balance_pending_debit",
            "balance_purchase_callback_id": callback_key,
        }
        orders.append(order)
        try:
            if ARCHIVE_STORE is None:
                raise RuntimeError("Telegram 归档存储未初始化")
            # Phase 1: write the order record first, with no balance deduction.
            ARCHIVE_STORE.save_many({"users": users_before, "orders": orders})
            ARCHIVE_STORE.flush("余额购买第一阶段：订单先行持久化")
        except Exception as exc:
            if ARCHIVE_STORE is not None:
                try:
                    ARCHIVE_STORE.save_many({"users": users_before, "orders": orders_before})
                    ARCHIVE_STORE.flush("余额购买第一阶段失败回滚")
                except Exception:
                    pass
            bot.send_message(call.message.chat.id, "❌ 订单预归档失败，余额未扣除；请稍后重试。")
            print("余额支付预归档失败：", type(exc).__name__, flush=True)
            return

        try:
            # Phase 2: only after the order snapshot is confirmed, debit the wallet
            # and append its ledger entry in the same durable snapshot.
            user["balance"] = float(balance - price)
            user.setdefault("purchase_ledger", []).append({
                "order_id": order_id,
                "type": "purchase_debit",
                "amount_usdt": format(-price, ".4f"),
                "amount_cny": format(-amount_cny, ".2f"),
                "created_at": now,
                "payment_method": "internal_balance",
            })
            order["payment_verification"] = "internal_balance_atomic_debit"
            order["procurement_status"] = "awaiting_payment_confirmation"
            ARCHIVE_STORE.save_many({"users": users, "orders": orders})
            ARCHIVE_STORE.flush("余额购买第二阶段：扣款流水与订单原子持久化")
        except Exception as exc:
            # Never proceed to supplier procurement unless both debit and order commit.
            try:
                ARCHIVE_STORE.save_many({"users": users_before, "orders": orders_before})
                ARCHIVE_STORE.flush("余额购买第二阶段失败回滚")
            except Exception as rollback_exc:
                print("余额购买回滚持久化失败，保留预归档待人工核对：", type(rollback_exc).__name__, flush=True)
            bot.send_message(call.message.chat.id, "❌ 扣款流水归档失败，未提交采购；请联系客服核对账户状态。")
            print("余额支付扣款归档失败：", type(exc).__name__, flush=True)
            return

    # This is the existing production payment handler. It invokes buy_with_balance
    # exactly once, stores the upstream order ID/content, and notifies the buyer.
    try:
        handled = handle_confirmed_payment(
            bot, order_id,
            {"txid": "INTERNAL-BALANCE-" + order_id,
             "provider": "internal_balance",
             "manual_verification": "internal_balance_atomic_debit"},
            load_orders=lambda: load_json_file(ORDERS_FILE, []),
            save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
            admin_id=ADMIN_ID,
            on_order_paid=lambda row: _reconcile_user_order_count(_order_user_id(row)),
        )
        if ARCHIVE_STORE is not None:
            ARCHIVE_STORE.flush("本店余额订单上游采购处理")
        if not handled:
            print("余额订单已扣款但生产支付处理器未接单：", order_id, flush=True)
            bot.send_message(uid, f"⚠️ 余额订单 {order_id} 已扣款，采购处理器未接单；请勿重复购买，客服正在核对。")
    except Exception as exc:
        print("余额订单采购处理异常：", order_id, type(exc).__name__, flush=True)
        bot.send_message(uid, f"⚠️ 订单 {order_id} 已扣款，发货状态正在核对；请勿重复下单。")

def preflight_live_supplier_product(product):
    """Verify the exact live upstream SKU before accepting customer funds."""
    if not product.get("is_upstream"):
        return
    from supplier_client import DahaoshangClient
    checked = DahaoshangClient().preflight_purchase(
        product.get("supplier_product_id"),
        product.get("supplier_price_cny"),
        quantity=int(product.get("supplier_quantity") or 1),
        model_id=product.get("supplier_model_id") or None,
        email=product.get("supplier_email") or "",
    )
    product.update(checked)
    product["supplier_url"] = str(product.get("supplier_url") or (
        "https://dahaoshang.com/#/product/" + str(product.get("supplier_product_id"))
    ))


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_tron_"))
def pay_tron_item(call):
    prod_id = call.data[len("pay_tron_"):]
    product = next((x for x in get_all_products() if str(x.get("id")) == prod_id), None)
    if product and product.get("is_local_inventory"):
        bot.answer_callback_query(call.id, "俄罗斯邮箱目前仅支持余额支付，支付宝和 USDT 暂不可用。", show_alert=True)
        return
    if not product:
        bot.answer_callback_query(call.id, "商品不存在或已下架，请刷新商品列表。", show_alert=True)
        return
    if not product.get("is_upstream"):
        bot.answer_callback_query(call.id, _customer_payment_block_reason(product) or "该商品暂不可购买，请联系客服。", show_alert=True)
        return
    try:
        preflight_live_supplier_product(product)
        blocked_reason = _customer_payment_block_reason(product)
        if blocked_reason:
            bot.answer_callback_query(call.id, blocked_reason, show_alert=True)
            return
    except Exception as exc:
        bot.answer_callback_query(call.id, "商品信息暂时无法确认，未创建支付订单，请稍后重试。", show_alert=True)
        print("付款前商品核验失败：", type(exc).__name__, flush=True)
        return
    if PAYMENT_WATCHER is None:
        bot.answer_callback_query(call.id, "链上收款暂未配置，请联系客服。", show_alert=True)
        return
    try:
        order, payment_text = create_payment_order(
            user_id=call.from_user.id, product=product,
            load_orders=lambda: load_json_file(ORDERS_FILE, []),
            save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
        )
    except PaymentConfigurationError as exc:
        bot.answer_callback_query(call.id, str(exc), show_alert=True)
        return
    except Exception as exc:
        bot.answer_callback_query(call.id, "订单创建失败，请稍后重试或联系客服。", show_alert=True)
        print("创建支付订单失败：", repr(exc), flush=True)
        return
    bot.answer_callback_query(call.id, "订单已创建")
    bot.send_photo(call.message.chat.id, photo=create_payment_qr(os.getenv("TRON_RECEIVE_ADDRESS", "TTT5MV8xeZqKaPDxUcDjR8bPFz2ce5kmBr").strip()), caption=payment_text, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_alipay_"))
def pay_alipay_item(call):
    prod_id = call.data[len("pay_alipay_"):]
    product = next((x for x in get_all_products() if str(x.get("id")) == prod_id), None)
    if product and product.get("is_local_inventory"):
        bot.answer_callback_query(call.id, "俄罗斯邮箱目前仅支持余额支付，支付宝和 USDT 暂不可用。", show_alert=True)
        return
    if not product:
        bot.answer_callback_query(call.id, "商品不存在或已下架，请刷新商品列表。", show_alert=True)
        return
    if not product.get("is_upstream"):
        bot.answer_callback_query(call.id, _customer_payment_block_reason(product) or "该商品暂不可购买，请联系客服。", show_alert=True)
        return
    try:
        preflight_live_supplier_product(product)
        blocked_reason = _customer_payment_block_reason(product)
        if blocked_reason:
            bot.answer_callback_query(call.id, blocked_reason, show_alert=True)
            return
    except Exception as exc:
        bot.answer_callback_query(call.id, "商品信息暂时无法确认，未创建支付订单，请稍后重试。", show_alert=True)
        print("支付宝付款前商品核验失败：", type(exc).__name__, flush=True)
        return
    qr_file = os.getenv("ALIPAY_PERSONAL_QR_FILE", "/root/fakabot/alipay_receive_qr.png").strip()
    if not os.path.isfile(qr_file):
        bot.answer_callback_query(call.id, "支付宝收款方式暂不可用，请联系客服。", show_alert=True)
        return

    order = None
    try:
        order, payment_text = create_alipay_order(
            user_id=call.from_user.id,
            product=product,
            load_orders=lambda: load_json_file(ORDERS_FILE, []),
            save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
            mode_override="stateless_gateway",
        )
        gateway_order = create_gateway_order(order["amount_cny"], order["order_id"])
        orders = load_json_file(ORDERS_FILE, [])
        target = next((row for row in orders if isinstance(row, dict)
                       and str(row.get("order_id")) == str(order["order_id"])), None)
        if target is None:
            raise RuntimeError("本地业务订单归档丢失")
        target.update({
            "gateway_order_id": str(gateway_order["order_id"]),
            "gateway_business_id": str(order["order_id"]),
            "gateway_create_time": int(gateway_order.get("create_time") or time.time()),
            "gateway_expire_time": int(gateway_order.get("expire_time") or (time.time() + 60)),
            "expires_at_ms": int(gateway_order.get("expire_time") or (time.time() + 60)) * 1000,
            "payment_verification": "stateless_gateway_webhook",
        })
        save_json_file(ORDERS_FILE, orders)
        if ARCHIVE_STORE is not None:
            ARCHIVE_STORE.flush("创建支付宝网关订单")
    except Exception as exc:
        if order is not None:
            try:
                orders = load_json_file(ORDERS_FILE, [])
                target = next((row for row in orders if isinstance(row, dict)
                               and str(row.get("order_id")) == str(order["order_id"])), None)
                if target is not None and target.get("payment_status") == "pending":
                    target.update({"payment_status": "failed", "status": "failed",
                                   "gateway_create_error": type(exc).__name__})
                    save_json_file(ORDERS_FILE, orders)
            except Exception:
                pass
        bot.answer_callback_query(call.id, "支付订单创建失败，请稍后重试或联系客服。", show_alert=True)
        print("创建无状态支付宝网关订单失败：", type(exc).__name__, flush=True)
        return

    bot.answer_callback_query(call.id, "订单已创建，请在 60 秒内付款")
    caption = (
        payment_text + "\n网关订单号：<code>" + html.escape(str(gateway_order["order_id"]))
        + "</code>\n有效期：<b>60 秒</b>。超时未到账将自动关闭订单，请勿向过期订单付款。"
    )
    try:
        with open(qr_file, "rb") as qr:
            bot.send_photo(call.message.chat.id, photo=qr, caption=caption, parse_mode="HTML")
    except Exception as exc:
        print("发送支付宝收款码失败：", type(exc).__name__, flush=True)
        bot.send_message(call.message.chat.id, caption, parse_mode="HTML")
    if ADMIN_ID.isdigit():
        bot.send_message(int(ADMIN_ID),
            "🧾 新支付宝网关订单\n"
            f"业务订单：{order['order_id']}\n网关订单：{gateway_order['order_id']}\n"
            f"用户：{call.from_user.id}\n金额：¥{order['amount_cny']}\n"
            f"商品：{order.get('product_name', '商品')}\n有效期：60 秒")



@bot.message_handler(commands=['alipay_test'])
def alipay_test_command(message):
    """Admin-only 0.01 CNY sandbox callback test; never triggers supplier procurement."""
    if not ADMIN_ID.isdigit() or str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "无权限。")
        return
    if not os.getenv("ALIPAY_APP_ID", "").strip():
        bot.reply_to(message, "支付宝收款验证环境尚未配置，暂不能创建验证收银台。")
        return
    from datetime import datetime, timezone
    import uuid
    order_id = "TESTAL" + uuid.uuid4().hex[:12].upper()
    amount = "0.01"
    try:
        from alipay_gateway import build_page_pay_url
        gateway_url = build_page_pay_url(order_id, amount, "晴天商城支付宝收款通知验证（不发货）")
        expires_at_ms = int((time.time() + 1200) * 1000)
        payment_url = build_checkout_url(order_id, gateway_url, expires_at_ms)
        order = {
            "order_id": order_id, "user_id": str(message.from_user.id),
            "product_id": "sandbox_callback_test", "product_name": "支付宝收款通知验证（不发货）",
            "payment_method": "alipay_gateway", "payment_url": payment_url, "gateway_url": gateway_url,
            "amount": 0.01, "amount_cny": amount, "amount_usdt": "0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at_ms": expires_at_ms,
            "payment_status": "pending", "status": "pending_payment",
            "procurement_status": "sandbox_test_no_delivery",
            "payment_verification": "provider_notification_required",
            "supplier_verified": False, "procurement_ready": False,
        }
        orders = load_json_file(ORDERS_FILE, [])
        if not isinstance(orders, list):
            raise RuntimeError("订单归档格式错误")
        orders.append(order)
        save_json_file(ORDERS_FILE, orders)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(text="打开支付宝收款验证页面", url=payment_url))
        bot.send_photo(message.chat.id, photo=create_alipay_payment_qr(payment_url),
            caption=("💳 <b>支付宝收款通知验证</b>\n订单：<code>" + order_id
                + "</code>\n金额：<b>¥0.01</b>\n\n请使用指定验证账户完成付款流程。本订单仅核对收款通知，不会产生商品交付。"),
            parse_mode="HTML", reply_markup=kb)
    except Exception as exc:
        bot.reply_to(message, "收款验证二维码生成失败，请检查收款服务配置。")
        print("支付宝收款验证单创建失败：", type(exc).__name__, flush=True)

@bot.message_handler(commands=['confirm_alipay'])
def confirm_alipay_command(message):
    """Admin confirms an actual personal-Alipay receipt; never auto-trust screenshots."""
    if not ADMIN_ID.isdigit() or str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "无权限。")
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        bot.reply_to(message, "用法：/confirm_alipay 订单号\n请先在自己的支付宝账单中核实金额与订单一致，再执行此命令。")
        return
    order_id = parts[1].strip()
    orders = load_json_file(ORDERS_FILE, [])
    target = next((x for x in orders if isinstance(x, dict) and str(x.get("order_id")) == order_id), None)
    if not target or target.get("payment_method") != "alipay_qr":
        bot.reply_to(message, "订单不存在，或不是支付宝订单。")
        return
    if target.get("payment_status") != "pending" or target.get("payment_verification") != "manual_review_required":
        bot.reply_to(message, "该订单不是待核账状态，或已经处理；不会重复采购。")
        return
    target["payment_verification"] = "admin_confirmed"
    target["alipay_confirmed_by"] = str(message.from_user.id)
    target["alipay_confirmed_at"] = datetime.now().astimezone().isoformat()
    save_json_file(ORDERS_FILE, orders)
    handled = handle_confirmed_payment(
        bot, order_id,
        {"txid": "ALIPAY-MANUAL-" + order_id, "provider": "alipay_manual", "manual_verification": "admin_confirmed"},
        load_orders=lambda: load_json_file(ORDERS_FILE, []),
        save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
        admin_id=ADMIN_ID,
        on_order_paid=lambda row: _reconcile_user_order_count(_order_user_id(row)),
    )
    if ARCHIVE_STORE is not None:
        ARCHIVE_STORE.flush("人工确认付款后的用户累计订单计数")
    bot.reply_to(message, "✅ 已登记人工到账核验并提交自动采购处理。请查看订单状态与机器人告警。" if handled else "订单状态已变化，未重复采购。")


@bot.message_handler(commands=['setalipayqr'])
def set_alipay_qr_command(message):
    if not ADMIN_ID.isdigit() or str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "无权限。"); return
    ALIPAY_QR_UPLOAD_PENDING.add(str(message.from_user.id))
    bot.reply_to(message, "请直接发送支付宝收款二维码图片。请勿发送密码、验证码或登录凭据。")

@bot.message_handler(commands=['setbotavatar'])
def set_bot_avatar_command(message):
    uid = str(message.from_user.id)
    if not ADMIN_ID.isdigit() or uid != ADMIN_ID:
        bot.reply_to(message, "无权限。")
        return
    BOT_PROFILE_PHOTO_UPLOAD_PENDING.add(uid)
    bot.reply_to(message, "请把机器人头像图片作为照片重新发送一次；收到后我会直接更新机器人头像。")


@bot.message_handler(content_types=['photo'])
def receive_alipay_qr_photo(message):
    uid = str(message.from_user.id)
    if uid == ADMIN_ID and uid in BOT_PROFILE_PHOTO_UPLOAD_PENDING:
        try:
            from io import BytesIO
            info = bot.get_file(message.photo[-1].file_id)
            data = bot.download_file(info.file_path)
            tmp = BOT_PROFILE_PHOTO_FILE + ".tmp"
            with open(tmp, "wb") as out: out.write(data)
            os.chmod(tmp, 0o600); os.replace(tmp, BOT_PROFILE_PHOTO_FILE)
            uploaded = telebot.types.InputFile(BytesIO(data), file_name="bot-avatar.jpg")
            profile_photo = telebot.types.InputProfilePhotoStatic(uploaded)
            if not bot.set_my_profile_photo(profile_photo):
                raise RuntimeError("Telegram did not confirm profile photo update")
            BOT_PROFILE_PHOTO_UPLOAD_PENDING.discard(uid)
            bot.reply_to(message, "✅ 机器人头像已更新。")
        except Exception as exc:
            print("更新机器人头像失败：", repr(exc), flush=True)
            bot.reply_to(message, "头像更新失败，请重新执行 /setbotavatar 后再发送图片。")
        return
    if uid != ADMIN_ID:
        return
    try:
        qr_file_id = message.photo[-1].file_id
        env_path = "/root/fakabot/.env"
        with open(env_path, "r", encoding="utf-8") as env_file:
            lines = env_file.read().splitlines()
        key = "ALIPAY_PERSONAL_QR_FILE_ID"
        replacement = f"{key}={qr_file_id}"
        found = False
        updated = []
        for line in lines:
            if line.startswith(key + "="):
                if not found:
                    updated.append(replacement)
                    found = True
            else:
                updated.append(line)
        if not found:
            updated.append(replacement)
        tmp = env_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as env_file:
            env_file.write("\n".join(updated) + "\n")
            env_file.flush()
            os.fsync(env_file.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
        os.environ[key] = qr_file_id
        ALIPAY_QR_UPLOAD_PENDING.discard(uid)
        bot.reply_to(message, "✅ 已登记这张支付宝收款码的 Telegram file_id，并写入服务器 .env 配置。充值页面将直接发送该 file_id，不再读取本地图片。")
    except Exception as exc:
        print("登记支付宝二维码 file_id 失败：", repr(exc), flush=True)
        bot.reply_to(message, "收款码登记失败，请重新发送 JPG 图片。")

ORDERS_PAGE_SIZE = 4
ORDERS_TEST_KEYWORDS = ("测试", "沙箱", "联调", "test", "sandbox", "mock")


def _orders_is_visible_to_customer(item):
    if not isinstance(item, dict):
        return False
    product_name = " ".join(str(item.get(key) or "") for key in (
        "product_name", "product_title", "item_name", "product"
    )).casefold()
    if any(word.casefold() in product_name for word in ORDERS_TEST_KEYWORDS):
        return False
    # Also hide records explicitly marked as test/sandbox/mock, even if the
    # product title itself looks ordinary.
    metadata = " ".join(str(item.get(key) or "") for key in (
        "payment_verification", "payment_method", "remark", "remarks",
        "note", "notes", "memo", "description", "order_type"
    )).casefold()
    if any(word.casefold() in metadata for word in ORDERS_TEST_KEYWORDS):
        return False
    return not (item.get("sandbox_test_order") is True or item.get("upstream_mock_mode") is True)


def _orders_page_keyboard(page, total_pages):
    kb = types.InlineKeyboardMarkup(row_width=2)
    previous_page = max(0, int(page) - 1)
    next_page = min(max(0, int(total_pages) - 1), int(page) + 1)
    kb.row(
        types.InlineKeyboardButton("⬅️ 上一页", callback_data=f"ui_orders_page_{previous_page}"),
        types.InlineKeyboardButton("下一页 ➡️", callback_data=f"ui_orders_page_{next_page}"),
    )
    kb.row(
        types.InlineKeyboardButton("👤 用户中心", callback_data="ui_recharge"),
        types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
    )
    return kb


def _render_orders_page(call, page=0):
    all_orders = load_json_file(ORDERS_FILE, [])
    uid = str(call.from_user.id)
    mine = [item for item in all_orders if isinstance(item, dict)
            and _order_user_id(item) == uid and _orders_is_visible_to_customer(item)] if isinstance(all_orders, list) else []
    mine.sort(key=lambda item: str(item.get("created_at") or item.get("paid_at") or item.get("paid_time") or ""), reverse=True)
    total = len(mine)
    total_pages = max(1, (total + ORDERS_PAGE_SIZE - 1) // ORDERS_PAGE_SIZE)
    page = min(max(0, int(page)), total_pages - 1)
    current = mine[page * ORDERS_PAGE_SIZE:(page + 1) * ORDERS_PAGE_SIZE]
    status_labels = {
        "paid": "已支付", "success": "已支付", "confirmed": "已支付",
        "gateway_confirmed": "已支付", "completed": "已完成", "delivered": "已交付",
        "pending": "待支付", "pending_payment": "待支付",
        "expired": "已过期", "failed": "失败", "cancelled": "已取消"
    }
    lines = ["📦 <b>我的订单</b>", f"共 <b>{total}</b> 笔订单 · 第 <b>{page + 1}/{total_pages}</b> 页", ""]
    for item in current:
        order_id = html.escape(str(item.get("order_id") or item.get("id") or "—"), quote=False)
        product = html.escape(str(item.get("product_name") or item.get("product_title") or item.get("item_name") or item.get("product") or "商品"), quote=False)
        amount = _ledger_amount_cny(item)
        procurement_status = str(item.get("procurement_status") or "").lower()
        order_status = str(item.get("status") or "").lower()
        if procurement_status == "delivered" or order_status == "delivered" or item.get("delivered_at"):
            status = "已发货"
        else:
            raw_status = str(item.get("payment_status") or item.get("status") or "unknown").lower()
            status = status_labels.get(raw_status, html.escape(raw_status, quote=False))
        created = html.escape(format_local_time(item.get("created_at") or item.get("paid_at") or item.get("paid_time")), quote=False)
        lines.extend([
            f"<b>📦 {product}</b>",
            f"🔢 单号：<code>{order_id}</code>",
            f"💰 金额：¥{amount:.2f}",
            f"📌 状态：{status}",
            f"🕒 时间：{created}",
            ""
        ])
    if not current:
        lines = ["📦 <b>我的订单</b>", "目前还没有可显示的订单记录。"]
    rendered = "\n".join(lines).rstrip()
    markup = _orders_page_keyboard(page, total_pages)
    # Keep the authored compact HTML and exact two-row keyboard intact.
    try:
        _original_edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=rendered,
            parse_mode="HTML",
            reply_markup=markup
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            print("❌ 我的订单页面更新失败：", repr(exc), flush=True)


@bot.callback_query_handler(func=lambda call: call.data == "ui_orders")
def show_orders(call):
    bot.answer_callback_query(call.id)
    _render_orders_page(call, 0)


@bot.callback_query_handler(func=lambda call: call.data.startswith("ui_orders_page_"))
def show_orders_page(call):
    bot.answer_callback_query(call.id)
    try:
        page = int(call.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        page = 0
    _render_orders_page(call, page)

LEDGER_PAGE_SIZE = 4
LEDGER_TEST_KEYWORDS = ("测试", "联调", "沙箱", "不发货", "不采购")
LEDGER_PAID_STATUSES = {"paid", "success", "completed", "confirmed", "delivered", "gateway_confirmed"}


def _ledger_is_real_paid_order(item):
    """账单只列真实付款订单；明确标记的测试/沙箱/联调订单始终排除。"""
    if not isinstance(item, dict):
        return False
    if item.get("sandbox_test_order") is True or item.get("upstream_mock_mode") is True:
        return False
    verification = str(item.get("payment_verification") or "").lower()
    payment_method = str(item.get("payment_method") or "").lower()
    if any(word in verification for word in ("test", "sandbox", "mock")) or payment_method in ("alipay_mock", "sandbox"):
        return False
    searchable = " ".join(str(item.get(key) or "") for key in (
        "product_name", "product_title", "item_name", "product", "remark", "remarks",
        "note", "notes", "memo", "description"
    )).lower()
    if any(word in searchable for word in LEDGER_TEST_KEYWORDS):
        return False
    payment_status = str(item.get("payment_status") or "").lower()
    status = str(item.get("status") or "").lower()
    return payment_status in LEDGER_PAID_STATUSES or status in LEDGER_PAID_STATUSES or bool(
        item.get("paid_at") or item.get("paid_time") or item.get("confirmed_at")
    )


def _ledger_amount_cny(item):
    """优先使用支付记录中的人民币金额；USDT 金额按项目汇率换算。"""
    for key in ("gateway_paid_amount", "amount_cny"):
        try:
            value = float(item.get(key))
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    usdt_value = item.get("amount_usdt") or item.get("amount") or item.get("price") or item.get("total")
    try:
        return float(price_cny(usdt_value))
    except Exception:
        return 0.0


def _ledger_keyboard(page, total_pages):
    kb = types.InlineKeyboardMarkup(row_width=2)
    previous_page = max(0, page - 1)
    next_page = min(max(0, total_pages - 1), page + 1)
    kb.row(
        types.InlineKeyboardButton("⬅️ 上一页", callback_data=f"ui_ledger_page_{previous_page}"),
        types.InlineKeyboardButton("下一页 ➡️", callback_data=f"ui_ledger_page_{next_page}")
    )
    kb.row(types.InlineKeyboardButton("👤 用户中心", callback_data="ui_recharge"),
           types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"))
    return kb


def _render_ledger(call, page=0):
    uid = str(call.from_user.id)
    all_orders = load_json_file(ORDERS_FILE, [])
    mine = [item for item in all_orders if isinstance(item, dict)
            and _order_user_id(item) == uid and _ledger_is_real_paid_order(item)] if isinstance(all_orders, list) else []
    mine.sort(key=lambda item: str(item.get("paid_at") or item.get("paid_time") or item.get("created_at") or ""), reverse=True)
    total = len(mine)
    total_pages = max(1, (total + LEDGER_PAGE_SIZE - 1) // LEDGER_PAGE_SIZE)
    page = min(max(0, int(page)), total_pages - 1)
    current = mine[page * LEDGER_PAGE_SIZE:(page + 1) * LEDGER_PAGE_SIZE]
    lines = ["🧾 <b>账单明细</b>", f"共 <b>{total}</b> 笔真实付款订单 · 第 <b>{page + 1}/{total_pages}</b> 页", "━━━━━━━━━━━━━━━━━━"]
    status_labels = {
        "paid": "已支付", "success": "已支付", "confirmed": "已支付",
        "gateway_confirmed": "已支付", "completed": "已完成", "delivered": "已交付",
        "expired": "已过期", "failed": "失败"
    }
    for item in current:
        order_id = html.escape(str(item.get("order_id") or item.get("id") or "—"), quote=False)
        product = html.escape(str(item.get("product_name") or item.get("product_title") or item.get("item_name") or item.get("product") or "商品"), quote=False)
        amount = _ledger_amount_cny(item)
        raw_status = str(item.get("payment_status") or item.get("status") or "unknown").lower()
        status = status_labels.get(raw_status, html.escape(raw_status, quote=False))
        created = html.escape(format_local_time(item.get("paid_at") or item.get("paid_time") or item.get("created_at")), quote=False)
        lines.extend([
            f"<b>📦 订单：{product}</b>",
            f"🔢 单号：<code>{order_id}</code>",
            f"💰 金额：¥{amount:.2f}",
            f"📌 状态：{status}",
            f"🕒 时间：{created}",
            ""
        ])
    if not current:
        lines.append("目前暂无真实付款订单记录。")
    safe_edit(bot, call.message.chat.id, call.message.message_id, "\n".join(lines).rstrip(), _ledger_keyboard(page, total_pages))


@bot.callback_query_handler(func=lambda call: call.data == "ui_ledger")
def show_ledger(call):
    bot.answer_callback_query(call.id)
    _render_ledger(call, 0)


@bot.callback_query_handler(func=lambda call: call.data.startswith("ui_ledger_page_"))
def show_ledger_page(call):
    bot.answer_callback_query(call.id)
    try:
        page = int(call.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        page = 0
    _render_ledger(call, page)


SERVICE_GUIDE_PAGES = {
    "guide_precheck": (
        "📋 <b>下单前核对</b>\n"
        "付款前请逐项核对商品页面信息：\n"
        "1. 商品所属平台、国家/地区。\n"
        "2. 账号类型、账号年限或注册时长。\n"
        "3. 是否包含邮箱，以及邮箱是否可用。\n"
        "4. 登录方式、二次验证及恢复方式。\n\n"
        "商品页面未明确说明的内容，不视为商品承诺；规格不清楚时，请先联系客服确认再付款。"
    ),
    "guide_after_sales": (
        "🔄 <b>售后与退换</b>\n"
        "本店销售数字化成品账号/虚拟商品，不是可物流退回的实体商品。账号凭据或数字商品交付后，原则上不支持退货、换货或退款。\n\n"
        "<b>可提交核实的情况</b>\n"
        "如发生未交付、发错货，或交付内容与商品页面明确描述不符，请提供订单号及必要证据，由客服核对订单与交付记录。\n\n"
        "因个人操作、设备环境、地区/IP、违反平台规则、擅自更改安全资料或转售分享导致的问题，不属于可退货理由。\n\n"
        "商品标题中的“售后”“包售后”等字样，不自动构成本店承诺。本店不承诺未明确提供的质保时长、邮箱找回能力、长期稳定性或平台永久可用。客服仅通过店铺公布的官方渠道处理订单问题。本规则是晴天号铺的店铺规则草案。适用法律要求经营者承担的强制责任不受本规则排除。"
    ),
    "guide_payment": (
        "💸 <b>支付与发货</b>\n"
        "1. 打开商品详情，核对货号、售价和商品说明。\n"
        "2. 按订单页面提示完成支付。\n"
        "3. 支付确认后，以机器人私聊发送的订单交付内容为准。\n"
        "4. 如未收到交付内容，请提供订单号联系客服核对。\n\n"
        "收到账号资料后请妥善保存，不要将账号资料转发给他人，也不要把密码、验证码或恢复码发到群聊或公开截图。"
    ),
    "guide_purchase_notes": (
        "📝 <b>购买说明</b>\n"
        "🛡 <b>安全保障</b> 账号购买过程采用安全加密技术保护\n\n"
        "⭐ <b>质量保证</b> 提供高质量社交媒体账号，适合各种营销用途\n\n"
        "🛠 <b>售后支持</b> 账号购买后提供专业的技术支持和售后服务\n\n"
        "💰 <b>价格优惠</b> 批量账号出售享受更多优惠，性价比高"
    ),
    "guide_account_flow": (
        "📱 <b>账号购买流程</b>\n"
        "1. 打开商品分类，选择具体平台。\n"
        "2. 核对国家/地区、账号类型、年限或注册时长。\n"
        "3. 查看邮箱是否包含及可用、登录方式和验证条件；不清楚的规格先联系客服确认。\n"
        "4. 打开商品详情，核对货号、售价和商品说明，再按订单页面提示支付。\n"
        "5. 支付确认后，查看机器人私聊发送的交付内容；未收到时提供订单号联系客服。\n"
        "6. 收到后妥善保存账号资料。首次登录请使用商品说明指定的官方客户端或官网，并按该商品教程操作。\n\n"
        "首次登录避免频繁切换设备、IP/地区或短时间反复尝试密码。除非商品教程明确允许，不要擅自更改邮箱、绑定手机、二次验证或恢复资料。联系售后时请提供订单号、商品货号、问题发生时间和必要截图；截图须遮挡密码、验证码、恢复码及邮箱安全链接。不要将完整账号凭据提交到公共群或非官方表单。"
    ),
}


def build_service_guide_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("📋 下单前核对", callback_data="guide_precheck"),
        types.InlineKeyboardButton("🔄 售后与退换", callback_data="guide_after_sales"),
    )
    kb.row(
        types.InlineKeyboardButton("💸 支付与发货", callback_data="guide_payment"),
        types.InlineKeyboardButton("📝 购买说明", callback_data="guide_purchase_notes"),
    )
    kb.row(
        types.InlineKeyboardButton("📱 账号购买流程", callback_data="guide_account_flow"),
        types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
    )
    return kb


def build_service_guide_detail_keyboard():
    """All service-guide detail pages use exactly one single-column return button."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("💬 联系客服", url="https://t.me/qingtian_Boss"),
        types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
    )
    return kb


def edit_service_guide_message(call, text, keyboard):
    """Render guide pages in HTML without the global keyboard/text alignment rewriter."""
    rendered = premiumize_text(text)
    kwargs = {
        "chat_id": call.message.chat.id,
        "message_id": call.message.message_id,
        "text": rendered,
        "parse_mode": "HTML",
        "reply_markup": keyboard,
    }
    try:
        return _call_with_premium_fallback(_original_edit_message_text, (), kwargs, 0)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return None
        send_kwargs = {
            "chat_id": call.message.chat.id,
            "text": rendered,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }
        return _call_with_premium_fallback(_original_send_message, (), send_kwargs, 1)


@bot.callback_query_handler(func=lambda call: call.data == "ui_help")
def show_help(call):
    bot.answer_callback_query(call.id)
    text = "💡 <b>服务指南 · 使用帮助</b>\n请选择要查看的内容 ↓"
    edit_service_guide_message(call, text, build_service_guide_keyboard())


@bot.callback_query_handler(func=lambda call: call.data in SERVICE_GUIDE_PAGES)
def show_service_guide_detail(call):
    bot.answer_callback_query(call.id)
    edit_service_guide_message(
        call,
        SERVICE_GUIDE_PAGES[call.data],
        build_service_guide_detail_keyboard(),
    )

def render_invite_page(chat_id, message_id, uid):
    user = get_user(uid)
    language = user.get("language", "zh")
    bot_username = bot.get_me().username
    invite_link = f"https://t.me/{bot_username}?start=ref_{uid}"
    safe_link = html.escape(invite_link, quote=True)
    if language == "en":
        text = (
            "⭐ <b>PERMANENT REFERRAL REWARDS</b>\n"
            "Share your personal invitation link and build a lasting referral relationship.\n\n"
            "🤝 <b>Friends spend, you earn 5% for life</b>\n"
            "Friends who join through your personal link will remain permanently linked to you. You will receive a 5% reward on every eligible order they successfully pay for. This referral relationship cannot be transferred.\n\n"
            "💰 <b>Flexible reward usage</b>\n"
            "Referral rewards are credited to your account balance and can be used to purchase products or withdrawn. Minimum withdrawal: ¥10.\n\n"
            "🔗 <b>Your personal invitation link</b>\n"
            f'<a href="{safe_link}">{html.escape(invite_link)}</a>\n\n'
            "Copy and share the link with friends. Their future eligible paid orders will continue to earn referral rewards for you."
        )
    else:
        text = (
            "⭐ <b>永久邀请奖励计划</b>\n"
            "分享专属邀请链接，建立长期有效的好友邀请关系。\n\n"
            "🤝 <b>好友消费，永久返佣 5%</b>\n"
            "好友通过您的专属链接加入后将永久绑定。此后每笔符合条件且已成功支付的订单，您都可获得订单金额 5% 的邀请奖励，绑定关系不可转移。\n\n"
            "💰 <b>奖励灵活使用</b>\n"
            "邀请奖励计入账户余额，可用于购买商品，也可申请提现。提现最低门槛为 ¥10。\n\n"
            "🔗 <b>您的专属邀请链接</b>\n"
            f'<a href="{safe_link}">{html.escape(invite_link)}</a>\n\n'
            "复制并分享链接给好友。好友后续成功支付的合格订单，将持续为您累计邀请奖励。"
        )
    # Keep invitation wording intact while applying the same global alignment.
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=get_invite_kb(language)
        )
    except Exception as exc:
        print("⚠️ 邀请奖励页面更新失败：", repr(exc), flush=True)
        if "message is not modified" not in str(exc).lower():
            try:
                bot.send_message(
                    chat_id, text, parse_mode="HTML",
                    reply_markup=get_invite_kb(language)
                )
            except Exception as send_exc:
                print("❌ 邀请奖励页面补发失败：", repr(send_exc), flush=True)

def render_invite_friends(chat_id, message_id, uid, page=0):
    selected, total, pages, page = get_referral_page(uid, page)
    user = get_user(uid)
    language = user.get("language", "zh")
    if language == "en":
        lines = [
            "👥 <b>REFERRAL ORDER STATEMENTS</b>",
            "<i>Paid orders from permanently linked invited friends</i>",
            "",
            f"Records: <b>{total}</b>  ·  Page <b>{page+1}/{pages}</b>",
            "━━━━━━━━━━━━━━━━━━"
        ]
    else:
        lines = [
            "👥 <b>邀请好友 · 订单返佣明细</b>",
            "<i>逐笔查看好友资料、成功订单及对应邀请收益</i>",
            "",
            f"记录总数：<b>{total}</b> 条　·　第 <b>{page+1}/{pages}</b> 页",
            "━━━━━━━━━━━━━━━━━━"
        ]
    if selected:
        for index, row in enumerate(selected, start=page * REFERRAL_PAGE_SIZE + 1):
            nick = html.escape(row["nickname"][:40])
            uid_text = html.escape(row["user_id"])
            date_text = html.escape(format_local_time(row.get("date")))
            product = html.escape(row["product"][:80])
            order_id = html.escape(row["order_id"])
            if row["no_order"]:
                if language == "en":
                    lines.append(f"\n<b>{index:02d} · {nick}</b>  <code>{uid_text}</code>")
                    lines.append(f"Joined: {date_text}")
                    lines.append("<i>No successfully paid orders yet.</i>")
                else:
                    lines.append(f"\n<b>{index:02d} · {nick}</b>  <code>{uid_text}</code>")
                    lines.append(f"加入时间：{date_text}")
                    lines.append("<i>暂未产生已确认支付的订单。</i>")
                continue
            amount = float(row["amount"])
            commission = float(row["commission"])
            if language == "en":
                lines.append(f"\n<b>{index:02d} · {nick}</b>  <code>{uid_text}</code>")
                lines.append(f"Date: <b>{date_text}</b>")
                lines.append(f"Product: {product}")
                lines.append(f"Order: <code>{order_id}</code>")
                lines.append(f"Spend: <b>{amount:.2f} USDT</b>  ·  Rate: <b>{row['rate']*100:.0f}%</b>")
                lines.append(f"Reward: <b>+{commission:.2f} USDT</b>")
            else:
                lines.append(f"\n<b>{index:02d} · {nick}</b>  <code>{uid_text}</code>")
                lines.append(f"消费日期：<b>{date_text}</b>")
                lines.append(f"商品名称：{product}")
                lines.append(f"订单编号：<code>{order_id}</code>")
                lines.append(f"消费金额：<b>{amount:.2f} USDT</b>　返佣比例：<b>{row['rate']*100:.0f}%</b>")
                lines.append(f"本笔返佣：<b>+{commission:.2f} USDT</b>")
            lines.append("────────────────")
    else:
        lines.append("\n<i>No referral records yet.</i>" if language == "en" else "\n<i>暂无邀请好友或订单记录，好友成功消费后会在这里逐笔展示。</i>")
    lines.append("\n<i>Only successfully paid orders are included. Commission is calculated at 5%.</i>" if language == "en"
                 else "\n<i>仅统计系统确认成功的订单；返佣按订单实付金额的 5% 计算。</i>")
    safe_edit(bot, chat_id, message_id, "\n".join(lines), get_invite_friends_kb(page, pages, language))

@bot.callback_query_handler(func=lambda call: call.data == "ui_invite")
def show_invite(call):
    bot.answer_callback_query(call.id)
    render_invite_page(call.message.chat.id, call.message.message_id, call.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("invite_spending_"))
def show_invite_spending(call):
    try:
        page = int(call.data.rsplit("_", 1)[1])
    except ValueError:
        page = 0
    bot.answer_callback_query(call.id)
    render_invite_friends(call.message.chat.id, call.message.message_id, call.from_user.id, page)


@bot.callback_query_handler(func=lambda call: call.data.startswith("invite_friends_"))
def show_invite_friends(call):
    try:
        page = int(call.data.rsplit("_", 1)[1])
    except ValueError:
        page = 0
    bot.answer_callback_query(call.id)
    render_invite_friends(call.message.chat.id, call.message.message_id, call.from_user.id, page)

@bot.callback_query_handler(func=lambda call: call.data == "invite_page_info")
def invite_page_info(call):
    bot.answer_callback_query(call.id)

def render_wallet_page(chat_id, message_id, uid):
    user = get_user(uid)
    language = user.get("language", "zh")
    address = str(user.get("wallet_address") or "")
    display_address = address if address else ("Not bound" if language == "en" else "暂未绑定")
    balance = float(user.get("balance", 0) or 0)
    balance_cny = float(price_cny(balance))
    if language == "en":
        text = (
            "<b>💼 WALLET MANAGEMENT</b>\n"
            "Network: TRON (TRC20)\n\n"
            f"💰 Available balance: ¥{balance_cny:.2f}\n"
            ""
            f"🔗 Bound receiving address\n<code>{html.escape(display_address)}</code>\n\n"
            "<b>📖 Binding & withdrawal guide</b>\n"
            "1. Tap [Bind / change wallet] below.\n"
            "2. Enter your USDT (TRC20) receiving address as prompted.\n"
            "3. The wallet is bound after the system verifies the address format.\n"
            "<b>4.</b>&#160;Withdrawals require at least ¥80.00. Funds can be withdrawn directly or used to purchase accounts.\n"
            "5. Withdrawal requests are reviewed manually and paid within 24 hours. Please wait patiently."
        )
    else:
        text = (
            "<b>💼 钱包地址管理</b>\n"
            "网络：TRON (TRC20)\n\n"
            f"💰 账户可用余额：¥{balance_cny:.2f}\n"
            ""
            f"🔗 当前绑定收款地址\n<code>{html.escape(display_address)}</code>\n\n"
            "<b>📖 绑定与提现教程</b>\n"
            "1. 点击下方【绑定/更换钱包】按钮。\n"
            "2. 按提示输入您的 USDT (TRC20) 收款地址。\n"
            "<b>3.</b>&#160;系统核验地址格式无误后即绑定成功。\n"
            "<b>4.</b>&#160;提现需满¥80.00，可用于直接提现或购买账号。\n"
            "<b>5.</b>&#160;提交提现申请后，24小时内全程人工审核打款，请耐心等待。"
        )
    safe_edit(bot, chat_id, message_id, text, get_wallet_kb(language), premiumize=False)


@bot.callback_query_handler(func=lambda call: call.data == "wallet_bind")
def wallet_bind(call):
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    language = user.get("language", "zh")
    prompt = ("Send your TRC20 USDT address. You will be asked to verify the full address before saving it."
              if language == "en" else
              "请发送您的 TRC20-USDT 收款地址。系统会先展示完整地址供您核对，确认后才会保存。")
    sent = bot.send_message(call.message.chat.id, prompt, reply_markup=types.ForceReply(selective=True))
    bot.register_next_step_handler(sent, receive_wallet_address)

def receive_wallet_address(message):
    address = (message.text or "").strip()
    user = get_user(message.from_user.id)
    language = user.get("language", "zh")
    if not valid_tron_address(address):
        bot.reply_to(message, "Invalid TRON address. Please enter a valid TRC20 address starting with T." if language == "en"
                     else "地址格式不正确。请检查是否为 TRON 网络的 TRC20 地址（通常以 T 开头），然后重新绑定。")
        return
    users = load_json_file(USERS_FILE, {})
    users.setdefault(str(message.from_user.id), {})["pending_wallet_address"] = address
    save_json_file(USERS_FILE, users)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        premium_button("Confirm and save address" if language == "en" else "核对无误，确认绑定",
                       callback_data="wallet_confirm"),
        premium_button("Cancel" if language == "en" else "取消", callback_data="wallet_cancel"),
    )
    preview = (f"Please verify this complete address carefully:\n<code>{html.escape(address)}</code>\n\n"
               "Only confirm if every character is correct." if language == "en" else
               f"请仔细核对以下完整收款地址：\n<code>{html.escape(address)}</code>\n\n确认前请逐位检查，确保网络和地址均正确。")
    bot.send_message(message.chat.id, preview, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "wallet_confirm")
def wallet_confirm(call):
    users = load_json_file(USERS_FILE, {})
    user = users.get(str(call.from_user.id), {})
    address = str(user.get("pending_wallet_address") or "")
    if not valid_tron_address(address):
        bot.answer_callback_query(call.id, "No pending address to confirm.", show_alert=True)
        return
    user["wallet_address"] = address
    user.pop("pending_wallet_address", None)
    users[str(call.from_user.id)] = user
    save_json_file(USERS_FILE, users)
    bot.answer_callback_query(call.id, "Wallet address saved." if user.get("language") == "en" else "钱包地址已确认保存")
    bot.send_message(call.message.chat.id, "Wallet address updated successfully." if user.get("language") == "en"
                     else "钱包地址已成功绑定 / 更新。")
    render_wallet_page(call.message.chat.id, call.message.message_id, call.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data == "wallet_cancel")
def wallet_cancel(call):
    users = load_json_file(USERS_FILE, {})
    user = users.get(str(call.from_user.id), {})
    user.pop("pending_wallet_address", None)
    users[str(call.from_user.id)] = user
    save_json_file(USERS_FILE, users)
    bot.answer_callback_query(call.id, "Cancelled.")
    render_wallet_page(call.message.chat.id, call.message.message_id, call.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data == "wallet_withdraw")
def wallet_withdraw(call):
    user = get_user(call.from_user.id)
    language = user.get("language", "zh")
    address = str(user.get("wallet_address") or "")
    balance = float(user.get("balance", 0) or 0)
    if not valid_tron_address(address):
        bot.answer_callback_query(
            call.id,
            "请先绑定收款地址" if language != "en" else "Please bind a receiving address first.",
            show_alert=True
        )
        return
    if balance < WITHDRAW_MINIMUM:
        bot.answer_callback_query(
            call.id,
            "余额不足，提现门槛为 10" if language != "en" else "Insufficient balance. Minimum withdrawal is 10.",
            show_alert=True
        )
        return
    bot.answer_callback_query(call.id)
    prompt = (
        "Enter the amount to withdraw (USDT)." if language == "en"
        else "请输入您要提现的金额（USDT）"
    )
    sent = bot.send_message(
        call.message.chat.id, prompt,
        reply_markup=types.ForceReply(selective=True)
    )
    bot.register_next_step_handler(sent, receive_withdraw_amount)


def receive_withdraw_amount(message):
    uid = str(message.from_user.id)
    user = get_user(uid)
    language = user.get("language", "zh")
    raw_amount = (message.text or "").strip()
    try:
        amount = round(float(raw_amount), 6)
    except (TypeError, ValueError):
        amount = 0
    users = load_json_file(USERS_FILE, {})
    user = users.get(uid, {})
    balance = float(user.get("balance", 0) or 0)
    address = str(user.get("wallet_address") or "")
    if not valid_tron_address(address):
        bot.reply_to(message, "Please bind a valid TRC20 address first." if language == "en" else "请先绑定有效的 TRC20 收款地址。")
        return
    if amount < WITHDRAW_MINIMUM or amount > balance:
        error = (
            f"Amount must be at least 10 USDT and no more than your balance ({balance:.6f} USDT). Please enter again."
            if language == "en" else
            f"提现金额需不少于 10，且不能超过当前可用余额（{balance:.6f}）。请重新输入。"
        )
        retry = bot.reply_to(message, error, reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(retry, receive_withdraw_amount)
        return

    user["pending_withdrawal"] = {
        "amount": amount,
        "address": address,
        "created_at": time.time(),
    }
    users[uid] = user
    save_json_file(USERS_FILE, users)

    amount_cny = float(price_cny(amount))
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("确认提现" if language != "en" else "Confirm withdrawal",
                                   callback_data="wallet_withdraw_confirm"),
        types.InlineKeyboardButton("取消" if language != "en" else "Cancel and return",
                                   callback_data="wallet_withdraw_cancel")
    )
    text = (
        f"✅ <b>提现确认</b>\n收款地址：<code>{html.escape(address)}</code>\n"
        f"提现金额：<b>¥{amount_cny:.2f}</b>\n请核对以上信息无误。"
        if language != "en" else
        f"✅ <b>Withdrawal confirmation</b>\nReceiving address: <code>{html.escape(address)}</code>\n"
        f"Amount: <b>¥{amount_cny:.2f}</b>\nPlease verify these details."
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "wallet_withdraw_confirm")
def wallet_withdraw_confirm(call):
    uid = str(call.from_user.id)
    language = get_user(uid).get("language", "zh")
    with _withdraw_lock:
        users_before = load_json_file(USERS_FILE, {})
        withdrawals_before = load_json_file(WITHDRAWALS_FILE, [])
        users = copy.deepcopy(users_before)
        withdrawals = copy.deepcopy(withdrawals_before)
        user = users.get(uid, {})
        pending = user.get("pending_withdrawal") or {}
        amount = round(float(pending.get("amount", 0) or 0), 6)
        address = str(pending.get("address") or "")
        balance = float(user.get("balance", 0) or 0)
        if (amount < WITHDRAW_MINIMUM or amount > balance
                or not valid_tron_address(address)
                or address != user.get("wallet_address")):
            user.pop("pending_withdrawal", None)
            users[uid] = user
            save_json_file(USERS_FILE, users)
            bot.answer_callback_query(
                call.id,
                "提现信息已失效，请重新申请。" if language != "en" else "Withdrawal details expired. Please try again.",
                show_alert=True
            )
            return

        request_id = f"WD{int(time.time() * 1000)}{uid[-4:]}"
        amount_cny = float(price_cny(amount))
        user["balance"] = round(balance - amount, 6)
        user.pop("pending_withdrawal", None)
        users[uid] = user
        withdrawals.append({
            "request_id": request_id,
            "user_id": uid,
            "amount": amount,
            "amount_cny": amount_cny,
            "address": address,
            "status": "pending",
            "created_at": time.time(),
        })

        # Persist both the debit and request together into the pinned Telegram archive snapshot.
        try:
            save_json_file(USERS_FILE, users)
            save_json_file(WITHDRAWALS_FILE, withdrawals)
            if ARCHIVE_STORE is None:
                raise RuntimeError("Telegram 归档存储未初始化")
            ARCHIVE_STORE.flush("提现申请提交")
        except Exception as exc:
            save_json_file(USERS_FILE, users_before)
            save_json_file(WITHDRAWALS_FILE, withdrawals_before)
            print("❌ 提现申请归档失败，已回滚余额变更：", repr(exc), flush=True)
            bot.answer_callback_query(
                call.id,
                "归档暂时不可用，未扣款，请稍后重试。" if language != "en" else "Archive unavailable. No balance was deducted; retry later.",
                show_alert=True
            )
            return

    # Publish an actionable manual-review card to the private Telegram archive channel.
    applicant = call.from_user
    nickname = " ".join(x for x in (applicant.first_name, applicant.last_name) if x).strip() or "用户"
    username = f"@{applicant.username}" if applicant.username else "无"
    applied_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    with _withdraw_lock:
        current_records = load_json_file(WITHDRAWALS_FILE, [])
        for record in current_records:
            if str(record.get("request_id")) == request_id:
                record.update({"nickname": nickname, "username": applicant.username or "", "applied_at": applied_at})
                break
        try:
            save_json_file(WITHDRAWALS_FILE, current_records)
            ARCHIVE_STORE.flush("提现申请审核信息")
        except Exception as exc:
            # The pending request itself was already durably recorded above.
            # Continue publishing the review card; the core request remains auditable.
            print("⚠️ 提现昵称等展示信息归档失败：", repr(exc), flush=True)

    review_kb = types.InlineKeyboardMarkup(row_width=2)
    review_kb.row(
        types.InlineKeyboardButton("✅ 确认打款", callback_data=f"withdraw_paid:{request_id}"),
        types.InlineKeyboardButton("💬 该用户", url=f"tg://user?id={uid}")
    )
    archive_text = (
        "🚨 <b>新提现申请</b>\n\n"
        f"👤 <b>用户昵称</b>：<a href=\"tg://user?id={uid}\">{html.escape(nickname)}</a>\n"
        f"🆔 <b>数字 ID</b>：<code>{uid}</code>\n"
        f"🔗 <b>用户名</b>：{html.escape(username)}\n"
        f"💰 <b>提现金额</b>：¥{amount_cny:.2f}\n"
        f"📅 <b>申请日期</b>：{html.escape(applied_at)}\n"
        f"🔗 <b>链上地址</b>：<code>{html.escape(address)}</code>\n"
        f"申请编号：<code>{request_id}</code>\n"
        "审核状态：待人工审核"
    )
    archive_sent = False
    try:
        bot.send_message(ARCHIVE_CHAT_ID, archive_text, parse_mode="HTML",
                         reply_markup=review_kb, disable_notification=False)
        archive_sent = True
    except Exception as exc:
        print("❌ 提现申请归档频道通知发送失败：", repr(exc), flush=True)

    if ADMIN_ID.isdigit():
        try:
            bot.send_message(
                int(ADMIN_ID), archive_text, parse_mode="HTML",
                reply_markup=review_kb, disable_notification=False
            )
        except Exception as exc:
            print("⚠️ 管理员提现通知发送失败：", repr(exc), flush=True)

    bot.answer_callback_query(call.id, "提现申请已提交" if language != "en" else "Withdrawal submitted.")
    final_text = (
        "提现申请已提交，24小时内人工审核打款"
        if language != "en" else
        "Withdrawal request submitted. Manual review and payout will be completed within 24 hours."
    )
    if not archive_sent:
        final_text += "\n归档频道通知发送失败，申请已保存在归档快照中。"
    try:
        bot.edit_message_text(
            final_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
    except Exception as exc:
        print("⚠️ 提现确认消息编辑失败：", repr(exc), flush=True)
        bot.send_message(call.message.chat.id, final_text)


@bot.callback_query_handler(func=lambda call: str(call.data or "").startswith("withdraw_paid:"))
def admin_confirm_withdraw_paid(call):
    """管理员确认已完成的人工打款；此回调本身不会发起链上转账。"""
    admin_id = str(call.from_user.id)
    if not ADMIN_ID.isdigit() or admin_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "无权执行此操作。", show_alert=True)
        return

    request_id = str(call.data).split(":", 1)[1].strip()
    if not request_id:
        bot.answer_callback_query(call.id, "提现申请编号无效。", show_alert=True)
        return
    admin_name = " ".join(x for x in (call.from_user.first_name, call.from_user.last_name) if x).strip()
    admin_name = admin_name or f"管理员 {admin_id}"
    now_text = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    with _withdraw_lock:
        records_before = load_json_file(WITHDRAWALS_FILE, [])
        records = copy.deepcopy(records_before)
        target = next((x for x in records if str(x.get("request_id")) == request_id), None)
        if target is None:
            bot.answer_callback_query(call.id, "未找到该提现申请。", show_alert=True)
            return
        if target.get("status") == "paid":
            bot.answer_callback_query(call.id, "该笔提现已确认打款，不能重复操作。", show_alert=True)
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return
        if target.get("status") != "pending":
            bot.answer_callback_query(call.id, "该提现申请当前状态不可确认。", show_alert=True)
            return

        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as exc:
            print("⚠️ 移除提现审核按钮失败：", repr(exc), flush=True)

        target.update({"status": "paid", "paid_at": now_text, "paid_by": admin_id, "paid_by_name": admin_name})
        try:
            save_json_file(WITHDRAWALS_FILE, records)
            if ARCHIVE_STORE is None:
                raise RuntimeError("Telegram 归档存储未初始化")
            ARCHIVE_STORE.flush("管理员确认提现打款")
        except Exception as exc:
            save_json_file(WITHDRAWALS_FILE, records_before)
            try:
                restore_kb = types.InlineKeyboardMarkup(row_width=2)
                restore_kb.row(
                    types.InlineKeyboardButton("✅ 确认打款", callback_data=f"withdraw_paid:{request_id}"),
                    types.InlineKeyboardButton("💬 该用户", url=f"tg://user?id={target.get('user_id')}")
                )
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=restore_kb)
            except Exception:
                pass
            print("❌ 提现打款状态归档失败：", repr(exc), flush=True)
            bot.answer_callback_query(call.id, "状态保存失败，未确认打款，请重试。", show_alert=True)
            return

    uid = str(target.get("user_id") or "")
    amount_cny = float(target.get("amount_cny", 0) or 0)
    address = str(target.get("address") or "")
    try:
        bot.edit_message_text(
            str(call.message.text or "") + f"\n\n<b>✅ 已打款 (操作人: {html.escape(admin_name)})</b>",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            parse_mode="HTML", reply_markup=None
        )
    except Exception as exc:
        print("⚠️ 管理员提现审核消息更新失败：", repr(exc), flush=True)

    user_text = (
        "🎉 <b>提现到账通知</b>\n"
        "您好，您的提现申请已成功打款！\n"
        f"💰 <b>打款金额</b>：¥{amount_cny:.2f}\n"
        f"📅 <b>到账日期</b>：{html.escape(now_text)}\n"
        f"🔗 <b>收款地址</b>：<code>{html.escape(address)}</code>\n"
        "请注意查收，感谢您的使用！"
    )
    try:
        bot.send_message(int(uid), user_text, parse_mode="HTML")
        dm_result = "用户到账通知已发送。"
    except Exception as exc:
        dm_result = "打款状态已记录，但用户私信发送失败（可能已拉黑机器人或未开启私聊）。"
        print("⚠️ 提现到账私信发送失败：", uid, repr(exc), flush=True)

    bot.answer_callback_query(call.id, "已记录打款确认。")
    try:
        bot.send_message(call.message.chat.id, dm_result)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "wallet_withdraw_cancel")
def wallet_withdraw_cancel(call):
    users = load_json_file(USERS_FILE, {})
    user = users.get(str(call.from_user.id), {})
    user.pop("pending_withdrawal", None)
    users[str(call.from_user.id)] = user
    save_json_file(USERS_FILE, users)
    bot.answer_callback_query(call.id, "已取消" if user.get("language") != "en" else "Cancelled.")
    render_wallet_page(call.message.chat.id, call.message.message_id, call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "ui_topup")
def show_topup(call):
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    language = user.get("language", "zh")
    if language == "en":
        alipay_text = "Hello, I would like to top up my account via Alipay. Please provide the payment instructions."
        usdt_text = "Hello, I would like to top up my account via USDT TRC20. Please provide the payment instructions."
        text = (
            "💳 <b>ACCOUNT TOP-UP</b>\n"
            "Choose a payment method below.\n\n"
            "🟦 <b>Alipay</b>\n"
            "RMB payment. Fixed rate: <b>1 USDT = ¥8 CNY</b>. Confirm the amount and receiving account with support before paying.\n\n"
            "🟩 <b>USDT (TRC20)</b>\n"
            "TRON / TRC20 network only. Fixed rate: <b>1 USDT = ¥8 CNY</b>. Confirm the receiving address before transferring.\n\n"
            "⚠️ <i>Top-ups are handled with support assistance. Automatic balance crediting is not enabled. Send your payment receipt for manual verification.</i>"
        )
    else:
        alipay_text = "你好，我要通过支付宝充值账户余额，请提供本次充值金额及收款方式。"
        usdt_text = "你好，我要通过 USDT（TRC20）充值账户余额，请提供本次充值金额及收款地址。"
        text = (
            "<b>💳 账户充值</b>\n\n"
            "请选择充值方式：\n\n"
            "<b>🟦 支付宝</b>\n"
            "人民币付款。固定汇率：1 USDT = 8 CNY。\n"
            "付款前请联系客服确认充值金额及收款账户。\n\n"
            "<b>🟩 USDT (TRC20)</b>\n"
            "仅支持 TRON / TRC20 网络。固定汇率：1 USDT = 8 CNY。\n"
            "转账前请先确认完整收款地址。\n\n"
            "⚠️ <b>目前由客服协助处理充值，付款后请发送凭证供人工核验；暂不支持付款后自动入账。请勿在确认收款信息前转账。</b>"
        )

    from urllib.parse import quote
    support_url = "https://t.me/qingtian_Boss"
    kb = types.InlineKeyboardMarkup(keyboard=[
        [
            types.InlineKeyboardButton(text="支付宝充值", callback_data="ui_topup_alipay"),
            types.InlineKeyboardButton(text="USDT充值", callback_data="ui_topup_usdt"),
        ],
        [
            types.InlineKeyboardButton(text="联系客服", url=support_url),
            types.InlineKeyboardButton(text="⬅️ 返回", callback_data="ui_recharge"),
        ],
    ])
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb, premiumize=False, align_copy="text")


@bot.callback_query_handler(func=lambda call: call.data == "ui_topup_alipay")
def show_alipay_topup(call):
    bot.answer_callback_query(call.id)

    caption = (
        "<b>💳 支付宝充值</b>\n"
        "<b>收款方式：</b>请使用支付宝扫码付款\n"
        "<b>固定汇率：</b>1U = 8元\n"
        "<b>最低充值：</b>10元\n\n"
        "⚠️ <i>转账完成后，系统将自动为您匹配入账。</i>"
    )
    kb = types.InlineKeyboardMarkup(keyboard=[
        [
            types.InlineKeyboardButton(text="10元", callback_data="amt_10"),
            types.InlineKeyboardButton(text="20元", callback_data="amt_20"),
        ],
        [
            types.InlineKeyboardButton(text="50元", callback_data="amt_50"),
            types.InlineKeyboardButton(text="100元", callback_data="amt_100"),
        ],
        [
            types.InlineKeyboardButton(text="自定义", callback_data="amt_custom"),
            types.InlineKeyboardButton(text="⬅️ 返回", callback_data="ui_recharge"),
        ],
    ])
    qr_file_id = os.getenv("ALIPAY_PERSONAL_QR_FILE_ID", "").strip()
    if qr_file_id:
        bot.send_photo(
            call.message.chat.id,
            photo=qr_file_id,
            caption=caption,
            reply_markup=kb,
            parse_mode="HTML",
        )
    else:
        bot.send_message(
            call.message.chat.id,
            "<b>💳 支付宝充值</b>\n"
            "<b>收款方式：</b>收款码尚未登记，请管理员重新发送真实支付宝收款码图片。\n"
            "<b>固定汇率：</b>1U = 8元\n"
            "<b>最低充值：</b>10元\n\n"
            "⚠️ <i>请勿向未经核验的二维码付款。</i>",
            reply_markup=kb,
            parse_mode="HTML",
        )


@bot.callback_query_handler(func=lambda call: call.data == "ui_topup_usdt")
def show_usdt_topup(call):
    address = "TTT5MV8xeZqKaPDxUcDjR8bPFz2ce5kmBr"
    if not valid_tron_address(address):
        bot.answer_callback_query(call.id, "TRC20 收款地址配置无效，请检查配置。", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    caption = (
        "<b>💳 USDT (TRC20) 充值</b>\n"
        "<b>链上地址：</b>\n"
        f"<code>{html.escape(address)}</code>\n"
        "<b>最低充值：</b>10 USDT\n"
        "<b>固定汇率：</b>1 USDT = 8 CNY\n\n"
        "⚠️ <i>转账完成后，系统将自动为您匹配入账。</i>"
    )
    kb = types.InlineKeyboardMarkup(keyboard=[
        [
            types.InlineKeyboardButton(text="5 USDT", callback_data="amt_5"),
            types.InlineKeyboardButton(text="10 USDT", callback_data="amt_10"),
        ],
        [
            types.InlineKeyboardButton(text="20 USDT", callback_data="amt_20"),
            types.InlineKeyboardButton(text="50 USDT", callback_data="amt_50"),
        ],
        [
            types.InlineKeyboardButton(text="自定义", callback_data="amt_custom"),
            types.InlineKeyboardButton(text="⬅️ 返回", callback_data="ui_recharge"),
        ],
    ])
    bot.send_photo(
        call.message.chat.id,
        photo=create_payment_qr(address),
        caption=caption,
        reply_markup=kb,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "amt_custom")
def prompt_usdt_custom_amount(call):
    bot.answer_callback_query(call.id)
    prompt = bot.send_message(
        call.message.chat.id,
        "请输入自定义充值金额（USDT），最低 10 USDT，最多 6 位小数。\n发送 /cancel 可取消。",
        reply_markup=types.ForceReply(selective=True),
    )
    bot.register_next_step_handler(prompt, receive_usdt_custom_amount)


def receive_usdt_custom_amount(message):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        bot.send_message(message.chat.id, "已取消自定义充值。")
        return
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount < Decimal("10") or amount.as_tuple().exponent < -6:
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError, TypeError):
        retry = bot.reply_to(
            message,
            "金额格式无效。请输入不低于 10 的数字，最多 6 位小数；例如：35 或 12.5。",
            reply_markup=types.ForceReply(selective=True),
        )
        bot.register_next_step_handler(retry, receive_usdt_custom_amount)
        return

    users = load_json_file(USERS_FILE, {})
    uid = str(message.from_user.id)
    user = users.setdefault(uid, {})
    user["pending_usdt_custom_topup"] = {
        "amount": format(amount, "f"),
        "created_at": time.time(),
    }
    save_json_file(USERS_FILE, users)

    cny = price_cny(amount)
    kb = types.InlineKeyboardMarkup(keyboard=[[
        types.InlineKeyboardButton(text="确认金额并显示收款码", callback_data="usdt_custom_confirm"),
        types.InlineKeyboardButton(text="取消", callback_data="usdt_custom_cancel"),
    ]])
    bot.send_message(
        message.chat.id,
        f"<b>请确认自定义充值金额</b>\n充值金额：<b>{format(amount, 'f')} USDT</b>\n"
        f"按固定汇率 1 USDT = 8 CNY，折合：<b>{cny:.2f} CNY</b>\n\n"
        "确认后将显示 TRC20 收款二维码。请只使用 TRON (TRC20) 网络，并按确认金额转账。",
        parse_mode="HTML",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data == "usdt_custom_confirm")
def confirm_usdt_custom_topup(call):
    users = load_json_file(USERS_FILE, {})
    uid = str(call.from_user.id)
    user = users.get(uid, {})
    pending = user.get("pending_usdt_custom_topup") or {}
    try:
        created_at = float(pending.get("created_at", 0))
        amount = Decimal(str(pending.get("amount", "")))
        if time.time() - created_at > 900 or not amount.is_finite() or amount < Decimal("10"):
            raise ValueError
    except (ValueError, TypeError, InvalidOperation):
        user.pop("pending_usdt_custom_topup", None)
        users[uid] = user
        save_json_file(USERS_FILE, users)
        bot.answer_callback_query(call.id, "金额确认已过期，请重新输入。", show_alert=True)
        return

    user.pop("pending_usdt_custom_topup", None)
    users[uid] = user
    save_json_file(USERS_FILE, users)
    try:
        order = create_custom_topup_order(
            uid, amount,
            load_orders=lambda: load_json_file(ORDERS_FILE, []),
            save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
        )
        if ARCHIVE_STORE is None:
            raise RuntimeError("Telegram 归档存储未就绪")
        ARCHIVE_STORE.flush("创建 USDT 自定义充值待支付订单")
    except Exception as exc:
        bot.answer_callback_query(call.id, "订单创建失败，请稍后重试。", show_alert=True)
        print("USDT 自定义充值订单创建失败：", type(exc).__name__, flush=True)
        return
    bot.answer_callback_query(call.id)
    address = os.getenv("TRON_RECEIVE_ADDRESS", "TTT5MV8xeZqKaPDxUcDjR8bPFz2ce5kmBr").strip()
    amount = Decimal(str(order["amount_usdt"]))
    cny = price_cny(amount)
    caption = (
        "<b>💳 USDT (TRC20) 自定义充值</b>\n"
        f"<b>订单号：</b><code>{html.escape(order['order_id'])}</code>\n"
        f"<b>请精确转账：</b><code>{format(amount, '.6f')} USDT</code>\n"
        f"<b>折合金额：</b>{cny:.2f} CNY\n"
        "<b>链上地址：</b>\n"
        f"<code>{html.escape(address)}</code>\n\n"
        "⚠️ <i>请务必精确转账指定金额，链上确认后系统自动匹配并入账。</i>"
    )
    kb = types.InlineKeyboardMarkup(keyboard=[[
        types.InlineKeyboardButton(text="⬅️ 返回", callback_data="ui_topup_usdt"),
    ], [
        types.InlineKeyboardButton(text="👤 用户中心", callback_data="ui_recharge"),
    ]])
    bot.send_photo(
        call.message.chat.id,
        photo=create_payment_qr(address),
        caption=caption,
        reply_markup=kb,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "usdt_custom_cancel")
def cancel_usdt_custom_topup(call):
    users = load_json_file(USERS_FILE, {})
    uid = str(call.from_user.id)
    user = users.get(uid, {})
    user.pop("pending_usdt_custom_topup", None)
    users[uid] = user
    save_json_file(USERS_FILE, users)
    bot.answer_callback_query(call.id, "已取消")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "ui_recharge")
def show_recharge(call):
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    uid = str(call.from_user.id)
    display_name = str(user.get("name") or call.from_user.first_name or "用户")
    balance = float(user.get("balance", 0) or 0)
    referral_reward = float(user.get("invite_bonus", 0) or 0)
    balance_cny = float(price_cny(balance))
    referral_cny = float(price_cny(referral_reward))
    all_orders = load_json_file(ORDERS_FILE, [])
    paid_orders = [
        item for item in all_orders
        if isinstance(item, dict)
        and _order_user_id(item) == uid
        and _ledger_is_real_paid_order(item)
    ] if isinstance(all_orders, list) else []
    spending_cny = sum(_ledger_amount_cny(item) for item in paid_orders)
    wallet = str(user.get("wallet_address") or "")
    wallet_display = wallet if wallet else "暂未绑定"

    text = (
        "<b>👤 用户中心</b>\n\n"
        f"昵称：<b>{html.escape(display_name)}</b>\n"
        f"数字 ID：<code>{html.escape(uid)}</code>\n\n"
        f"可用余额：<b>¥{balance_cny:.2f}</b>\n"
        f"邀请奖励：<b>¥{referral_cny:.2f}</b>\n"
        f"累计消费：<b>¥{spending_cny:.2f}</b>\n\n"
        "收款地址 (TRC20)：\n"
        f"<code>{html.escape(wallet_display)}</code>\n\n"
        "<i>💡 提示：提现前请核对地址；如需充值，请点击下方【充值】按钮。</i>"
    )

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("💼 钱包管理", callback_data="wallet_manage"),
        types.InlineKeyboardButton("💸 申请提现", callback_data="wallet_withdraw")
    )
    kb.row(
        types.InlineKeyboardButton("💳 充值", callback_data="ui_topup"),
        types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy")
    )
    # Bypass global alignment rewriting: preserve the exact authored HTML line breaks.
    try:
        _original_edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            _original_send_message(
                chat_id=call.message.chat.id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )


@bot.callback_query_handler(func=lambda call: call.data == "wallet_manage")
def wallet_manage(call):
    bot.answer_callback_query(call.id)
    render_wallet_page(call.message.chat.id, call.message.message_id, call.from_user.id)

def archive_source_bundle():
    """Upload a source-only ZIP to the private archive channel, replacing the prior bundle."""
    if ARCHIVE_STORE is None:
        return False

    project_root = os.path.dirname(os.path.abspath(__file__))
    excluded_dirs = {".git", "__pycache__", "venv", ".venv", "node_modules"}
    allowed_extensions = {".py", ".txt", ".yml", ".yaml", ".md", ".toml", ".ini", ".cfg", ".sh"}
    excluded_fragments = (".env", ".pem", ".key", "secret", "credential", "private_key")
    files = []

    for current_root, dirnames, filenames in os.walk(project_root):
        dirnames[:] = sorted(name for name in dirnames if name not in excluded_dirs)
        for filename in filenames:
            relative_path = os.path.relpath(os.path.join(current_root, filename), project_root)
            lowered = relative_path.lower()
            extension = os.path.splitext(filename)[1].lower()
            if extension not in allowed_extensions or any(item in lowered for item in excluded_fragments):
                continue
            full_path = os.path.join(current_root, filename)
            try:
                with open(full_path, "rb") as handle:
                    files.append((relative_path.replace(os.sep, "/"), handle.read()))
            except OSError as exc:
                print("⚠️ 源码归档跳过无法读取的文件：", relative_path, repr(exc), flush=True)

    files.sort(key=lambda item: item[0])
    if not files:
        print("⚠️ 源码归档未找到可上传的源文件", flush=True)
        return False

    digest = hashlib.sha256()
    for relative_path, content in files:
        digest.update(relative_path.encode("utf-8") + b"\\0" + content)
    source_digest = digest.hexdigest()
    meta = ARCHIVE_STORE.load("meta", {})
    if meta.get("source_bundle_sha256") == source_digest:
        return False

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_path, content in files:
            archive.writestr(relative_path, content)
    buffer.seek(0)
    buffer.name = "qingtian_fakabot_source.zip"
    caption = (
        "☀️ 晴天号铺 · 源码归档\n"
        f"文件数量：{len(files)}\n"
        f"SHA-256：<code>{source_digest}</code>\n"
        "仅包含程序源文件与依赖清单；排除业务 JSON、环境变量、密钥、日志和运行缓存。"
    )

    previous_message_id = meta.get("source_bundle_message_id")
    message_id = None
    if previous_message_id:
        try:
            media = types.InputMediaDocument(media=buffer, caption=caption, parse_mode="HTML")
            bot.edit_message_media(
                media=media,
                chat_id=ARCHIVE_CHAT_ID,
                message_id=int(previous_message_id),
            )
            message_id = int(previous_message_id)
        except Exception as exc:
            print("⚠️ 更新源码归档消息失败，改为上传新文件：", repr(exc), flush=True)
            buffer.seek(0)

    if message_id is None:
        message = bot.send_document(
            ARCHIVE_CHAT_ID,
            buffer,
            caption=caption,
            parse_mode="HTML",
            disable_notification=True,
        )
        message_id = message.message_id

    meta["source_bundle_sha256"] = source_digest
    meta["source_bundle_message_id"] = message_id
    ARCHIVE_STORE.save("meta", meta)
    print("✅ 源码已批量归档至 Telegram 私密频道：", len(files), "个文件", flush=True)
    return True


def referral_reward_sync_loop():
    while True:
        try:
            sync_referral_rewards()
        except Exception as exc:
            print("⚠️ 邀请返佣同步异常：", repr(exc), flush=True)
        time.sleep(20)


def daily_report_text(report_date):
    try:
        report_tz = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Asia/Shanghai"))
    except Exception:
        report_tz = ZoneInfo("UTC")
    users = load_json_file(USERS_FILE, {})
    orders = load_json_file(ORDERS_FILE, [])
    withdrawals = load_json_file(WITHDRAWALS_FILE, [])
    today_users = 0
    for user in users.values():
        try:
            joined = user.get("join_time")
            if joined and datetime.fromtimestamp(float(joined), tz=report_tz).date().isoformat() == report_date:
                today_users += 1
        except (TypeError, ValueError, OSError):
            continue
    paid_orders = [o for o in orders if isinstance(o, dict) and _order_is_paid(o)]
    today_paid = []
    for order in paid_orders:
        stamp = order.get("paid_at") or order.get("paid_time") or order.get("confirmed_at")
        try:
            paid_date = datetime.fromtimestamp(float(stamp), tz=report_tz).date().isoformat() if isinstance(stamp, (int, float)) else str(stamp)[:10]
        except (TypeError, ValueError, OSError):
            paid_date = ""
        if paid_date == report_date:
            today_paid.append(order)
    revenue = sum(_order_amount(o) for o in today_paid)
    pending_withdrawals = sum(
        1 for item in withdrawals
        if isinstance(item, dict) and str(item.get("status", "")).lower() in ("pending", "review")
    )
    copy_text = (
        "☀️ <b>晴天号铺 · 数字商品服务中心</b>\n"
        "账号商城、AI 服务与节点产品信息持续整理中。\n"
        "欢迎通过机器人查看商品规格、价格及购买说明；下单前请核对商品适用范围，具体以订单页面展示为准。\n"
        "有订单或使用问题，可通过机器人内的官方客服入口联系。"
    )
    return (
        f"☀️ <b>晴天号铺 · 每日运营工作汇报</b>\n"
        f"统计日期：<code>{report_date}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"用户档案：<b>{len(users)}</b> 人\n"
        f"今日新增：<b>{today_users}</b> 人\n"
        f"累计订单：<b>{len(orders)}</b> 笔\n"
        f"已确认支付：<b>{len(paid_orders)}</b> 笔\n"
        f"今日确认支付：<b>{len(today_paid)}</b> 笔\n"
        f"今日确认金额：<b>{revenue:.2f} USDT</b>\n"
        f"待审核提现：<b>{pending_withdrawals}</b> 笔\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>系统情况</b>\n"
        "当前在线支付入口仍待接通，以上支付统计只反映归档中已有的确认记录，不代表支付链路已完成。\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>今日宣传文案</b>\n"
        f"{copy_text}"
    )


def _metric_time(value, report_tz):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=report_tz)
        raw = str(value or "").strip()
        if not raw:
            return None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=report_tz) if parsed.tzinfo is None else parsed.astimezone(report_tz)
    except (TypeError, ValueError, OSError):
        return None


def three_hour_report_text(start_at, end_at, counters, report_tz):
    orders = load_json_file(ORDERS_FILE, [])
    paid = []
    revenue = 0.0
    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, dict) or not _order_is_paid(order):
                continue
            paid_at = _metric_time(
                order.get("paid_at") or order.get("paid_time") or order.get("confirmed_at") or order.get("created_at"),
                report_tz,
            )
            if paid_at and start_at <= paid_at < end_at:
                paid.append(order)
                revenue += _order_amount(order)
    start_text = start_at.strftime("%Y-%m-%d %H:%M")
    end_text = end_at.strftime("%Y-%m-%d %H:%M")
    return (
        "☀️ <b>晴天号铺 · 三小时运营汇报</b>\n"
        "平台：<b>Telegram</b> · 机器人：<b>号铺机器人</b>\n"
        f"统计日期：<code>{start_at.strftime('%Y-%m-%d')}</code>\n"
        f"统计区间：<code>{start_text} — {end_text}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"接待消息：<b>{int(counters.get('inbound_messages', 0))}</b> 次\n"
        f"按钮交互：<b>{int(counters.get('callback_interactions', 0))}</b> 次\n"
        f"成功收入：<b>{revenue:.2f} USDT</b>\n"
        f"成功支付订单：<b>{len(paid)}</b> 笔\n"
        f"签到：<b>{int(counters.get('checkins', 0))}</b> 次（当前未接入签到事件）\n"
        f"发出节点：<b>{int(counters.get('nodes_issued', 0))}</b> 个（当前未接入发放事件）\n"
        f"收回节点：<b>{int(counters.get('nodes_returned', 0))}</b> 个（当前未接入回收事件）\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>接待按消息和按钮操作次数累计，不做用户去重；未接入的业务事件不会虚构统计。</i>"
    )


def three_hour_report_loop():
    timezone_name = os.getenv("REPORT_TIMEZONE", "Asia/Shanghai")
    try:
        report_tz = ZoneInfo(timezone_name)
    except Exception:
        print("⚠️ REPORT_TIMEZONE 无效，三小时汇报使用 UTC", flush=True)
        report_tz = ZoneInfo("UTC")
    while True:
        now = datetime.now(report_tz)
        next_hour = ((now.hour // 3) + 1) * 3
        if next_hour >= 24:
            target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            target = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        time.sleep(max(1, (target - now).total_seconds()))
        end_at = datetime.now(report_tz).replace(minute=0, second=0, microsecond=0)
        start_at = end_at - timedelta(hours=3)
        counters = ARCHIVE_STORE.take_counters()
        try:
            report_text = three_hour_report_text(start_at, end_at, counters, report_tz)
            ARCHIVE_STORE.send_report_snapshot(
                report_text,
                report_title="三小时运营汇报 " + start_at.strftime("%Y-%m-%d %H:%M"),
            )
            print("✅ 三小时运营汇报已发送：" + start_at.isoformat(), flush=True)
        except Exception as exc:
            for key, value in counters.items():
                try:
                    ARCHIVE_STORE.increment_counter(key, int(value or 0))
                except Exception:
                    pass
            print("❌ 三小时运营汇报发送失败：", repr(exc), flush=True)


def daily_report_loop():
    timezone_name = os.getenv("REPORT_TIMEZONE", "Asia/Shanghai")
    try:
        report_tz = ZoneInfo(timezone_name)
    except Exception:
        print("⚠️ REPORT_TIMEZONE 无效，日报使用 UTC", flush=True)
        report_tz = ZoneInfo("UTC")
    while True:
        now = datetime.now(report_tz)
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep(max(1, (target - now).total_seconds()))
        report_date = datetime.now(report_tz).date().isoformat()
        try:
            meta = ARCHIVE_STORE.load("meta", {})
            if meta.get("last_daily_report") == report_date:
                continue
            report_text = daily_report_text(report_date)
            ARCHIVE_STORE.send_report_snapshot(
                report_text, report_title="每日运营汇报 " + report_date
            )
            meta["last_daily_report"] = report_date
            ARCHIVE_STORE.save("meta", meta)
            print("✅ 每日运营汇报已发送：" + report_date, flush=True)
        except Exception as exc:
            print("❌ 每日运营汇报发送失败：", repr(exc), flush=True)


# Only this Telegram account may change storefront category icons.
STOREFRONT_ICON_ADMIN_ID = 7867116527


def _utf16_slice(value, offset, length):
    """Telegram MessageEntity offsets are UTF-16 code units, not Python indexes."""
    raw = str(value or "").encode("utf-16-le")
    part = raw[int(offset) * 2:(int(offset) + int(length)) * 2]
    return part.decode("utf-16-le", errors="strict")


def _resolve_storefront_category_key(label):
    requested = re.sub(r"[\s_-]+", "", str(label or "")).casefold()
    aliases = {
        "chatgpt": {"chatgpt", "ai", "aiplatform"},
        "discord": {"discord"}, "facebook": {"facebook", "fb"},
        "google": {"google"}, "instagram": {"instagram", "ig"},
        "linkedin": {"linkedin"}, "teams": {"teams", "microsoftteams"},
        "microsoft": {"microsoft", "outlook", "微软", "微软信箱", "微软邮箱"},
        "appleid": {"appleid", "apple", "苹果id", "苹果账号"},
        "steam": {"steam"}, "tiktok": {"tiktok", "tk"},
        "xx": {"xx", "twitter", "x"},
    }
    for category in get_storefront_categories():
        key = str(category.get("key") or "")
        alias = str(category.get("alias") or "").strip().lower()
        source_name = str(category.get("name") or "").strip()
        normalized_source = re.sub(r"[\s_-]+", "", source_name).casefold()
        candidates = {normalized_source, alias}
        if "chatgpt" in normalized_source or "ai平台" in normalized_source:
            candidates.add("chatgpt")
        for canonical, names in aliases.items():
            if alias in names or any(candidate in names for candidate in candidates):
                candidates.add(canonical)
        if requested in candidates:
            return key, category
    return None, None


@bot.message_handler(commands=["seticon"])
def set_storefront_category_icon(message):
    """Admin command: /seticon Category emoji-or-custom-emoji; persist to archive meta."""
    sender = getattr(message, "from_user", None)
    if not sender or int(getattr(sender, "id", 0) or 0) != STOREFRONT_ICON_ADMIN_ID:
        return
    if getattr(getattr(message, "chat", None), "type", None) != "private":
        bot.reply_to(message, "请在机器人私聊中使用 /seticon。")
        return
    text_value = str(getattr(message, "text", "") or "")
    if re.fullmatch(r"/seticon(?:@\w+)?\s*", text_value.strip(), re.I):
        names = []
        seen_names = set()
        for item in UPSTREAM_CATEGORIES:
            name = str(item.get("name") or "").strip()
            normalized = re.sub(r"\s+", "", name).casefold()
            if name and normalized not in seen_names:
                seen_names.add(normalized)
                names.append(name)
        listing = "📋 当前商品库真实分类名称（每行一个）\n" + "\n".join(names)
        bot.reply_to(message, listing)
        return
    match = re.match(r"^/seticon(?:@\w+)?\s+(\S+)\s+(.+?)\s*$", text_value, re.S)
    if not match:
        bot.reply_to(message, "用法：/seticon 分类名 图标\n例如：/seticon TikTok 🎵\n发送 /seticon 可查看全部真实分类名称。")
        return
    category_label = match.group(1)
    category_key, category = _resolve_storefront_category_key(category_label)
    if not category_key:
        bot.reply_to(message, "未找到这个商城分类，请检查分类名。")
        return

    icon_argument = match.group(2).strip()
    argument_start = match.start(2)
    argument_start_utf16 = len(text_value[:argument_start].encode("utf-16-le")) // 2
    argument_end_utf16 = len(text_value[:match.end(2)].encode("utf-16-le")) // 2
    custom_entities = []
    for entity in (getattr(message, "entities", None) or []):
        if getattr(entity, "type", "") != "custom_emoji":
            continue
        start = int(getattr(entity, "offset", -1))
        end = start + int(getattr(entity, "length", 0))
        if start >= argument_start_utf16 and end <= argument_end_utf16:
            custom_entities.append(entity)
    if len(custom_entities) > 1:
        bot.reply_to(message, "一次只能设置一个图标，请只发送一个 Emoji。")
        return

    icon_record = {"updated_by": str(STOREFRONT_ICON_ADMIN_ID), "updated_at": datetime.now().astimezone().isoformat()}
    if custom_entities:
        entity = custom_entities[0]
        custom_id = str(getattr(entity, "custom_emoji_id", "") or "").strip()
        if not custom_id:
            bot.reply_to(message, "没有从 Telegram 消息实体中读取到 custom_emoji_id，请重新发送。")
            return
        try:
            entity_text = _utf16_slice(text_value, entity.offset, entity.length)
        except Exception:
            bot.reply_to(message, "高级表情实体位置解析失败，请重新发送图标。")
            return
        if not entity_text.strip():
            bot.reply_to(message, "高级表情内容为空，请重新发送。")
            return
        icon_record.update({"custom_emoji_id": custom_id, "source": "telegram_message_entity", "entity_text": entity_text})
    else:
        native_emoji = re.sub(r"\s+", "", icon_argument)
        emoji_pattern = r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0E\uFE0F\u200D\u20E3]+$"
        if not native_emoji or len(native_emoji) > 16 or not re.fullmatch(emoji_pattern, native_emoji):
            bot.reply_to(message, "请在分类名后发送一个普通 Emoji，或直接使用 Telegram 高级表情。")
            return
        icon_record.update({"emoji": native_emoji, "source": "unicode_emoji"})

    if ARCHIVE_STORE is None:
        bot.reply_to(message, "归档存储尚未就绪，图标未更新，请稍后重试。")
        return
    try:
        meta = ARCHIVE_STORE.load("meta", {})
        icon_map = meta.setdefault("storefront_category_icons", {})
        icon_map[category_key] = icon_record
        ARCHIVE_STORE.save("meta", meta)
        ARCHIVE_STORE.flush("管理员动态更新商城分类图标")
    except Exception as exc:
        print("❌ /seticon 持久化失败：", repr(exc), flush=True)
        bot.reply_to(message, "图标保存失败，尚未确认持久化，请稍后重试。")
        return
    display_name = str(category.get("name") or category_label)
    bot.reply_to(message, f"✅ 已成功更新 {display_name} 的图标")


@bot.message_handler(content_types=["sticker"])
def capture_admin_custom_emoji_sticker(message):
    """Capture custom-emoji sticker metadata sent by the configured admin for storefront mapping."""
    if getattr(getattr(message, "chat", None), "type", None) != "private":
        return
    if int(getattr(getattr(message, "from_user", None), "id", 0) or 0) != STOREFRONT_ICON_ADMIN_ID:
        return
    sticker = getattr(message, "sticker", None)
    if not sticker:
        return
    record = {
        "type": "sticker",
        "message_id": getattr(message, "message_id", None),
        "file_id": getattr(sticker, "file_id", None),
        "file_unique_id": getattr(sticker, "file_unique_id", None),
        "custom_emoji_id": getattr(sticker, "custom_emoji_id", None),
        "emoji": getattr(sticker, "emoji", None),
        "set_name": getattr(sticker, "set_name", None),
        "captured_at": datetime.now().isoformat(),
    }
    print("🎨 ADMIN_EMOJI_CAPTURE " + json.dumps(record, ensure_ascii=False), flush=True)
    if ARCHIVE_STORE is not None:
        meta = ARCHIVE_STORE.load("meta", {})
        captured = meta.setdefault("admin_custom_emoji_capture", [])
        captured.append(record)
        meta["admin_custom_emoji_capture"] = captured[-100:]
        ARCHIVE_STORE.save("meta", meta)
    bot.reply_to(message, "已收到这个图标，正在登记对应信息。")


@bot.message_handler(func=lambda message: bool(getattr(message, "entities", None)) and any(getattr(entity, "type", "") == "custom_emoji" for entity in message.entities), content_types=["text"])
def capture_admin_custom_emoji_entities(message):
    """Capture custom emoji IDs embedded in admin text messages."""
    if getattr(getattr(message, "chat", None), "type", None) != "private":
        return
    if int(getattr(getattr(message, "from_user", None), "id", 0) or 0) != STOREFRONT_ICON_ADMIN_ID:
        return
    records = []
    for entity in message.entities or []:
        if getattr(entity, "type", "") == "custom_emoji":
            records.append({"type": "text_entity", "custom_emoji_id": getattr(entity, "custom_emoji_id", None), "offset": entity.offset, "length": entity.length})
    record = {"type": "text", "message_id": getattr(message, "message_id", None), "text": message.text or "", "entities": records, "captured_at": datetime.now().isoformat()}
    print("🎨 ADMIN_EMOJI_CAPTURE " + json.dumps(record, ensure_ascii=False), flush=True)
    if ARCHIVE_STORE is not None:
        meta = ARCHIVE_STORE.load("meta", {})
        captured = meta.setdefault("admin_custom_emoji_capture", [])
        captured.append(record)
        meta["admin_custom_emoji_capture"] = captured[-100:]
        ARCHIVE_STORE.save("meta", meta)
    bot.reply_to(message, "已收到图标及文字标注，正在登记对应关系。")



_SEARCH_PAGE_SIZE = 8
_SEARCH_SESSIONS = {}
_SEARCH_SESSIONS_LOCK = threading.RLock()


def _search_normalize(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\u3000\W_]+", "", value, flags=re.UNICODE)


def _search_product_text(product):
    parts = []
    def collect(value, depth=0):
        if depth > 4 or value is None:
            return
        if isinstance(value, dict):
            for child in value.values():
                collect(child, depth + 1)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                collect(child, depth + 1)
        elif isinstance(value, (str, int, float, bool)):
            parts.append(str(value))
    collect(product)
    category_key = str(product.get("category_key") or "")
    category = UPSTREAM_CATEGORY_BY_KEY.get(category_key, {})
    parts.extend((str(category.get("name") or ""), str(category.get("alias") or "")))
    # Include common description fields even when nested under a supplier payload.
    for key in ("description", "desc", "detail", "remark", "notice", "specification", "content", "tags"):
        value = product.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


def _search_match_products(keyword):
    needle = _search_normalize(keyword)
    if not needle:
        return []
    scored = []
    for index, product in enumerate(get_all_products()):
        if not isinstance(product, dict):
            continue
        haystack_raw = _search_product_text(product)
        haystack = _search_normalize(haystack_raw)
        if needle in haystack:
            # Prefer matches in product name, then description/category matches.
            name = _search_normalize(product.get("name") or product.get("title") or "")
            score = 0 if needle in name else 1
            scored.append((score, index, product))
            continue
        # Fuzzy fallback: all query characters/tokens must be represented.
        # For CJK, contiguous substring remains the primary precise path.
        tokens = [x for x in re.split(r"[\s,，、|/]+", str(keyword).casefold()) if x]
        if len(needle) >= 2 and tokens and all(_search_normalize(token) in haystack for token in tokens):
            scored.append((2, index, product))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored]


def _search_session_key(chat_id, user_id, keyword):
    seed = f"{chat_id}:{user_id}:{keyword}:{time.time_ns()}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:12]


def _render_search_page(keyword, products, page):
    total = len(products)
    pages = max(1, (total + _SEARCH_PAGE_SIZE - 1) // _SEARCH_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    text = (
        f"🔍 搜索关键词：{html.escape(keyword)}\n"
        f"为您找到 {total} 个相关商品，当前第 {page + 1}/{pages} 页："
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    selected = products[page * _SEARCH_PAGE_SIZE:(page + 1) * _SEARCH_PAGE_SIZE]
    for product in selected:
        product_id = str(product.get("id") or "")
        if not product_id:
            continue
        callback = f"buy_{product_id}_0"
        if len(callback.encode("utf-8")) > 64:
            continue
        label = compact_product_button_label(product)
        kb.row(types.InlineKeyboardButton(text=label, callback_data=callback))
    # Navigation is always one paired row, matching the requested edge-page behavior.
    return text, kb, pages, page


@bot.callback_query_handler(func=lambda call: call.data.startswith("srch_"))
def show_keyword_search_page(call):
    parts = call.data.split("_")
    if len(parts) != 3:
        bot.answer_callback_query(call.id, "搜索分页参数无效，请重新搜索。", show_alert=True)
        return
    token = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        bot.answer_callback_query(call.id, "搜索页码无效，请重新搜索。", show_alert=True)
        return
    with _SEARCH_SESSIONS_LOCK:
        session = _SEARCH_SESSIONS.get(token)
    if not session:
        bot.answer_callback_query(call.id, "搜索结果已过期，请重新发送关键词。", show_alert=True)
        return
    keyword, products, owner_id = session
    if int(getattr(call.from_user, "id", 0) or 0) != int(owner_id):
        bot.answer_callback_query(call.id, "这不是您的搜索结果。", show_alert=True)
        return
    total_pages = max(1, (len(products) + _SEARCH_PAGE_SIZE - 1) // _SEARCH_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    text, kb, _pages, _page = _render_search_page(keyword, products, page)
    selected = products[page * _SEARCH_PAGE_SIZE:(page + 1) * _SEARCH_PAGE_SIZE]
    # Replace placeholder navigation with this session's stable token.
    if page > 0 and page < total_pages - 1:
        left = types.InlineKeyboardButton("⬅️ 上一页", callback_data=f"srch_{token}_{page-1}")
        right = types.InlineKeyboardButton("下一页 ➡️", callback_data=f"srch_{token}_{page+1}")
    elif page == 0 and total_pages > 1:
        left = types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy")
        right = types.InlineKeyboardButton("下一页 ➡️", callback_data=f"srch_{token}_{page+1}")
    elif page > 0:
        left = types.InlineKeyboardButton("⬅️ 上一页", callback_data=f"srch_{token}_{page-1}")
        right = types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy")
    else:
        left = types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy")
        right = types.InlineKeyboardButton("清空搜索", callback_data="ui_buy")
    kb.row(left, right)
    bot.answer_callback_query(call.id)
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)


@bot.message_handler(func=lambda message: True, content_types=["text"])
def fallback_text_message(message):
    """Search all local and upstream products for any ordinary text keyword."""
    query = (message.text or "").strip()
    if not query or query.startswith("/") or query.isdigit():
        return
    if not _action_debounce_allowed(getattr(message.from_user, "id", "unknown"), "product_search", 3.0):
        bot.send_message(message.chat.id, "查询过于频繁，请稍后再试。")
        return
    products = _search_match_products(query)
    if not products:
        bot.send_message(
            message.chat.id,
            f"❌ 未找到与“{html.escape(query)}”相关的商品，请尝试更换关键词（如：苹果、哥伦比亚、邮箱）。",
            parse_mode="HTML",
            reply_markup=get_main_kb(),
        )
        return
    token = _search_session_key(message.chat.id, message.from_user.id, query)
    with _SEARCH_SESSIONS_LOCK:
        _SEARCH_SESSIONS[token] = (query, products, int(message.from_user.id))
        # Bound memory usage; retain only the newest 200 search sessions.
        while len(_SEARCH_SESSIONS) > 200:
            _SEARCH_SESSIONS.pop(next(iter(_SEARCH_SESSIONS)))
    text, kb, pages, page = _render_search_page(query, products, 0)
    if pages > 1:
        kb.row(
            types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
            types.InlineKeyboardButton("下一页 ➡️", callback_data=f"srch_{token}_1"),
        )
    else:
        kb.row(
            types.InlineKeyboardButton("⬅️ 返回", callback_data="ui_buy"),
            types.InlineKeyboardButton("清空搜索", callback_data="ui_buy"),
        )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: True)
def fallback_callback_query(call):
    """Never leave an unknown inline-button callback spinning without feedback."""
    try:
        bot.answer_callback_query(call.id, "该入口暂不可用，请返回主菜单。", show_alert=True)
    except Exception as exc:
        print("⚠️ 未识别按钮回调应答失败：", repr(exc), flush=True)


def install_callback_safety():
    """Acknowledge every inline callback and surface handler exceptions to the user."""
    answered_ids = set()
    original_answer = bot.answer_callback_query

    def tracked_answer(callback_query_id, *args, **kwargs):
        callback_id = str(callback_query_id)
        if callback_id in answered_ids:
            return None
        result = original_answer(callback_query_id, *args, **kwargs)
        answered_ids.add(callback_id)
        return result

    bot.answer_callback_query = tracked_answer

    for entry in bot.callback_query_handlers:
        original_handler = entry.get("function")
        if not callable(original_handler) or getattr(original_handler, "_qingtian_safe", False):
            continue

        def safe_handler(call, *args, _handler=original_handler, **kwargs):
            callback_id = str(call.id)
            answered_ids.discard(callback_id)
            # Acknowledge before counters, database work, or message edits so
            # Telegram clears the button spinner immediately.
            callback_data = str(getattr(call, "data", "") or "")
            user_id = getattr(getattr(call, "from_user", None), "id", "unknown")
            action_bucket = None
            if callback_data.startswith("buy_"):
                action_bucket = "product_detail"
            elif callback_data.startswith(("pay_balance_", "pay_tron_", "pay_alipay_")):
                action_bucket = "purchase"
            if action_bucket and not _action_debounce_allowed(user_id, action_bucket, 3.0):
                try:
                    original_answer(call.id, "操作过于频繁，请稍后再试。", show_alert=True)
                except Exception as answer_exc:
                    print("RATE_LIMIT_ACK_FAILED", type(answer_exc).__name__, flush=True)
                return
            defer_withdraw_ack = callback_data == "wallet_withdraw" or callback_data.startswith("withdraw_paid:")
            if not defer_withdraw_ack:
                try:
                    original_answer(call.id)
                    answered_ids.add(callback_id)
                except Exception as answer_exc:
                    print("⚠️ 按钮即时回执失败：", repr(answer_exc), flush=True)
            try:
                if ARCHIVE_STORE is not None:
                    ARCHIVE_STORE.increment_counter("callback_interactions", 1)
                return _handler(call, *args, **kwargs)
            except Exception as exc:
                print("❌ 内联按钮处理失败：", callback_id, repr(exc), flush=True)
                _notify_admin_critical("Telegram 按钮处理异常", exc, callback_data[:120])
                if callback_id not in answered_ids:
                    try:
                        bot.answer_callback_query(call.id, "操作暂时失败，请重试。", show_alert=True)
                    except Exception as answer_exc:
                        print("⚠️ 按钮异常提示发送失败：", repr(answer_exc), flush=True)
                if getattr(call, "message", None):
                    try:
                        bot.send_message(
                            call.message.chat.id,
                            "本次操作未完成，请返回主菜单重试。",
                            reply_markup=get_main_kb(),
                        )
                    except Exception as send_exc:
                        print("⚠️ 按钮异常补充提示失败：", repr(send_exc), flush=True)
            finally:
                if callback_id not in answered_ids:
                    try:
                        bot.answer_callback_query(call.id)
                    except Exception as answer_exc:
                        print("⚠️ 按钮回执发送失败：", repr(answer_exc), flush=True)
                answered_ids.discard(callback_id)

        safe_handler._qingtian_safe = True
        entry["function"] = safe_handler


def install_message_metrics():
    """Count every handled inbound message; intentionally do not deduplicate users."""
    for entry in bot.message_handlers:
        original_handler = entry.get("function")
        if not callable(original_handler) or getattr(original_handler, "_qingtian_metrics", False):
            continue
        def counted_handler(message, *args, _handler=original_handler, **kwargs):
            if ARCHIVE_STORE is not None:
                try:
                    ARCHIVE_STORE.increment_counter("inbound_messages", 1)
                except Exception as exc:
                    print("⚠️ 接待计数更新失败：", repr(exc), flush=True)
            return _handler(message, *args, **kwargs)
        counted_handler._qingtian_metrics = True
        entry["function"] = counted_handler


install_callback_safety()
install_message_metrics()



def alipay_event_poll_loop():
    """Consume verified Alipay notifications buffered by the public Cloudflare Worker."""
    import requests
    from alipay_listener import process_alipay_notification, process_vmq_notification
    endpoint = os.getenv("ALIPAY_NOTIFY_API_URL", "https://fakabot-alipay-notify.chulingli1988.workers.dev").rstrip("/")
    token = os.getenv("ALIPAY_NOTIFY_TOKEN", "").strip()
    app_id = os.getenv("ALIPAY_APP_ID", "").strip()
    seller_id = os.getenv("ALIPAY_SELLER_ID", "").strip()
    public_key_file = os.getenv("ALIPAY_PUBLIC_KEY_FILE", "/root/.config/fakabot/alipay_sandbox_public.pem")
    if not token:
        print("ℹ️ 支付通知消费端等待内部队列令牌配置", flush=True)
        return
    public_key = open(public_key_file, "rb").read() if os.path.isfile(public_key_file) else b""
    upstream_mock = os.getenv("UPSTREAM_MOCK_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
    sandbox_gateway = "sandbox" in os.getenv("ALIPAY_GATEWAY", "").strip().lower()
    if upstream_mock and sandbox_gateway:
        print("【测试模式已开启】仅沙箱支付宝订单使用固定测试卡密；真实上游采购已拦截", flush=True)
    else:
        print("支付宝通知消费端：测试采购拦截未启用（或非沙箱网关）", flush=True)
    session = requests.Session()
    headers = {"Authorization": "Bearer " + token}
    while True:
        try:
            response = session.get(endpoint + "/internal/events", headers=headers, timeout=15)
            response.raise_for_status()
            payload = response.json()
            for event in payload.get("items", []):
                event_id = str(event.get("event_id") or "")
                try:
                    event_params = event.get("params") or {}
                    if isinstance(event_params, dict) and event_params.get("event_type") == "vmq_android_push":
                        result = process_vmq_notification(
                            event_params,
                            load_orders=lambda: load_json_file(ORDERS_FILE, []),
                            save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
                            bot=bot, admin_id=ADMIN_ID,
                            on_order_paid=lambda row: _reconcile_user_order_count(_order_user_id(row)),
                        )
                    elif isinstance(event_params, dict) and event_params.get("event_type") == "alipay_fund_order_changed":
                        if not (public_key and app_id):
                            raise RuntimeError("支付宝资金单据通知缺少 RSA2 公钥或 AppID 配置")
                        from alipay_listener import process_alipay_fund_order_changed
                        result = process_alipay_fund_order_changed(
                            event_params, public_key, app_id,
                            load_orders=lambda: load_json_file(ORDERS_FILE, []),
                        )
                    else:
                        if not (public_key and app_id and seller_id):
                            raise RuntimeError("支付宝 RSA2 通知缺少公钥、AppID 或 SellerID 配置")
                        result = process_alipay_notification(
                            event_params, public_key, app_id, seller_id,
                            load_orders=lambda: load_json_file(ORDERS_FILE, []),
                            save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
                            bot=bot, admin_id=ADMIN_ID,
                            on_order_paid=lambda row: _reconcile_user_order_count(_order_user_id(row)),
                        )
                    if result.get("ok"):
                        # Persist the confirmed payment/delivery snapshot before ACKing the source event.
                        if ARCHIVE_STORE is not None:
                            ARCHIVE_STORE.flush("支付通知处理完成，ACK 前确认订单归档")
                        ack = session.post(endpoint + "/internal/events/" + event_id + "/ack",
                                           headers=headers, timeout=15)
                        ack.raise_for_status()
                        if result.get("mock_delivery"):
                            print("✅ 内部 Mock 测试事件已校验、模拟发货并 ACK：", result.get("order_id"), flush=True)
                        elif result.get("provider") == "vmq_android_monitor":
                            print("✅ V免签安卓到账通知已验签、唯一订单匹配并 ACK：", result.get("order_id"), flush=True)
                        elif result.get("event_type") == "alipay_fund_order_changed":
                            print("✅ 支付宝资金单据通知已二次验签、记录字段并 ACK（仅日志，不据此发货）：", result.get("payment_ref"), flush=True)
                        else:
                            print("✅ 支付宝异步通知已验签并处理：", result.get("order_id"), flush=True)
                except Exception as event_exc:
                    _notify_admin_critical("支付通知事件处理失败", event_exc, "event retained for retry")
                    detail = str(event_exc)
                    response = getattr(event_exc, "response", None)
                    if response is not None:
                        detail += f" HTTP={response.status_code} BODY={response.text[:240]}"
                    print("⚠️ 支付宝通知处理失败，保留事件等待重试：", type(event_exc).__name__, detail[:400], flush=True)
        except Exception as exc:
            _notify_admin_critical("支付通知队列异常", exc, "event poll loop")
            detail = str(exc)
            response = getattr(exc, "response", None)
            if response is not None:
                detail += f" HTTP={response.status_code} BODY={response.text[:240]}"
            print("⚠️ 支付宝通知队列暂不可用：", type(exc).__name__, detail[:400], flush=True)
        time.sleep(8)


def supplier_delivery_reconcile_loop():
    """Retry delivery retrieval for known upstream order IDs; never repurchase."""
    session_url = os.getenv("SUPPLIER_SESSION_URL", "").strip()
    local_session = "/root/.config/fakabot/dahaoshang_session.json"
    while True:
        try:
            if session_url or os.path.isfile(local_session):
                delivered = reconcile_pending_supplier_deliveries(
                    bot,
                    load_orders=lambda: load_json_file(ORDERS_FILE, []),
                    save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
                    admin_id=ADMIN_ID,
                )
                if delivered:
                    print(f"✅ 自动补发已完成：{delivered} 笔", flush=True)
        except Exception as exc:
            print("⚠️ 上游延迟交付核对暂时失败：", type(exc).__name__, flush=True)
            _notify_admin_critical("大号商交付核对失败", exc, "delivery reconcile")
        time.sleep(60)



def settle_usdt_wallet_topup(target, orders, transfer):
    """Atomically credit wallet balance and persist matching paid order in private Telegram archive."""
    if ARCHIVE_STORE is None:
        raise RuntimeError("Telegram 归档存储未就绪，拒绝入账")
    uid = str(target.get("user_id") or "")
    order_id = str(target.get("order_id") or "")
    raw = str(transfer.get("amount_raw") or "")
    if not uid.isdigit() or not order_id or not raw.isdigit():
        raise RuntimeError("充值订单或链上金额字段无效")
    amount = Decimal(raw) / Decimal("1000000")
    if amount < Decimal("10") or raw != str(target.get("amount_raw")):
        raise RuntimeError("链上金额与充值订单不一致，拒绝入账")
    users = load_json_file(USERS_FILE, {})
    user = users.setdefault(uid, {"balance": 0.0})
    ledger = user.setdefault("topup_ledger", [])
    if any(isinstance(row, dict) and str(row.get("order_id")) == order_id for row in ledger):
        raise RuntimeError("该充值订单已存在入账流水，拒绝重复入账")
    user["balance"] = round(float(user.get("balance", 0) or 0) + float(amount), 6)
    ledger.append({
        "order_id": order_id, "amount_usdt": format(amount, ".6f"),
        "amount_cny": format(price_cny(amount), ".2f"),
        "txid": str(transfer.get("txid") or ""),
        "created_at": datetime.now().astimezone().isoformat(),
    })
    old_users = load_json_file(USERS_FILE, {})
    old_orders = load_json_file(ORDERS_FILE, [])
    try:
        ARCHIVE_STORE.save_many({"users": users, "orders": orders})
        ARCHIVE_STORE.flush("USDT 自定义充值到账与余额入账")
    except Exception:
        ARCHIVE_STORE.save_many({"users": old_users, "orders": old_orders})
        try:
            ARCHIVE_STORE.flush("回滚未能确认归档的 USDT 充值")
        except Exception:
            pass
        raise
    try:
        bot.send_message(
            int(uid),
            "✅ <b>充值成功通知</b>\n\n"
            f"💰 <b>到账金额</b>：¥{price_cny(amount):.2f}\n"
            "💳 <b>充值方式</b>：USDT（TRC20）\n"
            f"🔢 <b>订单号</b>：<code>{html.escape(order_id)}</code>\n"
            f"👛 <b>当前余额</b>：¥{price_cny(Decimal(str(user.get('balance', 0) or 0))):.2f}\n\n"
            "<i>感谢您的支持，祝您购物愉快！</i>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        print("USDT 充值到账私聊通知失败：", type(exc).__name__, flush=True)
    try:
        bot.send_message(
            ARCHIVE_CHAT_ID,
            "💰 <b>USDT 自定义充值到账归档</b>\n"
            f"用户：<code>{uid}</code>\n订单：<code>{html.escape(order_id)}</code>\n"
            f"金额：<b>{format(amount, '.6f')} USDT</b>（¥{price_cny(amount):.2f}）\n"
            f"TX：<code>{html.escape(str(transfer.get('txid') or ''))}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        print("USDT 充值到账频道通知失败：", type(exc).__name__, flush=True)


if __name__ == "__main__":
    print("🤖 晴天号铺机器人启动中...", flush=True)
    try:
        try:
            catalog_sync = refresh_supplier_catalog(force=True)
            print(
                "✅ 商品目录已从大号商 API 全量同步："
                f"{catalog_sync['category_count']} 个分类 / {catalog_sync['product_count']} 个商品",
                flush=True,
            )
        except Exception as catalog_exc:
            print("⚠️ 大号商 API 全量同步暂不可用，继续使用最近一次成功的本地缓存：", repr(catalog_exc), flush=True)
            _notify_admin_critical("大号商目录同步失败", catalog_exc, "startup catalog refresh; cached catalog retained")
        start_supplier_catalog_refresh(interval=300, on_error=lambda exc: _notify_admin_critical("大号商目录同步失败", exc, "background catalog refresh"))
        import threading
        from telegram_storage import TelegramArchiveStore
        legacy_paths = {
            "users": USERS_FILE,
            "orders": ORDERS_FILE,
            "withdrawals": WITHDRAWALS_FILE,
        }
        ARCHIVE_STORE = TelegramArchiveStore(
            bot=bot,
            chat_id=ARCHIVE_CHAT_ID,
            legacy_paths=legacy_paths,
        )
        archive_ready = False
        try:
            ARCHIVE_STORE.initialize()
            archive_ready = True
            try:
                from products.local_inventory import set_critical_error_handler
                set_critical_error_handler(lambda exc: _notify_admin_critical("本地库存文件锁异常", exc, "local inventory lock"))
            except Exception as inventory_alert_setup_exc:
                _notify_admin_critical("本地库存告警初始化失败", inventory_alert_setup_exc, "set inventory error handler")
            print("✅ 私密 Telegram 归档已连接，业务数据不再写入服务器磁盘", flush=True)
        except Exception as archive_init_exc:
            _notify_admin_critical("Telegram 归档初始化失败", archive_init_exc, "startup archive initialize")
            # Business handlers depend on the archive store. Polling without it
            # would make /start and every data-backed button fail silently.
            print("❌ Telegram 归档初始化失败，停止启动并交由 systemd 重试：", repr(archive_init_exc), flush=True)
            raise RuntimeError("业务数据归档未就绪，拒绝启动不完整的机器人实例") from archive_init_exc

        # Startup is intentionally silent: no channel or user broadcast.
        # Optional Premium user-account bridge: Bot API remains primary.
        # Missing user credentials never block the storefront or payment listener.
        try:
            import telegram_user_bridge
            user_bridge_ready, user_bridge_status = telegram_user_bridge.initialize()
            if user_bridge_ready:
                print("✅ Telegram Premium 用户账号桥接已连接（仅作 Bot API 能力补充）", flush=True)
            else:
                print("ℹ️ Telegram 用户账号桥接暂未授权：", user_bridge_status, flush=True)
        except Exception as bridge_exc:
            print("⚠️ Telegram 用户账号桥接不可用，不影响机器人主流程：", type(bridge_exc).__name__, flush=True)

        if archive_ready:
            try:
                start_webhook_server(
                    load_orders=lambda: load_json_file(ORDERS_FILE, []),
                    save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
                    flush_orders=lambda: ARCHIVE_STORE.flush("支付宝网关到账 Webhook"),
                )
                audit_channel = os.getenv("PAYMENT_AUDIT_CHANNEL_ID", "").strip() or ARCHIVE_CHAT_ID
                threading.Thread(
                    target=gateway_order_worker_loop,
                    args=(bot,
                          lambda: load_json_file(ORDERS_FILE, []),
                          lambda rows: save_json_file(ORDERS_FILE, rows),
                          lambda: ARCHIVE_STORE.flush("支付宝网关订单处理")),
                    kwargs={"admin_id": ADMIN_ID, "audit_channel_id": audit_channel,
                            "on_order_paid": lambda row: _reconcile_user_order_count(_order_user_id(row))},
                    name="payment-gateway-order-worker", daemon=True,
                ).start()
                print("✅ 无状态支付网关 Webhook 已启动（127.0.0.1:8011），60 秒订单过期与发货归档任务已启动", flush=True)
            except Exception as gateway_exc:
                print("⚠️ 无状态支付网关接收端未启动：", type(gateway_exc).__name__, flush=True)
            try:
                PAYMENT_WATCHER = PaymentWatcher(
                    load_orders=lambda: load_json_file(ORDERS_FILE, []),
                    save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
                    on_payment=lambda order_id, transfer: handle_confirmed_payment(
                        bot, order_id, transfer,
                        load_orders=lambda: load_json_file(ORDERS_FILE, []),
                        save_orders=lambda rows: save_json_file(ORDERS_FILE, rows),
                        admin_id=ADMIN_ID,
                        settle_topup=settle_usdt_wallet_topup,
                        on_order_paid=lambda row: _reconcile_user_order_count(_order_user_id(row)),
                    ),
                )
                PAYMENT_WATCHER.start()
                print("✅ TRC20 USDT 付款监听已启动", flush=True)
            except PaymentConfigurationError as exc:
                PAYMENT_WATCHER = None
                print("⚠️ 链上收款未启动：", str(exc), flush=True)
            except Exception as exc:
                PAYMENT_WATCHER = None
                print("⚠️ 链上收款启动失败，不影响机器人菜单：", repr(exc), flush=True)
            try:
                archive_source_bundle()
            except Exception as archive_exc:
                print("❌ 源码归档上传失败，不阻断机器人消息轮询：", repr(archive_exc), flush=True)
                _notify_admin_critical("源码归档上传失败", archive_exc, "archive source bundle")
            threading.Thread(target=ARCHIVE_STORE.flush_loop, args=(5,), kwargs={"on_error": lambda exc: _notify_admin_critical("归档快照写入失败", exc, "background archive flush")}, name="archive-flush", daemon=True).start()
            threading.Thread(target=supplier_delivery_reconcile_loop, name="supplier-delivery-reconcile", daemon=True).start()
            threading.Thread(target=alipay_event_poll_loop, name="alipay-notification-poller", daemon=True).start()
            threading.Thread(target=referral_reward_sync_loop, name="referral-reward-sync", daemon=True).start()
            threading.Thread(target=three_hour_report_loop, name="three-hour-operations-report", daemon=True).start()
            threading.Thread(target=daily_report_loop, name="daily-operations-report", daemon=True).start()
        try:
            bot.remove_webhook()
            print("✅ Webhook 已清理，保留待处理更新", flush=True)
        except Exception as webhook_exc:
            print("⚠️ Webhook 清理失败，继续尝试 long polling：", repr(webhook_exc), flush=True)
        print("📡 开始 long polling", flush=True)
        bot.infinity_polling(skip_pending=False, timeout=30, long_polling_timeout=30)
    except KeyboardInterrupt:
        print("🛑 收到停止信号，机器人退出", flush=True)
    except Exception as exc:
        import traceback
        _notify_admin_critical("机器人主轮询异常", exc, "infinity_polling")
        print("❌ 机器人 polling 异常：", repr(exc), flush=True)
        traceback.print_exc()
        raise

# -*- coding: utf-8 -*-
import telebot, time, json, os, re, html, io, hashlib, zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telebot import types
from products.catalog import (
    APPLE_PRODUCTS, TG_PRODUCTS, INSTAGRAM_PRODUCTS, AI_PRODUCTS, NODE_PRODUCTS,
    PRODUCT_CATALOGS, UPSTREAM_CATEGORIES, UPSTREAM_CATEGORY_BY_KEY, get_all_products,
)
from payment_flow import PaymentConfigurationError, PaymentWatcher, create_payment_order, create_payment_qr, handle_confirmed_payment, reconcile_pending_supplier_deliveries

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
if not TOKEN:
    raise RuntimeError("缺少环境变量 BOT_TOKEN，请在服务器环境中配置机器人 Token。")
# Keep the default worker pool small for low-memory VPS instances.
_worker_threads = max(2, min(8, int(os.getenv("BOT_WORKER_THREADS", "4"))))
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=_worker_threads)
USERS_FILE = "/root/fakabot/users.json"  # logical key only; business data is stored in Telegram

EMOJI = {
    "usdt": "5001485391807645910", "tg": "5001558608115140737", "gear": "6244525025500601411",
    "gift": "6246956045709612135", "diamond": "6244585885187184999", "star": "6244603610517215237",
    "apple": "5001358570013329603", "ai": "6289722534019475465", "video": "5001404418789214347",
    "node": "6244694135542913498"
}


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

def premium_button(text, **kwargs):
    """统一给所有内联按钮配置 Telegram Premium 自定义表情图标。"""
    label = clean_button_text(text)
    # Detect category icon from the original label before decorative emoji are stripped.
    probe = (str(text) + " " + str(kwargs.get("callback_data", ""))).lower()
    if any(x in probe for x in ("充值", "支付", "usdt", "pay_")):
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
    elif "🌐" in probe or any(x in probe for x in ("节点", "node")):
        key = "node"
    elif any(x in probe for x in ("邀请", "奖励")):
        key = "gift"
    elif any(x in probe for x in ("客服", "帮助", "返回", "更多")):
        key = "gear"
    elif any(x in probe for x in ("商城", "账号铺", "商品")):
        key = "diamond"
    elif any(x in probe for x in ("订单", "记录")):
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
        key = "gift" if any(x in emoji for x in ("🎁", "💰", "💸")) else "diamond"
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
ORDERS_FILE = "/root/fakabot/orders.json"

ARCHIVE_CHAT_ID = os.getenv("TELEGRAM_ARCHIVE_CHAT_ID", "-1004459109788").strip()
ARCHIVE_STORE = None
PAYMENT_WATCHER = None

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
        raise RuntimeError("Telegram 归档存储尚未初始化，拒绝写入本地业务数据")
    ARCHIVE_STORE.save(_archive_key(path), value)

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
        kb.add(premium_button("Invited friends & activity", callback_data="invite_friends_0"))
        kb.add(premium_button("Back to main menu", callback_data="ui_buy"))
    else:
        kb.add(premium_button("好友名单与消费明细", callback_data="invite_friends_0"))
        kb.add(premium_button("返回主菜单", callback_data="ui_buy"))
    return kb

def get_invite_friends_kb(page, pages, language="zh"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if language == "en":
        kb.row(premium_button("Previous", callback_data=f"invite_friends_{max(0,page-1)}"),
               premium_button("Next", callback_data=f"invite_friends_{min(pages-1,page+1)}"))
        kb.add(premium_button("Back to referral overview", callback_data="ui_invite"))
    else:
        kb.row(premium_button("上一页", callback_data=f"invite_friends_{max(0,page-1)}"),
               premium_button("下一页", callback_data=f"invite_friends_{min(pages-1,page+1)}"))
        kb.add(premium_button("返回邀请奖励", callback_data="ui_invite"))
    return kb

def get_wallet_kb(language="zh"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if language == "en":
        kb.row(
            premium_button("Bind / change wallet", callback_data="wallet_bind"),
            premium_button("Withdraw USDT", callback_data="wallet_withdraw")
        )
        kb.row(
            premium_button("Referral rewards", callback_data="ui_invite"),
            premium_button("Back to funds", callback_data="ui_recharge")
        )
    else:
        kb.row(
            premium_button("绑定 / 更换钱包", callback_data="wallet_bind"),
            premium_button("申请提现", callback_data="wallet_withdraw")
        )
        kb.row(
            premium_button("邀请奖励", callback_data="ui_invite"),
            premium_button("返回资金中心", callback_data="ui_recharge")
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
    if any(value.startswith("buy_") for value in callbacks) and any(
        value.startswith("products_") for value in callbacks
    ):
        return kb
    buttons = [button for row in kb.keyboard for button in row]
    if not buttons:
        return kb
    if len(buttons) % 2:
        last_button = buttons[-1]
        last_text = str(getattr(last_button, "text", "") or "").lower()
        is_existing_back = ("返回" in last_text or "back" in last_text)
        if not is_existing_back:
            try:
                back_button = types.InlineKeyboardButton(
                    text="返回主菜单",
                    callback_data="ui_buy",
                    icon_custom_emoji_id=EMOJI.get("gear")
                )
            except TypeError:
                back_button = types.InlineKeyboardButton(
                    text="返回主菜单", callback_data="ui_buy"
                )
            buttons.append(back_button)
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    # pyTelegramBotAPI exposes keyboard as a read-only property; mutate its list in place.
    kb.keyboard[:] = rows
    return kb

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


_original_send_message = bot.send_message
def _send_message_with_premium_emoji(*args, **kwargs):
    if len(args) >= 2:
        args = (args[0], premiumize_text(args[1]), *args[2:])
    elif "text" in kwargs:
        kwargs["text"] = premiumize_text(kwargs["text"])
    if kwargs.get("reply_markup") is not None:
        kwargs["reply_markup"] = enforce_two_column_keyboard(kwargs["reply_markup"])
    kwargs.setdefault("parse_mode", "HTML")
    return _call_with_premium_fallback(_original_send_message, args, kwargs, 1)
bot.send_message = _send_message_with_premium_emoji

_original_edit_message_text = bot.edit_message_text
def _edit_message_text_two_columns(*args, **kwargs):
    if kwargs.get("reply_markup") is not None:
        kwargs["reply_markup"] = enforce_two_column_keyboard(kwargs["reply_markup"])
    if len(args) >= 3:
        args = (*args[:2], premiumize_text(args[2]), *args[3:])
    elif "text" in kwargs:
        kwargs["text"] = premiumize_text(kwargs["text"])
    kwargs.setdefault("parse_mode", "HTML")
    return _call_with_premium_fallback(_original_edit_message_text, args, kwargs, 2)
bot.edit_message_text = _edit_message_text_two_columns

def safe_edit(bot, chat_id, msg_id, text, kb=None):
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                              text=premiumize_text(text), parse_mode="HTML",
                              reply_markup=enforce_two_column_keyboard(kb))
    except Exception as exc:
        print("⚠️ 消息更新失败：", repr(exc), flush=True)
        if "message is not modified" not in str(exc).lower():
            try:
                bot.send_message(chat_id, text, parse_mode="HTML",
                                 reply_markup=enforce_two_column_keyboard(kb))
            except Exception as send_exc:
                print("❌ 编辑失败后的补发也失败：", repr(send_exc), flush=True)

def get_main_kb():
    """主界面：账号与节点合并为账号商城，严格两列三行。"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        premium_button("账号商城", callback_data="cat_account"),
        premium_button("历史订单", callback_data="ui_orders")
    )
    kb.row(
        premium_button("邀请奖励", callback_data="ui_invite"),
        premium_button("充值", callback_data="ui_recharge")
    )
    kb.row(
        premium_button("使用帮助", callback_data="ui_help"),
        premium_button("更多服务", callback_data="more_services")
    )
    return kb
def get_more_kb(language="zh"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if language == "en":
        kb.row(
            premium_button("Node Community", url="https://t.me/qingtian_vip"),
            premium_button("Proxy Channel", url="https://t.me/jiedian888")
        )
        kb.row(
            premium_button("Account Store News", url="https://t.me/qingtian_tutorial"),
            premium_button("Contact Support", callback_data="show_support")
        )
        kb.row(
            premium_button("Back", callback_data="ui_buy"),
            premium_button("中文", callback_data="lang_zh")
        )
    else:
        kb.row(
            premium_button("节点交流群", url="https://t.me/qingtian_vip"),
            premium_button("代理频道", url="https://t.me/jiedian888")
        )
        kb.row(
            premium_button("账号铺频道", url="https://t.me/qingtian_tutorial"),
            premium_button("联系客服", callback_data="show_support")
        )
        kb.row(
            premium_button("返回主菜单", callback_data="ui_buy"),
            premium_button("English", callback_data="lang_en")
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

def get_account_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        premium_button(
            f"{category.get('display_icon', '🛍️')} {category.get('name', '商品')} ({category.get('product_count', 0)})",
            callback_data=f"list_{category['key']}"
        )
        for category in UPSTREAM_CATEGORIES
    ]
    for index in range(0, len(buttons), 2):
        kb.row(*buttons[index:index + 2])
    kb.row(
        premium_button("🌐 节点套餐", callback_data="list_node"),
        premium_button("返回主菜单", callback_data="ui_buy")
    )
    return kb

def get_brand_group_kb(group_id):
    group = BRAND_GROUPS[group_id]
    kb = types.InlineKeyboardMarkup(row_width=2)
    for brand_index, (brand_name, brand_emoji) in enumerate(group["brands"]):
        # Brand-specific Premium emoji IDs are intentionally not fabricated.
        kb.add(premium_button(f"{brand_emoji} {brand_name}", callback_data=f"brand_{group_id}_{brand_index}"))
    kb.row(premium_button("返回账号商城", callback_data="cat_account"))
    return kb

def get_product_catalog(cat_id):
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
    pages = max(1, (total + 7) // 8)
    page = max(0, min(int(page), pages - 1))
    titles = {
        "apple": ("🍎", "苹果 ID 专区", "覆盖不同地区、账号年限及使用需求。请先阅读商品名称和适用说明，再选择对应商品。"),
        "tg": ("✈️", "TG 账号专区", "提供不同国家区号的 Telegram 账号。购买前请核对国家区号及账号用途。"),
        "instagram": ("📸", "Instagram / Threads 账号专区", "Instagram（IG/INS）及 Threads 商品目录。请根据商品名称和登录配置选择。"),
        "ai": ("🤖", "AI 账号专区", "汇集 AI 工具账号选项。请根据服务类型和使用需求选择。"),
        "social": ("👥", "社交账号专区", "选择需要的社交平台账号类别。"),
        "email": ("✉️", "邮箱账号专区", "选择需要的邮箱服务类别。"),
        "video": ("🎬", "视频账号专区", "选择需要的视频平台账号类别。"),
        "communication": ("💬", "通讯账号专区", "选择需要的通讯平台账号类别。"),
        "commerce": ("🛒", "电商账号专区", "选择需要的电商平台账号类别。"),
        "node": ("🌐", "节点套餐专区", "按流量和使用需求选择套餐；购买前请确认套餐规格。"),
    }
    upstream_category = UPSTREAM_CATEGORY_BY_KEY.get(cat_id)
    if upstream_category:
        titles[cat_id] = (
            upstream_category.get("display_icon", "🛍️"),
            upstream_category.get("name", "商品专区"),
            "大号商实时商品目录。价格按人民币采购价 ÷ 4 换算为 USDT 展示；上游自动采购尚未完成验证，当前仅供浏览。"
        )
    icon, title, intro = titles.get(cat_id, ("🛍️", "商品专区", "请选择适合您的商品。"))
    first = page * 8 + 1 if total else 0
    last = min((page + 1) * 8, total)
    item_label = "平台入口" if cat_id in BRAND_GROUPS else "商品"
    return (
        f"{icon} <b>{title}</b>\n"
        f"📦 {item_label}数量：<b>{total} 种</b>\n"
        f"📄 当前页：<b>{page + 1}/{pages}</b> · 显示第 <b>{first}-{last}</b> 项\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{intro}\n\n"
        "👇 <b>请选择下方项目：</b>"
    )


def get_products_kb(cat_id, page=0):
    products = get_product_catalog(cat_id)
    total = len(products)
    pages = max(1, (total + 7) // 8)
    page = max(0, min(int(page), pages - 1))
    selected = products[page * 8:(page + 1) * 8]
    # 商品列表单列展示；品牌导航按两列排列，避免品牌入口占满整页。
    kb = types.InlineKeyboardMarkup(row_width=2 if cat_id in BRAND_GROUPS else 1)
    product_icons = {
        "apple": "🍎", "tg": "✈️", "instagram": "📸", "ai": "🤖", "social": "👥",
        "email": "✉️", "video": "🎬", "communication": "💬",
        "commerce": "🛒", "node": "🌐"
    }
    upstream_category = UPSTREAM_CATEGORY_BY_KEY.get(cat_id)
    product_icon = (
        upstream_category.get("display_icon", "🛍️")
        if upstream_category else product_icons.get(cat_id, "🛍️")
    )
    if cat_id in BRAND_GROUPS:
        brand_buttons = [
            premium_button(
                product["name"],
                callback_data=f"brand_{cat_id}_{product['brand_index']}"
            )
            for product in selected
        ]
        for index in range(0, len(brand_buttons), 2):
            kb.row(*brand_buttons[index:index + 2])
    else:
        for product_index, product in enumerate(selected, start=page * 8 + 1):
            callback = f"buy_{product['id']}_{page}"
            product_name = str(product.get("name", "商品"))
            if len(product_name) > 52:
                product_name = product_name[:49] + "…"
            label = f"{product_icon} {product_index}. {product_name} | {product['price']} U"
            kb.add(premium_button(label, callback_data=callback))
    if not selected:
        kb.add(premium_button("暂无已登记商品", callback_data="show_support"))
    previous_page = max(0, page - 1)
    next_page = min(pages - 1, page + 1)
    kb.row(
        premium_button("上一页", callback_data=f"products_{cat_id}_{previous_page}"),
        premium_button("下一页", callback_data=f"products_{cat_id}_{next_page}"),
        premium_button("返回", callback_data="cat_account")
    )
    return kb

def get_back_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(premium_button("🔙 返回主菜单", callback_data="ui_buy"))
    return kb

def generate_main_text(user, uid):
    """使用此前确认的晴天号铺欢迎页文案与 Telegram Premium 自定义表情。"""
    balance = float(user.get("balance", 0) or 0)
    orders = int(user.get("orders_count", 0) or 0)
    invited = int(user.get("invited_count", 0) or 0)
    bonus = float(user.get("invite_bonus", 0) or 0)
    return (
        f'{premium_emoji("star", "✨")} <b>欢迎光临晴天号铺。</b>\n\n'
        f'{premium_emoji("diamond", "📌")} <b>您的专属账户信息</b>\n'
        f'├ {premium_emoji("gear", "🆔")} <b>用户 ID：</b> <code>{uid}</code>\n'
        f'├ {premium_emoji("usdt", "💰")} <b>账户余额：</b> <b>{balance:.2f}</b> USDT\n'
        f'├ {premium_emoji("apple", "🛒")} <b>订单记录：</b> {orders} 单\n'
        f'├ {premium_emoji("tg", "👥")} <b>邀请用户：</b> {invited} 位\n'
        f'└ {premium_emoji("gift", "🎁")} <b>邀请奖励：</b> 累计 {bonus:.2f} USDT\n\n'
        f'━━━━━━━━━━━━━━━━\n'
        f'{premium_emoji("star", "🛒")} <b>自助选购</b>：点击下方按钮即可挑选。'
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
            bind_referrer(message.from_user.id, payload[1])
        response_text = generate_main_text(user, message.from_user.id)
        bot.send_message(message.chat.id, response_text, parse_mode="HTML", reply_markup=get_main_kb())
    except Exception as exc:
        print("❌ /start 处理失败：", repr(exc), flush=True)
        bot.send_message(message.chat.id, "☀️ 晴天号铺正在恢复数据服务，请稍后重试。基础在线检测：/ping")

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
    user = get_user(call.from_user.id)
    language = user.get("language", "zh")
    if language == "en":
        text = (
            "💬 <b>OFFICIAL CUSTOMER SUPPORT</b>\n"
            "<i>Personal assistance from the QingTian support account.</i>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "👤 <b>Support account preview</b>\n"
            "Account: <b>@qingtian_Boss</b>\n"
            "Channel: Telegram direct message\n\n"
            "📌 <b>What we can help with</b>\n"
            "• Product details and pre-purchase questions\n"
            "• Order status and delivery assistance\n"
            "• Basic usage guidance and after-sales questions\n\n"
            "Please include your order number and a short description when contacting support. Never share your password or verification code."
        )
        contact_label, back_label = "Open support chat", "Back to services"
    else:
        text = (
            "💬 <b>晴天官方人工客服</b>\n"
            "<i>为您提供清晰、及时的订单与商品咨询支持。</i>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "👤 <b>客服账号预览</b>\n"
            "账号名称：<b>晴天客服</b>\n"
            "Telegram：<code>@qingtian_Boss</code>\n"
            "沟通方式：一对一私信\n\n"
            "📌 <b>可协助处理</b>\n"
            "• 商品规格、库存及购买前咨询\n"
            "• 订单状态、支付与发货问题\n"
            "• 商品使用指引及售后问题\n\n"
            "联系时建议附上订单编号和问题描述，以便客服更快定位。请勿向任何人提供账户密码或短信验证码。"
        )
        contact_label, back_label = "私信联系人工客服", "返回更多服务"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(premium_button(contact_label, url="https://t.me/qingtian_Boss"))
    kb.add(premium_button(back_label, callback_data="more_services"))
    kb.add(premium_button("中文" if language == "en" else "English", callback_data="lang_zh" if language == "en" else "lang_en"))
    try:
        support = bot.get_chat("@qingtian_Boss")
        photos = bot.get_user_profile_photos(support.id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            bot.send_photo(call.message.chat.id, file_id, caption=text, parse_mode="HTML", reply_markup=kb)
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
            return
    except Exception:
        pass
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)

@bot.callback_query_handler(func=lambda call: call.data == "cat_account")
def show_account_menu(call):
    bot.answer_callback_query(call.id)
    account_shop_text = (
        "🛒 <b>账号专属商城</b>\n\n"
        "🏆 <b>源头直供，全网底价</b>\n"
        "✨ 专注提供全球优质账号，全部现货秒发。\n"
        "🛡️ <b>安全保障</b>：售后质保，提供详细操作指南。\n"
        "⚡ <b>自动发货</b>：24小时无人值守，即买即用。\n\n"
        "📚 <b>商品分类：共 9 大类</b>\n"
        "包含苹果 ID、TG、AI、社交、邮箱、视频、通讯、电商及节点套餐。\n\n"
        "👇 请选择您需要的账号类型："
    )
    safe_edit(bot, call.message.chat.id, call.message.message_id, account_shop_text, get_account_kb())

@bot.callback_query_handler(func=lambda call: call.data == "cat_node")
def show_node_menu(call):
    bot.answer_callback_query(call.id)
    safe_edit(bot, call.message.chat.id, call.message.message_id, "🚀 <b>节点铺</b>\n👇 <i>请选择：</i>", get_products_kb("node"))

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
        kb.row(
            premium_button("返回社交账号分类", callback_data="brands_social"),
            premium_button("返回账号商城", callback_data="cat_account")
        )
        safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)
        return

    # Non-social brand groups keep their own explicit landing page.
    text = (
        f"🏷️ <b>{html.escape(brand_name)}</b>\n\n"
        "该平台目前尚未录入可核对的商品明细。\n"
        "为避免展示错误货号或价格，请联系客服确认上架信息。"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(premium_button("联系客服", url="https://t.me/qingtian_Boss"), premium_button("返回品牌分类", callback_data=f"brands_{group_id}"))
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("list_"))
def show_category_list(call):
    bot.answer_callback_query(call.id)
    cat_id = call.data.replace("list_", "")
    titles = {
        "apple": f"🍎 <b>苹果ID 专区（{len(APPLE_PRODUCTS)} 种账号）</b>",
        "tg": f"✈️ <b>TG账号 专区（{len(TG_PRODUCTS)} 种账号）</b>",
        "ai": f"🤖 <b>AI 专区（{len(AI_PRODUCTS)} 种账号）</b>",
        "video": "🎬 <b>视频账号</b>",
        "node": f"🌐 <b>节点套餐（{len(NODE_PRODUCTS)} 种套餐）</b>"
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
    if cat_id not in PRODUCT_CATALOGS and cat_id not in BRAND_GROUPS:
        bot.answer_callback_query(call.id, "商品分类不存在。", show_alert=True)
        return
    safe_edit(
        bot, call.message.chat.id, call.message.message_id,
        get_product_page_text(cat_id, page), get_products_kb(cat_id, page)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def buy_item(call):
    parts = call.data.split("_")
    prod_id = parts[1] if len(parts) > 1 else ""
    try:
        return_page = max(0, int(parts[2])) if len(parts) > 2 else 0
    except ValueError:
        return_page = 0
    all_products = get_all_products()
    p = next((x for x in all_products if x["id"] == prod_id), None)
    if not p:
        bot.answer_callback_query(call.id, "商品不存在或已下架，请刷新商品列表。", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    if p.get("is_upstream"):
        detail = (
            f"{p.get('display_icon', '🛍️')} <b>{html.escape(p['name'])}</b>\\n"
            f"💴 上游标价：<code>{p.get('price_cny', '—')} CNY</code>\\n"
            f"💰 展示售价：<code>{p['price']} USDT</code>\\n"
            f"📦 上游库存快照：<code>{p.get('supplier_stock', 0)}</code>\\n"
            f"🆔 货号：<code>{html.escape(str(p.get('supplier_product_id', prod_id)))}</code>\\n\\n"
            "⚠️ 商品已从大号商公开商品接口抓取；自动登录采购和卡密交付尚未验证，暂不收款。"
        )
        back_cat = p.get("category_key", "up0")
        kb = types.InlineKeyboardMarkup(row_width=2)
        if p.get("supplier_url"):
            kb.add(premium_button("查看上游商品", url=p["supplier_url"]))
        kb.add(premium_button("联系客服", url="https://t.me/qingtian_Boss"))
        kb.add(premium_button("返回列表", callback_data=f"products_{back_cat}_{return_page}"))


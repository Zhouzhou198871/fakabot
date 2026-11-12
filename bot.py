# -*- coding: utf-8 -*-
import telebot, time, json, os, re, html, io, hashlib, zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telebot import types

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
if not TOKEN:
    raise RuntimeError("缺少环境变量 BOT_TOKEN，请在服务器环境中配置机器人 Token。")
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=max(8, min(32, int(os.getenv("BOT_WORKER_THREADS", "16")))))
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
APPLE_PRODUCTS = [
    {"id": "a1", "name": "美区共享号｜已购小火箭", "price": "2.78"},
    {"id": "a2", "name": "香港｜半年号｜完整密保", "price": "2.78"},
    {"id": "a3", "name": "美区｜半年号｜完整密保", "price": "2.78"},
    {"id": "a4", "name": "韩国｜半年号｜完整密保", "price": "2.78"},
    {"id": "a5", "name": "日本｜半年号｜完整密保", "price": "2.78"},
    {"id": "a6", "name": "日本老号｜完整密保｜独享", "price": "5.21"},
    {"id": "a7", "name": "马来西亚老号｜完整密保｜独享", "price": "5.21"},
    {"id": "a8", "name": "台湾｜独享｜仅限 App Store", "price": "5.56"},
    {"id": "a9", "name": "美国 IC｜可激活 iCloud", "price": "8.06"},
    {"id": "a10", "name": "独享下载号｜iCloud 后缀", "price": "8.25"},
    {"id": "a11", "name": "1–5 年老号｜稳定 IC", "price": "9.70"},
    {"id": "a12", "name": "2010–2020 年老号", "price": "10.39"},
    {"id": "a13", "name": "美国免税区｜可激活 iCloud", "price": "10.58"},
    {"id": "a14", "name": "完整密保｜独享下载号", "price": "10.90"},
    {"id": "a15", "name": "冷门国家｜独享下载号", "price": "11.09"},
    {"id": "a16", "name": "台湾 13–19 年私人号｜禁 IM", "price": "12.11"},
    {"id": "a17", "name": "美国 13–19 年私人号｜免税区", "price": "12.11"},
    {"id": "a18", "name": "10 个月以上｜稳定 IC", "price": "12.48"},
    {"id": "a19", "name": "美国｜10 年以上", "price": "13.25"},
    {"id": "a20", "name": "5–10 年老 IC", "price": "13.68"},
    {"id": "a21", "name": "15 年以上珍藏老号", "price": "19.24"},
    {"id": "a22", "name": "15 年以上老 IC", "price": "20.63"},
    {"id": "a23", "name": "已购小火箭（Shadowrocket）", "price": "24.30"},
    {"id": "a24", "name": "美国 5–10 年私人号｜小火箭", "price": "61.11"},
    {"id": "a25", "name": "美国 15 年以上私人号｜小火箭", "price": "105.08"}
]

TG_PRODUCTS = [
    {"id": "t1", "name": "✈️ 美国 +1 成品号", "price": "4.5"},
    {"id": "t2", "name": "✈️ 印度 +91 成品号", "price": "2.0"},
    {"id": "t3", "name": "✈️ 印尼 +62 成品号", "price": "3.0"},
    {"id": "t4", "name": "✈️ 菲律宾 +63 成品号", "price": "3.5"},
    {"id": "t5", "name": "✈️ 越南 +84 成品号", "price": "4.0"}
]

AI_PRODUCTS = [
    {"id": "ai1", "name": "🤖 ChatGPT 独享账号", "price": "12.0"}
]

NODE_PRODUCTS = [
    {"id": "n1", "name": "🌐 高速专线节点 100G", "price": "10.0"},
    {"id": "n2", "name": "🚀 极速抗封锁节点 500G", "price": "30.0"},
    {"id": "n3", "name": "💎 独享顶级节点 1T", "price": "50.0"}
]
# ================================================


REFERRAL_RATE = 0.05
REFERRAL_PAGE_SIZE = 8
WITHDRAW_MINIMUM = 10.0
WITHDRAWALS_FILE = "/root/fakabot/withdrawals.json"
ORDERS_FILE = "/root/fakabot/orders.json"

ARCHIVE_CHAT_ID = os.getenv("TELEGRAM_ARCHIVE_CHAT_ID", "-1004459109788").strip()
ARCHIVE_STORE = None

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
    kb.row(premium_button("苹果 ID", callback_data="list_apple"), premium_button("TG 账号", callback_data="list_tg"))
    kb.row(premium_button("AI 专区", callback_data="list_ai"), premium_button("社交账号", callback_data="brands_social"))
    kb.row(premium_button("邮箱账号", callback_data="brands_email"), premium_button("视频账号", callback_data="brands_video"))
    kb.row(premium_button("通讯账号", callback_data="brands_communication"), premium_button("电商账号", callback_data="brands_commerce"))
    kb.row(premium_button("节点套餐", callback_data="list_node"), premium_button("返回主菜单", callback_data="ui_buy"))
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
    catalogs = {
        "apple": APPLE_PRODUCTS,
        "tg": TG_PRODUCTS,
        "ai": AI_PRODUCTS,
        "node": NODE_PRODUCTS,
    }
    # 其余五大类目前展示已配置的品牌入口，不伪造商品、库存或价格。
    if cat_id in BRAND_GROUPS:
        return [
            {"id": f"{cat_id}_{i}", "name": name, "brand_index": i}
            for i, (name, _emoji) in enumerate(BRAND_GROUPS[cat_id]["brands"])
        ]
    return catalogs.get(cat_id, [])


def get_product_page_text(cat_id, page=0):
    products = get_product_catalog(cat_id)
    total = len(products)
    pages = max(1, (total + 7) // 8)
    page = max(0, min(int(page), pages - 1))
    titles = {
        "apple": ("🍎", "苹果 ID 专区", "覆盖不同地区、账号年限及使用需求。请先阅读商品名称和适用说明，再选择对应商品。"),
        "tg": ("✈️", "TG 账号专区", "提供不同国家区号的 Telegram 账号。购买前请核对国家区号及账号用途。"),
        "ai": ("🤖", "AI 账号专区", "汇集 AI 工具账号选项。请根据服务类型和使用需求选择。"),
        "social": ("👥", "社交账号专区", "选择需要的社交平台账号类别。"),
        "email": ("✉️", "邮箱账号专区", "选择需要的邮箱服务类别。"),
        "video": ("🎬", "视频账号专区", "选择需要的视频平台账号类别。"),
        "communication": ("💬", "通讯账号专区", "选择需要的通讯平台账号类别。"),
        "commerce": ("🛒", "电商账号专区", "选择需要的电商平台账号类别。"),
        "node": ("🌐", "节点套餐专区", "按流量和使用需求选择套餐；购买前请确认套餐规格。"),
    }
    icon, title, intro = titles.get(cat_id, ("🛍️", "商品专区", "请选择适合您的商品。"))
    first = page * 8 + 1 if total else 0
    last = min((page + 1) * 8, total)
    item_label = "商品" if cat_id in ("apple", "tg", "ai", "node") else "品牌入口"
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
    # 九大类统一为单列：每个商品/品牌入口独占一行，点击整行直接进入对应流程。
    kb = types.InlineKeyboardMarkup(row_width=1)
    product_icons = {
        "apple": "🍎", "tg": "✈️", "ai": "🤖", "social": "👥",
        "email": "✉️", "video": "🎬", "communication": "💬",
        "commerce": "🛒", "node": "🌐"
    }
    product_icon = product_icons.get(cat_id, "🛍️")
    for product_index, product in enumerate(selected, start=page * 8 + 1):
        if cat_id in BRAND_GROUPS:
            callback = f"brand_{cat_id}_{product['brand_index']}"
            item_name = product["name"]
        else:
            callback = f"buy_{product['id']}_{page}"
            item_name = f"{product['name']} | {product['price']} U"
        label = f"{product_icon} {product_index}. {item_name}"
        kb.add(premium_button(label, callback_data=callback))
    if not selected:
        kb.add(premium_button("商品整理上架中", callback_data="show_support"))
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
    text = f"🏷️ <b>{html.escape(brand_name)}</b>\n\n该品牌商品正在整理上架。请联系客服确认可售商品、库存及价格。"
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
    if cat_id not in ("apple", "tg", "ai", "social", "email", "video", "communication", "commerce", "node"):
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
    all_products = APPLE_PRODUCTS + TG_PRODUCTS + AI_PRODUCTS + NODE_PRODUCTS
    p = next((x for x in all_products if x["id"] == prod_id), None)
    if not p:
        bot.answer_callback_query(call.id, "商品不存在或已下架，请刷新商品列表。", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    if prod_id.startswith("ai"):
        detail = f"🛍 <b>商品详情：{p['name']}</b>\n💰 <b>售价：</b> <code>{p['price']} U</code>\n\n👇 <i>点击下方【在线支付】全自动发货</i>"
    elif prod_id.startswith("a"):
        detail = (
            f"🛍 <b>商品详情：{p['name']}</b>\n"
            f"💰 <b>售价：</b> <code>{p['price']} U</code>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📝 <b>【商品描述】</b>\n买后发一个链接给你，获取苹果ID和密码。仅限自用。\n\n"
            f"⚠️ <b>禁止登录账号设置，否则后果自负</b>\n\n"
            f"📌 <b>登录操作流程图解：</b>\n"
            f"1. 打开 App Store 退出当前账号\n"
            f"2. 输入卡密中的账号密码登录\n"
            f"3. 如弹窗提示，选择【其他选项】->【不升级】\n"
            f"4. 仅限在 App Store 登录下载，<b>千万别去设置里登！</b>\n\n"
            f"👇 <i>点击下方【在线支付】全自动发货</i>"
        )
    else:
        detail = f"🛍 <b>商品详情：{p['name']}</b>\n💰 <b>售价：</b> <code>{p['price']} U</code>\n\n👇 <i>点击下方【在线支付】全自动发货</i>"

    back_cat = "ai" if prod_id.startswith("ai") else ("apple" if prod_id.startswith("a") else ("tg" if prod_id.startswith("t") else "node"))
    back_cb = f"products_{back_cat}_{return_page}"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(premium_button("💳 在线支付", callback_data=f"pay_{prod_id}"), premium_button("💬 联系客服", url="https://t.me/qingtian_Boss"))
    kb.add(premium_button("🔙 返回列表", callback_data=back_cb))
    safe_edit(bot, call.message.chat.id, call.message.message_id, detail, kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def pay_item(call):
    bot.answer_callback_query(call.id, "支付接口准备中", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "ui_orders")
def show_orders(call):
    bot.answer_callback_query(call.id)
    all_orders = load_json_file(ORDERS_FILE, [])
    uid = str(call.from_user.id)
    mine = [
        item for item in all_orders
        if isinstance(item, dict) and _order_user_id(item) == uid
    ] if isinstance(all_orders, list) else []
    mine.sort(key=lambda item: str(item.get("created_at") or item.get("paid_at") or ""), reverse=True)
    if not mine:
        text = (
            "📦 <b>我的历史订单</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "目前还没有订单记录。您可以返回商城选择商品，订单创建后会自动归档到这里。"
        )
    else:
        lines = [
            "📦 <b>我的历史订单</b>",
            f"共 <b>{len(mine)}</b> 笔记录 · 最近显示 10 笔",
            "━━━━━━━━━━━━━━━━━━",
        ]
        status_labels = {
            "paid": "已支付", "success": "已支付", "completed": "已完成",
            "pending": "待支付", "pending_payment": "待支付",
            "expired": "已过期", "failed": "失败", "delivered": "已交付",
        }
        for item in mine[:10]:
            order_id = html.escape(str(item.get("order_id") or item.get("id") or "—"))
            product = html.escape(str(item.get("product_name") or item.get("product_title") or item.get("item_name") or item.get("product") or "商品"))
            amount = _order_amount(item)
            raw_status = str(item.get("payment_status") or item.get("status") or "unknown").lower()
            status = status_labels.get(raw_status, html.escape(raw_status))
            created = html.escape(format_local_time(item.get("created_at") or item.get("paid_at") or item.get("paid_time")))
            lines.append(
                f"\n<b>{product}</b>\n订单：<code>{order_id}</code>\n"
                f"金额：<b>{amount:.2f} USDT</b> · 状态：<b>{status}</b>\n"
                f"时间：{created}"
            )
        text = "\n".join(lines)
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_back_kb())

@bot.callback_query_handler(func=lambda call: call.data == "ui_help")
def show_help(call):
    text = "❓ <b>使用帮助</b>\n\n• 购买流程：选择分类 -> 选择商品 -> 支付 -> 自动发货。\n• 售后说明：虚拟商品售出概不退换，如有问题请点击【联系客服】。"
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_back_kb())

def render_invite_page(chat_id, message_id, uid):
    user = get_user(uid)
    language = user.get("language", "zh")
    bot_username = bot.get_me().username
    invite_link = f"https://t.me/{bot_username}?start=ref_{uid}"
    if language == "en":
        text = (
            "🎁 <b>PERMANENT REFERRAL PROGRAM</b>\n"
            "<i>Share your personal invitation link and build a lasting referral network.</i>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💎 <b>Lifetime 5% commission</b>\n"
            "Receive 5% of every eligible purchase successfully paid by a friend you invite. The referral relationship is permanently linked and cannot be transferred.\n\n"
            "💳 <b>Flexible rewards</b>\n"
            "Referral rewards are credited to your account balance and can be used for purchases or eligible withdrawals. Minimum withdrawal: 10 USDT.\n\n"
            "🔗 <b>Your personal invitation link</b>\n"
            f"<code>{html.escape(invite_link)}</code>\n\n"
            "<i>Share the link with a friend. Their qualifying paid orders will contribute to your lifetime rewards.</i>"
        )
    else:
        text = (
            "🎁 <b>永久邀请奖励计划</b>\n"
            "<i>分享专属邀请链接，建立长期有效的好友邀请关系。</i>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💎 <b>好友消费，永久返佣 5%</b>\n"
            "好友通过您的专属链接加入后将永久绑定。此后每笔符合条件且已成功支付的订单，您都可获得订单金额 5% 的邀请奖励，绑定关系不可转移。\n\n"
            "💳 <b>奖励灵活使用</b>\n"
            "邀请奖励计入账户余额，可用于购买商品，也可申请提现。提现最低门槛为 10 USDT。\n\n"
            "🔗 <b>您的专属邀请链接</b>\n"
            f"<code>{html.escape(invite_link)}</code>\n\n"
            "<i>复制并分享链接给好友。好友后续成功支付的合格订单，将持续为您累计邀请奖励。</i>"
        )
    safe_edit(bot, chat_id, message_id, text, get_invite_kb(language))

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
    referral_reward = float(user.get("invite_bonus", 0) or 0)
    if language == "en":
        text = (
            "WALLET MANAGEMENT\n"
            "<i>TRON network · USDT · TRC20</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Available balance: <b>{balance:.2f} USDT</b>\n"
            f"Referral rewards: <b>{referral_reward:.2f} USDT</b>\n\n"
            f"Bound receiving address:\n<code>{html.escape(display_address)}</code>\n\n"
            "You may update this address at any time. A new address is saved only after you review the full address and confirm it."
        )
    else:
        text = (
            "钱包地址管理\n"
            "<i>TRON 网络 · USDT · TRC20</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"账户可用余额：<b>{balance:.2f} USDT</b>\n"
            f"累计邀请奖励：<b>{referral_reward:.2f} USDT</b>\n\n"
            f"当前绑定收款地址：\n<code>{html.escape(display_address)}</code>\n\n"
            "您可以随时更换收款地址。新地址必须先完整展示，经您逐位核对并确认后才会保存。提现前也会再次核对收款地址。"
        )
    safe_edit(bot, chat_id, message_id, text, get_wallet_kb(language))


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
    kb.add(premium_button("Confirm and save address" if language == "en" else "核对无误，确认绑定",
                          callback_data="wallet_confirm"))
    kb.add(premium_button("Cancel" if language == "en" else "取消", callback_data="wallet_cancel"))
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
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    language = user.get("language", "zh")
    address = str(user.get("wallet_address") or "")
    balance = float(user.get("balance", 0) or 0)
    if not valid_tron_address(address):
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(premium_button("Bind wallet address" if language == "en" else "先绑定收款钱包", callback_data="wallet_bind"))
        bot.send_message(call.message.chat.id,
                         "Please bind and verify a TRC20 wallet first." if language == "en" else
                         "请先绑定并核对 TRC20 收款钱包地址，再申请提现。", reply_markup=kb)
        return
    if balance < WITHDRAW_MINIMUM:
        bot.send_message(call.message.chat.id,
                         f"Minimum withdrawal: 10 USDT. Available: {balance:.2f} USDT." if language == "en" else
                         f"提现门槛为 10 USDT，当前可用余额 {balance:.2f} USDT，达到门槛后即可申请。")
        return
    prompt = ("Enter the withdrawal amount in USDT. Your saved wallet address will be shown for final confirmation."
              if language == "en" else "请输入提现金额（USDT）。提交前系统会再次展示已绑定的钱包地址供您核对。")
    sent = bot.send_message(call.message.chat.id, prompt, reply_markup=types.ForceReply(selective=True))
    bot.register_next_step_handler(sent, receive_withdraw_amount)

def receive_withdraw_amount(message):
    uid = str(message.from_user.id)
    user = get_user(uid)
    language = user.get("language", "zh")
    try:
        amount = round(float((message.text or "").strip()), 6)
    except (TypeError, ValueError):
        bot.reply_to(message, "Invalid amount." if language == "en" else "金额格式不正确，请重新发起提现申请。")
        return
    users = load_json_file(USERS_FILE, {})
    user = users.get(uid, {})
    balance = float(user.get("balance", 0) or 0)
    address = str(user.get("wallet_address") or "")
    if amount < WITHDRAW_MINIMUM or amount > balance or not valid_tron_address(address):
        bot.reply_to(message, "Amount or wallet is invalid. Please check your balance and saved wallet." if language == "en"
                     else "提现金额无效、余额不足或钱包未绑定，请检查后重新申请。")
        return
    user["pending_withdrawal"] = {"amount": amount, "address": address, "created_at": time.time()}
    users[uid] = user
    save_json_file(USERS_FILE, users)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(premium_button("Confirm withdrawal request" if language == "en" else "确认提交提现申请", callback_data="wallet_withdraw_confirm"))
    kb.add(premium_button("Cancel" if language == "en" else "取消", callback_data="wallet_withdraw_cancel"))
    text = (f"Confirm withdrawal:\n<b>{amount:.2f} USDT</b>\nTRC20 address:\n<code>{html.escape(address)}</code>\n\n"
            "Please verify the destination before submitting." if language == "en" else
            f"请核对提现信息：\n提现金额：<b>{amount:.2f} USDT</b>\nTRC20 收款地址：\n<code>{html.escape(address)}</code>\n\n确认地址和金额无误后，再提交申请。")
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "wallet_withdraw_confirm")
def wallet_withdraw_confirm(call):
    uid = str(call.from_user.id)
    users = load_json_file(USERS_FILE, {})
    user = users.get(uid, {})
    pending = user.get("pending_withdrawal") or {}
    amount = round(float(pending.get("amount", 0) or 0), 6)
    address = str(pending.get("address") or "")
    balance = float(user.get("balance", 0) or 0)
    if amount < WITHDRAW_MINIMUM or amount > balance or not valid_tron_address(address) or address != user.get("wallet_address"):
        bot.answer_callback_query(call.id, "Withdrawal details changed or invalid. Please restart.", show_alert=True)
        user.pop("pending_withdrawal", None)
        users[uid] = user
        save_json_file(USERS_FILE, users)
        return
    withdrawals = load_json_file(WITHDRAWALS_FILE, [])
    request_id = f"WD{int(time.time())}{uid[-4:]}"
    user["balance"] = round(balance - amount, 6)
    user.pop("pending_withdrawal", None)
    users[uid] = user
    withdrawals.append({"request_id": request_id, "user_id": uid, "amount": amount,
                        "address": address, "status": "pending", "created_at": time.time()})
    save_json_file(USERS_FILE, users)
    save_json_file(WITHDRAWALS_FILE, withdrawals)
    language = user.get("language", "zh")
    bot.answer_callback_query(call.id, "Request submitted." if language == "en" else "提现申请已提交")
    bot.send_message(call.message.chat.id,
                     f"Withdrawal request <b>{request_id}</b> submitted for manual review." if language == "en" else
                     f"提现申请已提交，编号：<code>{request_id}</code>\n状态：待人工审核。", parse_mode="HTML")
    if ADMIN_ID.isdigit():
        try:
            bot.send_message(int(ADMIN_ID),
                f"💸 <b>新提现申请</b>\n编号：<code>{request_id}</code>\n用户：<code>{uid}</code>\n金额：{amount:.2f} USDT\n地址：<code>{html.escape(address)}</code>",
                parse_mode="HTML")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data == "wallet_withdraw_cancel")
def wallet_withdraw_cancel(call):
    users = load_json_file(USERS_FILE, {})
    user = users.get(str(call.from_user.id), {})
    user.pop("pending_withdrawal", None)
    users[str(call.from_user.id)] = user
    save_json_file(USERS_FILE, users)
    bot.answer_callback_query(call.id, "Cancelled.")
    render_wallet_page(call.message.chat.id, call.message.message_id, call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "ui_recharge")
def show_recharge(call):
    user = get_user(call.from_user.id)
    language = user.get("language", "zh")
    uid = str(call.from_user.id)
    display_name = str(user.get("name") or call.from_user.first_name or ("User" if language == "en" else "用户"))
    balance = float(user.get("balance", 0) or 0)
    referral_reward = float(user.get("invite_bonus", 0) or 0)
    wallet = str(user.get("wallet_address") or "")
    wallet_display = wallet if wallet else ("Not bound" if language == "en" else "暂未绑定")
    profile_link = f'<a href="tg://user?id={uid}">{html.escape(display_name)}</a>'
    if language == "en":
        text = (
            "💼 <b>ACCOUNT & FUNDS CENTER</b>\n"
            "<i>Account overview · Balance · Referral rewards · Wallet</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Account:</b> {profile_link}\n"
            f"🆔 <b>Telegram ID:</b> <code>{uid}</code>\n\n"
            f"💵 <b>Available balance:</b> <b>{balance:.2f} USDT</b>\n"
            f"🎁 <b>Referral rewards earned:</b> <b>{referral_reward:.2f} USDT</b>\n\n"
            f"🔗 <b>TRC20 wallet:</b>\n<code>{html.escape(wallet_display)}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Manage your receiving address and review it carefully before every withdrawal. Deposits are not yet automated; contact support for deposit assistance.</i>"
        )
    else:
        text = (
            "💼 <b>账户与资金中心</b>\n"
            "<i>账户资料 · 余额管理 · 邀请奖励 · 收款钱包</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>用户昵称：</b>{profile_link}\n"
            f"🆔 <b>数字 ID：</b><code>{uid}</code>\n\n"
            f"💵 <b>账户可用余额：</b><b>{balance:.2f} USDT</b>\n"
            f"🎁 <b>累计邀请奖励：</b><b>{referral_reward:.2f} USDT</b>\n\n"
            f"🔗 <b>已绑定钱包地址（TRC20）：</b>\n<code>{html.escape(wallet_display)}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>可在下方管理或更换收款地址。每次提现前请仔细核对完整地址。自动充值通道尚未开放，如需充值请联系客服。</i>"
        )
    kb = types.InlineKeyboardMarkup(row_width=2)
    if language == "en":
        kb.row(
            premium_button("Manage wallet", callback_data="wallet_manage"),
            premium_button("Withdraw", callback_data="wallet_withdraw")
        )
        kb.row(
            premium_button("Referral rewards", callback_data="ui_invite"),
            premium_button("Main menu", callback_data="ui_buy")
        )
    else:
        kb.row(
            premium_button("钱包地址管理", callback_data="wallet_manage"),
            premium_button("申请提现", callback_data="wallet_withdraw")
        )
        kb.row(
            premium_button("邀请奖励", callback_data="ui_invite"),
            premium_button("返回主菜单", callback_data="ui_buy")
        )
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, kb)


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


@bot.message_handler(func=lambda message: True, content_types=["text"])
def fallback_text_message(message):
    """Give a visible response to ordinary text that has no active conversation handler."""
    bot.send_message(
        message.chat.id,
        "☀️ 已收到您的消息。请使用下方菜单继续操作；如需重新打开主菜单，请发送 /start。",
        reply_markup=get_main_kb(),
    )


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
        result = original_answer(callback_query_id, *args, **kwargs)
        answered_ids.add(str(callback_query_id))
        return result

    bot.answer_callback_query = tracked_answer

    for entry in bot.callback_query_handlers:
        original_handler = entry.get("function")
        if not callable(original_handler) or getattr(original_handler, "_qingtian_safe", False):
            continue

        def safe_handler(call, *args, _handler=original_handler, **kwargs):
            callback_id = str(call.id)
            answered_ids.discard(callback_id)
            try:
                if ARCHIVE_STORE is not None:
                    ARCHIVE_STORE.increment_counter("callback_interactions", 1)
                return _handler(call, *args, **kwargs)
            except Exception as exc:
                print("❌ 内联按钮处理失败：", callback_id, repr(exc), flush=True)
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


if __name__ == "__main__":
    print("🤖 晴天号铺机器人启动中...", flush=True)
    try:
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
            print("✅ 私密 Telegram 归档已连接，业务数据不再写入服务器磁盘", flush=True)
        except Exception as archive_init_exc:
            # Business handlers depend on the archive store. Polling without it
            # would make /start and every data-backed button fail silently.
            print("❌ Telegram 归档初始化失败，停止启动并交由 systemd 重试：", repr(archive_init_exc), flush=True)
            raise RuntimeError("业务数据归档未就绪，拒绝启动不完整的机器人实例") from archive_init_exc

        # Startup notifications and source archiving are auxiliary; never block polling.
        try:
            me = bot.get_me()
            startup_report = (
                "☀️ 晴天号铺 · 启动工作汇报\n"
                f"机器人：@{me.username or 'unknown'}\n"
                f"启动时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
                "状态：机器人正在启动消息轮询。\n"
                "测试指令：/ping"
            )
            if archive_ready:
                ARCHIVE_STORE.send_report_snapshot(startup_report, report_title="启动汇报")
            else:
                bot.send_message(ARCHIVE_CHAT_ID, startup_report, disable_notification=True)
        except Exception as report_exc:
            print("⚠️ 启动汇报发送失败，不影响机器人轮询：", repr(report_exc), flush=True)
        if archive_ready:
            try:
                archive_source_bundle()
            except Exception as archive_exc:
                print("❌ 源码归档上传失败，不阻断机器人消息轮询：", repr(archive_exc), flush=True)
            threading.Thread(target=ARCHIVE_STORE.flush_loop, args=(5,), name="archive-flush", daemon=True).start()
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
        print("❌ 机器人 polling 异常：", repr(exc), flush=True)
        traceback.print_exc()
        raise

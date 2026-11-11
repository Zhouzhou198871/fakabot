# -*- coding: utf-8 -*-
"""
Telegram 自动发卡机器人
依赖：pyTelegramBotAPI
数据：users.json / orders.json / config.json / stock.txt

说明：
- 本版本按“库存领取即发货”实现，不包含真实收款或余额扣款。
- TOKEN、ADMIN_ID 可通过环境变量 BOT_TOKEN、ADMIN_ID 配置。
- JSON 文件采用原子替换；进程内共享锁保护并发领取。
- 如部署多进程/多实例，请改用 SQLite 或其他具备事务能力的数据库。
"""

import os
import json
import html
import logging
import threading
import tempfile
from datetime import datetime, timezone

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException


# ========================= 基础配置 =========================

TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STOCK_FILE = os.path.join(BASE_DIR, "stock.txt")

CHANNEL_URL = "https://t.me/qingtian_vip"
SUPPORT_URL = "https://t.me/qingtian_Boss"

DEFAULT_CONFIG = {
    "welcome_tip": "点击下方按钮即可挑选心仪商品，系统全自动发货。",
    "buy_button_text": "🛒 立即购买"
}

DATA_LOCK = threading.RLock()
ADMIN_EDIT_STATE = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8")
    ]
)
logger = logging.getLogger("fakabot")

if not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
    raise RuntimeError("请设置 BOT_TOKEN 环境变量或在 bot.py 中填写 TOKEN。")
if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID 必须是有效的 Telegram 数字用户 ID。")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True, num_threads=8)


# ========================= 文件与数据工具 =========================

def atomic_write_text(path, content):
    """在目标文件同一目录写临时文件，再原子替换。"""
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


def load_json(path, expected_type, default):
    """读取 JSON；损坏时先备份，不静默覆盖原始文件。"""
    if not os.path.exists(path):
        return default.copy() if isinstance(default, (dict, list)) else default

    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, expected_type):
            raise ValueError("JSON 根节点类型不符合预期")
        return value
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        backup = path + ".corrupt"
        try:
            if not os.path.exists(backup):
                os.replace(path, backup)
        except OSError:
            logger.exception("备份损坏文件失败：%s", path)
        logger.error("JSON 文件异常 %s：%s；已使用内存默认值继续", path, exc)
        return default.copy() if isinstance(default, (dict, list)) else default
    except OSError:
        logger.exception("读取文件失败：%s", path)
        raise


def ensure_files():
    """首次启动时创建所有数据文件。"""
    with DATA_LOCK:
        if not os.path.exists(USERS_FILE):
            atomic_write_json(USERS_FILE, {})
        if not os.path.exists(ORDERS_FILE):
            atomic_write_json(ORDERS_FILE, [])
        if not os.path.exists(CONFIG_FILE):
            atomic_write_json(CONFIG_FILE, DEFAULT_CONFIG)
        if not os.path.exists(STOCK_FILE):
            atomic_write_text(STOCK_FILE, "")


def read_users():
    return load_json(USERS_FILE, dict, {})


def read_orders():
    return load_json(ORDERS_FILE, list, [])


def read_config():
    config = load_json(CONFIG_FILE, dict, DEFAULT_CONFIG.copy())
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)
    return config


def write_users(data):
    atomic_write_json(USERS_FILE, data)


def write_orders(data):
    atomic_write_json(ORDERS_FILE, data)


def write_config(data):
    atomic_write_json(CONFIG_FILE, data)


def read_stock():
    with open(STOCK_FILE, "r", encoding="utf-8") as f:
        return [line.rstrip("\r\n") for line in f if line.strip()]


def write_stock(items):
    # 卡密本身允许包含空格，但不允许换行；每行代表一张卡。
    for item in items:
        if "\n" in item or "\r" in item:
            raise ValueError("卡密不能包含换行符")
    atomic_write_text(STOCK_FILE, "".join(item + "\n" for item in items))


def html_text(value):
    return html.escape(str(value), quote=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def calculate_days(created_at):
    try:
        start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - start.astimezone(timezone.utc)).days)
    except (TypeError, ValueError):
        return 0


def get_or_create_user(user):
    """首次 /start 或购买时登记用户；余额字段预留为 0。"""
    uid = str(user.id)
    with DATA_LOCK:
        users = read_users()
        record = users.get(uid)
        if not isinstance(record, dict):
            record = {
                "user_id": user.id,
                "first_name": user.first_name or "",
                "username": user.username or "",
                "balance": 0.0,
                "first_seen": now_iso()
            }
        else:
            record["first_name"] = user.first_name or record.get("first_name", "")
            record["username"] = user.username or record.get("username", "")
            record.setdefault("balance", 0.0)
            record.setdefault("first_seen", now_iso())
            record["user_id"] = user.id
        users[uid] = record
        write_users(users)
        return record


def is_admin(user_id):
    return int(user_id) == ADMIN_ID


# ========================= 用户首屏与菜单 =========================

def main_keyboard(config):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buy_text = config.get("buy_button_text", DEFAULT_CONFIG["buy_button_text"])
    if not isinstance(buy_text, str) or not buy_text.strip():
        buy_text = DEFAULT_CONFIG["buy_button_text"]

    markup.row(
        types.InlineKeyboardButton(buy_text[:64], callback_data="buy"),
        types.InlineKeyboardButton("👤 历史订单", callback_data="orders")
    )
    markup.row(
        types.InlineKeyboardButton("📢 官方频道", url=CHANNEL_URL),
        types.InlineKeyboardButton("💬 联系客服", url=SUPPORT_URL)
    )
    return markup


def welcome_text(user, record, config):
    uid = user.id
    first_name = html_text(user.first_name or "用户")
    balance = record.get("balance", 0)
    try:
        balance_text = f"{float(balance):.2f}"
    except (TypeError, ValueError):
        balance_text = "0.00"
    days = calculate_days(record.get("first_seen", now_iso()))

    # 管理员配置的提示语按纯文本处理，避免输入 HTML 破坏消息结构。
    tip = html_text(config.get("welcome_tip", DEFAULT_CONFIG["welcome_tip"]))
    tip = tip.replace("\n", "\n")

    return (
        f"✨ <b>尊敬的 {first_name}，欢迎光临！</b>\n\n"
        f"📌 <b>您的专属账户信息</b>\n"
        f" ├ 🆔 <b>用户 ID：</b> <code>{uid}</code>\n"
        f" ├ 💰 <b>账户余额：</b> <b>{balance_text}</b> USDT\n"
        f" └ ⏳ <b>相伴天数：</b> 已陪伴您 <b>{days}</b> 天\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🛒 <b>自助选购</b>：{tip}\n"
        f"🛡 <b>安全提示</b>：请妥善保管您的账号密码，虚拟商品售出概不退换。\n\n"
        f"📢 <b>内置节点频道</b>：<a href=\"{CHANNEL_URL}\">点击加入</a>\n"
        f"💬 <b>专属客服</b>：<a href=\"{SUPPORT_URL}\">@qingtian_Boss</a>\n\n"
        f'<tg-emoji emoji-id="5368324170671202286">✨</tg-emoji>'
    )


@bot.message_handler(commands=["start"])
def handle_start(message):
    try:
        record = get_or_create_user(message.from_user)
        config = read_config()
        bot.send_message(
            message.chat.id,
            welcome_text(message.from_user, record, config),
            reply_markup=main_keyboard(config),
            disable_web_page_preview=True
        )
    except ApiTelegramException:
        logger.exception("发送 /start 消息失败")
        safe_send(message.chat.id, "欢迎页面发送失败，请稍后再试。")
    except Exception:
        logger.exception("处理 /start 时发生异常")
        safe_send(message.chat.id, "系统暂时繁忙，请稍后再试。")


def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except ApiTelegramException:
        logger.exception("Telegram 消息发送失败，chat_id=%s", chat_id)
    except Exception:
        logger.exception("发送消息时发生未知异常，chat_id=%s", chat_id)
    return None


# ========================= 购买与自动发货 =========================

def deliver_one_card(user):
    """
    原子领取一条库存并记录订单。
    进程内锁保证本机器人多个线程不会发出同一条卡密。
    """
    uid = str(user.id)
    with DATA_LOCK:
        stock = read_stock()
        if not stock:
            return None, "empty"

        card = stock[0]
        remaining = stock[1:]
        orders = read_orders()

        order = {
            "order_id": f"{int(datetime.now(timezone.utc).timestamp() * 1000)}-{user.id}",
            "time": now_iso(),
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "product": card,
            "status": "delivered"
        }

        # 先持久化库存与订单；任一写入失败则尽可能回滚库存。
        write_stock(remaining)
        try:
            orders.append(order)
            write_orders(orders)
        except Exception:
            logger.exception("写入订单失败，尝试恢复库存")
            write_stock(stock)
            raise

        return card, order


@bot.callback_query_handler(func=lambda call: call.data == "buy")
def handle_buy(call):
    try:
        bot.answer_callback_query(call.id)
        get_or_create_user(call.from_user)

        with DATA_LOCK:
            stock = read_stock()
            if not stock:
                bot.send_message(call.message.chat.id, "😔 商品已售罄，请稍后再来。")
                return

            card, order = deliver_one_card(call.from_user)

        if card is None:
            bot.send_message(call.message.chat.id, "😔 商品已售罄，请稍后再来。")
            return

        sent = safe_send(
            call.message.chat.id,
            "🎉 <b>发货成功！</b>\n\n"
            "📦 <b>您的商品卡密：</b>\n"
            f"<code>{html_text(card)}</code>\n\n"
            f"🧾 <b>订单编号：</b> <code>{html_text(order['order_id'])}</code>\n"
            "请及时保存卡密，避免丢失。"
        )
        if sent is None:
            # Telegram 返回失败时不自动回滚已记账库存，避免网络超时但消息实际送达时重复发卡。
            logger.error("订单 %s 已登记，但发货消息未确认送达，请管理员核查", order["order_id"])
            safe_send(ADMIN_ID, f"⚠️ 订单 {html_text(order['order_id'])} 已扣库存，但用户消息发送未确认，请核查。")
    except ApiTelegramException:
        logger.exception("购买回调 Telegram API 异常")
        try:
            bot.answer_callback_query(call.id, "操作失败，请稍后重试。", show_alert=True)
        except Exception:
            pass
    except Exception:
        logger.exception("购买处理失败")
        try:
            bot.answer_callback_query(call.id, "系统繁忙，请联系管理员。", show_alert=True)
        except Exception:
            pass


# ========================= 历史订单 =========================

def format_user_orders(user_id):
    with DATA_LOCK:
        orders = read_orders()

    mine = [
        order for order in orders
        if str(order.get("user_id")) == str(user_id)
    ]
    mine.sort(key=lambda x: x.get("time", ""), reverse=True)
    mine = mine[:5]

    if not mine:
        return "📭 <b>暂无历史订单。</b>\n购买商品后，订单会显示在这里。"

    lines = ["📜 <b>最近 5 笔历史订单</b>\n"]
    for index, order in enumerate(mine, 1):
        timestamp = html_text(order.get("time", "未知时间"))
        order_id = html_text(order.get("order_id", "未知"))
        product = html_text(order.get("product", "未知商品"))
        lines.append(
            f"{index}. 🕒 <code>{timestamp}</code>\n"
            f"   🧾 订单：<code>{order_id}</code>\n"
            f"   📦 卡密：<code>{product}</code>\n"
        )
    return "\n".join(lines)


@bot.callback_query_handler(func=lambda call: call.data == "orders")
def handle_orders_callback(call):
    try:
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, format_user_orders(call.from_user.id))
    except Exception:
        logger.exception("读取历史订单失败")
        safe_send(call.message.chat.id, "订单读取失败，请稍后重试。")


@bot.message_handler(commands=["orders"])
def handle_orders_command(message):
    try:
        bot.send_message(message.chat.id, format_user_orders(message.from_user.id))
    except Exception:
        logger.exception("/orders 处理失败")
        safe_send(message.chat.id, "订单读取失败，请稍后重试。")


# ========================= 管理员控制面板 =========================

def admin_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("✏️ 修改自定义提示语", callback_data="admin_edit_tip"),
        types.InlineKeyboardButton("🔘 修改购买按钮文字", callback_data="admin_edit_button"),
        types.InlineKeyboardButton("🔄 刷新库存", callback_data="admin_refresh")
    )
    return markup


def admin_panel_text():
    with DATA_LOCK:
        stock_count = len(read_stock())
        config = read_config()
    return (
        "🛠 <b>机器人管理员控制面板</b>\n\n"
        f"📦 当前库存：<b>{stock_count}</b> 条\n"
        f"📝 当前提示语：{html_text(config.get('welcome_tip', ''))}\n"
        f"🔘 当前购买按钮：{html_text(config.get('buy_button_text', ''))}\n\n"
        "请选择需要修改的配置："
    )


@bot.message_handler(commands=["admin"])
def handle_admin(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ 你没有权限使用管理员面板。")
        return
    try:
        bot.send_message(
            message.chat.id,
            admin_panel_text(),
            reply_markup=admin_keyboard()
        )
    except Exception:
        logger.exception("打开管理员面板失败")


@bot.callback_query_handler(func=lambda call: call.data in (
    "admin_edit_tip", "admin_edit_button", "admin_refresh"
))
def handle_admin_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "无权操作", show_alert=True)
        return

    try:
        if call.data == "admin_refresh":
            bot.answer_callback_query(call.id, "库存已刷新")
            bot.edit_message_text(
                admin_panel_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=admin_keyboard()
            )
            return

        field = "welcome_tip" if call.data == "admin_edit_tip" else "buy_button_text"
        prompt = (
            "请发送新的自定义提示语："
            if field == "welcome_tip"
            else "请发送新的购买按钮文字（最多 64 个字符）："
        )
        ADMIN_EDIT_STATE[call.from_user.id] = field
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            prompt + "\n发送 /cancel 可取消修改。"
        )
    except Exception:
        logger.exception("管理员按钮处理失败")
        try:
            bot.answer_callback_query(call.id, "操作失败，请重试。", show_alert=True)
        except Exception:
            pass


@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_EDIT_STATE.pop(message.from_user.id, None)
    safe_send(message.chat.id, "已取消本次配置修改。")


@bot.message_handler(
    func=lambda message: message.from_user is not None
    and is_admin(message.from_user.id)
    and message.from_user.id in ADMIN_EDIT_STATE,
    content_types=["text"]
)
def handle_admin_config_input(message):
    field = ADMIN_EDIT_STATE.get(message.from_user.id)
    if not field:
        return

    value = (message.text or "").strip()
    if not value:
        safe_send(message.chat.id, "内容不能为空，请重新发送。")
        return

    if len(value) > 500:
        safe_send(message.chat.id, "内容过长，请控制在 500 个字符以内。")
        return

    if field == "buy_button_text" and len(value) > 64:
        safe_send(message.chat.id, "按钮文字最多 64 个字符，请重新发送。")
        return

    try:
        with DATA_LOCK:
            config = read_config()
            config[field] = value
            write_config(config)

        ADMIN_EDIT_STATE.pop(message.from_user.id, None)
        safe_send(
            message.chat.id,
            "✅ <b>配置修改成功！</b>\n"
            "新配置已保存，下次用户发送 /start 时立即生效。\n\n"
            + admin_panel_text(),
            reply_markup=admin_keyboard()
        )
    except Exception:
        logger.exception("保存管理员配置失败")
        safe_send(message.chat.id, "❌ 保存失败，请检查服务器文件权限后重试。")


# ========================= 管理员库存补货 =========================

@bot.message_handler(commands=["addstock"])
def handle_addstock(message):
    """管理员使用：/addstock 卡密内容；卡密内容不能包含换行。"""
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ 无权操作。")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        safe_send(
            message.chat.id,
            "用法：<code>/addstock 卡密内容</code>\n"
            "每次添加一条卡密；也可以直接编辑服务器上的 stock.txt 批量补货。"
        )
        return

    card = parts[1].strip()
    if "\n" in card or "\r" in card:
        safe_send(message.chat.id, "卡密不能包含换行符。")
        return

    try:
        with DATA_LOCK:
            stock = read_stock()
            stock.append(card)
            write_stock(stock)
        safe_send(message.chat.id, f"✅ 补货成功！当前库存：<b>{len(stock)}</b> 条。")
    except Exception:
        logger.exception("管理员补货失败")
        safe_send(message.chat.id, "补货失败，请检查 stock.txt 文件权限。")


# ========================= 全局异常与启动 =========================

@bot.message_handler(content_types=["text"])
def handle_unknown_text(message):
    # 不打断管理员正在进行的配置输入。
    if is_admin(message.from_user.id) and message.from_user.id in ADMIN_EDIT_STATE:
        return
    if (message.text or "").startswith("/"):
        safe_send(message.chat.id, "未知命令。发送 /start 返回主菜单。")


def main():
    ensure_files()
    logger.info("FakaBot 启动中，管理员 ID：%s", ADMIN_ID)

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=False
            )
        except KeyboardInterrupt:
            logger.info("收到退出信号，机器人停止。")
            break
        except Exception:
            logger.exception("Polling 发生异常，10 秒后重试")
            import time
            time.sleep(10)


if __name__ == "__main__":
    main()

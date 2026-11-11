# -*- coding: utf-8 -*-
import telebot, time, json, os
from telebot import types

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
if not TOKEN:
    raise RuntimeError("缺少环境变量 BOT_TOKEN，请在服务器环境中配置机器人 Token。")
bot = telebot.TeleBot(TOKEN, threaded=False)
USERS_FILE = "/root/fakabot/users.json"

EMOJI = {
    "usdt": "5001485391807645910", "tg": "5001558608115140737", "gear": "6244525025500601411",
    "gift": "6246956045709612135", "diamond": "6244585885187184999", "star": "6244603610517215237",
    "apple": "5001358570013329603", "ai": "6289722534019475465", "video": "5001404418789214347",
    "node": "6244694135542913498"
}

# ================= 完整真实数据区 =================
APPLE_PRODUCTS = [
    {"id": "a1", "name": "🤝美区 共享账号 | 已购小伙剪APP", "price": "2.78"},
    {"id": "a2", "name": "🤝香港 | 注册半年左右 | 完整密保", "price": "2.78"},
    {"id": "a3", "name": "🤝美区 | 注册半年左右 | 完整密保", "price": "2.78"},
    {"id": "a4", "name": "🤝韩国 | 注册半年左右 | 完整密保", "price": "2.78"},
    {"id": "a5", "name": "🤝日本 | 注册半年左右 | 完整密保", "price": "2.78"},
    {"id": "a6", "name": "🤝日本老号 | 密保资料齐全 | 独享", "price": "5.21"},
    {"id": "a7", "name": "🤝马来西亚老号 | 密保资料齐全 | 独享", "price": "5.21"},
    {"id": "a8", "name": "🟠台湾 | 独享 | 仅适用于app store", "price": "5.56"},
    {"id": "a9", "name": "💖美国IC(激活iCloud设置和下载)", "price": "8.06"},
    {"id": "a10", "name": "💖独享下载号 | Icloud后缀", "price": "8.25"},
    {"id": "a11", "name": "💖注册1-5年老号超稳", "price": "9.70"},
    {"id": "a12", "name": "💖2010~2020年老号", "price": "10.39"},
    {"id": "a13", "name": "💖美国 | 免税区 | 激活iCloud", "price": "10.58"},
    {"id": "a14", "name": "💖包含密保 | 独享下载号", "price": "10.90"},
    {"id": "a15", "name": "💖独享下载号 | 冷门国家", "price": "11.09"},
    {"id": "a16", "name": "💖台湾13~19年私人号 | 禁IM", "price": "12.11"},
    {"id": "a17", "name": "💖美国13~19年私人号 | 免税区", "price": "12.11"},
    {"id": "a18", "name": "💖10个月以上 | 稳定IC", "price": "12.48"},
    {"id": "a19", "name": "💖美国 | 注册超过10年", "price": "13.25"},
    {"id": "a20", "name": "💖注册超5-10年以上老IC", "price": "13.68"},
    {"id": "a21", "name": "💖注册超15年以上珍藏老号", "price": "19.24"},
    {"id": "a22", "name": "💖绝版15年以上老IC", "price": "20.63"},
    {"id": "a23", "name": "💖已购买小火箭shadowrocket", "price": "24.30"},
    {"id": "a24", "name": "💖美国 | 注册超5-10年私人号 | 小火箭", "price": "61.11"},
    {"id": "a25", "name": "💖美国 | 注册超15年私人号 | 小火箭", "price": "105.08"}
]

TG_PRODUCTS = [
    {"id": "t1", "name": "✈️ 美国 +1 成品号", "price": "4.5"},
    {"id": "t2", "name": "✈️ 印度 +91 成品号", "price": "2.0"},
    {"id": "t3", "name": "✈️ 印尼 +62 成品号", "price": "3.0"},
    {"id": "t4", "name": "✈️ 菲律宾 +63 成品号", "price": "3.5"},
    {"id": "t5", "name": "✈️ 越南 +84 成品号", "price": "4.0"}
]

AI_PRODUCTS = [
    {"id": "ai1", "name": "🤖 ChatGPT 独享账号", "price": "12.0"},
    {"id": "ai2", "name": "🤖 Claude 3 独享账号", "price": "15.0"},
    {"id": "ai3", "name": "🎨 Midjourney 共享号", "price": "20.0"}
]

NODE_PRODUCTS = [
    {"id": "n1", "name": "🌐 高速专线节点 100G", "price": "10.0"},
    {"id": "n2", "name": "🚀 极速抗封锁节点 500G", "price": "30.0"},
    {"id": "n3", "name": "💎 独享顶级节点 1T", "price": "50.0"}
]
# ================================================

def get_user(uid, name=""):
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f: json.dump({}, f)
    with open(USERS_FILE, "r", encoding="utf-8") as f: users = json.load(f)
    uid_str = str(uid)
    if uid_str not in users:
        users[uid_str] = {"name": name, "join_time": time.time(), "balance": 0.0, "orders_count": 0, "invited_count": 0, "invite_bonus": 0.0}
        with open(USERS_FILE, "w", encoding="utf-8") as f: json.dump(users, f, ensure_ascii=False, indent=2)
    return users[uid_str]

def safe_edit(bot, chat_id, msg_id, text, kb=None):
    try: bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML", reply_markup=kb)
    except: pass

def get_main_kb():
    """主界面：账号与节点合并为账号商城，严格两列三行。"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("账号商城", callback_data="cat_account"),
        types.InlineKeyboardButton("历史订单", callback_data="ui_orders")
    )
    kb.row(
        types.InlineKeyboardButton("邀请奖励", callback_data="ui_invite"),
        types.InlineKeyboardButton("充值", callback_data="ui_recharge")
    )
    kb.row(
        types.InlineKeyboardButton("使用帮助", callback_data="ui_help"),
        types.InlineKeyboardButton("更多服务", callback_data="more_services")
    )
    return kb
def get_more_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("📢 节点交流群", url="https://t.me/qingtian_vip"),
        types.InlineKeyboardButton("📢 代理频道", url="https://t.me/jiedian888")
    )
    kb.row(
        types.InlineKeyboardButton("📢 账号铺频道", url="https://t.me/qingtian_tutorial"),
        types.InlineKeyboardButton("💬 联系客服", callback_data="show_support")
    )
    kb.row(
        types.InlineKeyboardButton("🔙 返回", callback_data="ui_buy"),
        types.InlineKeyboardButton("☀️ 晴天", callback_data="ui_buy")
    )
    return kb

def get_account_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("苹果ID", callback_data="list_apple"), types.InlineKeyboardButton("TG账号", callback_data="list_tg"))
    kb.add(types.InlineKeyboardButton("AI 专区", callback_data="list_ai"), types.InlineKeyboardButton("影音游戏", callback_data="list_video"))
    kb.add(types.InlineKeyboardButton("🔙 返回主菜单", callback_data="ui_buy"))
    return kb

def get_products_kb(cat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if cat_id == "apple": products = APPLE_PRODUCTS
    elif cat_id == "tg": products = TG_PRODUCTS
    elif cat_id == "ai": products = AI_PRODUCTS
    elif cat_id == "node": products = NODE_PRODUCTS
    else: products = [{"id": "0", "name": "暂无商品", "price": "0"}]
    for p in products:
        btn = types.InlineKeyboardButton(f"{p['name']} | {p['price']} U", callback_data=f"buy_{p['id']}")
        if cat_id == "apple": btn.icon_custom_emoji_id = EMOJI["apple"]
        if cat_id == "tg": btn.icon_custom_emoji_id = EMOJI["tg"]
        if cat_id == "ai": btn.icon_custom_emoji_id = EMOJI["ai"]
        kb.add(btn)
    back_btn = "cat_account" if cat_id in ["apple", "tg", "ai", "video"] else "ui_buy"
    kb.add(types.InlineKeyboardButton("🔙 返回上一级", callback_data=back_btn))
    return kb

def get_back_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 返回主菜单", callback_data="ui_buy"))
    return kb

def generate_main_text(user, uid):
    """主界面正文统一使用 Telegram Custom Emoji 实体，不混用普通 Emoji。"""
    return (
        f'<tg-emoji emoji-id="{EMOJI["star"]}">✨</tg-emoji> <b>欢迎光临晴天号铺。</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["diamond"]}">📌</tg-emoji> <b>您的专属账户信息</b>\n'
        f' ├ <tg-emoji emoji-id="{EMOJI["gear"]}">🆔</tg-emoji> <b>用户 ID：</b> <code>{uid}</code>\n'
        f' ├ <tg-emoji emoji-id="{EMOJI["usdt"]}">💰</tg-emoji> <b>账户余额：</b> <b>{user["balance"]:.2f}</b> USDT\n'
        f' ├ <tg-emoji emoji-id="{EMOJI["apple"]}">🛒</tg-emoji> <b>订单记录：</b> {user.get("orders_count", 0)} 单\n'
        f' ├ <tg-emoji emoji-id="{EMOJI["tg"]}">👥</tg-emoji> <b>邀请用户：</b> {user.get("invited_count", 0)} 位\n'
        f' └ <tg-emoji emoji-id="{EMOJI["gift"]}">🎁</tg-emoji> <b>邀请奖励：</b> 累计 {user.get("invite_bonus", 0.0):.2f} USDT\n\n'
        f'━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="{EMOJI["star"]}">🛒</tg-emoji> <b>自助选购</b>：点击下方按钮即可挑选。'
    )

@bot.message_handler(commands=['start'])
def cmd_start(message):
    user = get_user(message.from_user.id, message.from_user.first_name or "老板")
    text = generate_main_text(user, message.from_user.id)
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=get_main_kb())

@bot.callback_query_handler(func=lambda call: call.data == "ui_buy")
def show_main_menu(call):
    bot.answer_callback_query(call.id)
    user = get_user(call.from_user.id)
    text = generate_main_text(user, call.from_user.id)
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_main_kb())

@bot.callback_query_handler(func=lambda call: call.data == "more_services")
def show_more_menu(call):
    bot.answer_callback_query(call.id)
    text = "加入官方社群，获取最新动态。\n如有任何疑问，请随时联系客服为您解答。\n\n👇 请选择需要的服务："
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_more_kb())

@bot.callback_query_handler(func=lambda call: call.data == "show_support")
def show_support(call):
    bot.answer_callback_query(call.id)
    text = "如果在购买或使用过程中遇到任何问题，请点击下方按钮联系人工客服。\n\n⏰ <b>客服在线时间：</b> 09:00 - 24:00"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("👨‍💻 主客服 @qingtian_Boss", url="https://t.me/qingtian_Boss"),
        types.InlineKeyboardButton("🔙 返回", callback_data="more_services")
    )
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass

    photos = bot.get_user_profile_photos(call.bot.id, limit=1)
    if photos.total_count > 0:
        file_id = photos.photos[0][-1].file_id
        bot.send_photo(call.message.chat.id, file_id, caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        bot.send_message(call.message.chat.id, "💬 <b>联系客服</b>\n" + text, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "cat_account")
def show_account_menu(call):
    bot.answer_callback_query(call.id)
    safe_edit(bot, call.message.chat.id, call.message.message_id, "🛍 <b>账号铺</b>\n👇 <i>请选择：</i>", get_account_kb())

@bot.callback_query_handler(func=lambda call: call.data == "cat_node")
def show_node_menu(call):
    bot.answer_callback_query(call.id)
    safe_edit(bot, call.message.chat.id, call.message.message_id, "🚀 <b>节点铺</b>\n👇 <i>请选择：</i>", get_products_kb("node"))

@bot.callback_query_handler(func=lambda call: call.data.startswith("list_"))
def show_category_list(call):
    cat_id = call.data.replace("list_", "")
    titles = {"apple": "🍎 <b>苹果ID 专区</b>", "tg": "✈️ <b>TG账号 专区</b>", "ai": "🤖 <b>AI 专区</b>", "video": "🎬 <b>影音游戏</b>"}
    safe_edit(bot, call.message.chat.id, call.message.message_id, f"{titles.get(cat_id, '商品列表')}\n👇 <i>请选择：</i>", get_products_kb(cat_id))

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def buy_item(call):
    prod_id = call.data.replace("buy_", "")
    all_products = APPLE_PRODUCTS + TG_PRODUCTS + AI_PRODUCTS + NODE_PRODUCTS
    p = next((x for x in all_products if x["id"] == prod_id), None)
    if not p: return
    
    if prod_id.startswith("a"):
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

    back_cb = "list_apple" if prod_id.startswith("a") else ("list_tg" if prod_id.startswith("t") else ("list_ai" if prod_id.startswith("ai") else "cat_node"))
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("💳 在线支付", callback_data=f"pay_{prod_id}"), types.InlineKeyboardButton("💬 联系客服", url="https://t.me/qingtian_Boss"))
    kb.add(types.InlineKeyboardButton("🔙 返回列表", callback_data=back_cb))
    safe_edit(bot, call.message.chat.id, call.message.message_id, detail, kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def pay_item(call):
    bot.answer_callback_query(call.id, "⏳ 支付接口准备中", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "ui_orders")
def show_orders(call):
    bot.answer_callback_query(call.id, "🧾 您暂时还没有历史订单哦~", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "ui_help")
def show_help(call):
    text = "❓ <b>使用帮助</b>\n\n• 购买流程：选择分类 -> 选择商品 -> 支付 -> 自动发货。\n• 售后说明：虚拟商品售出概不退换，如有问题请点击【联系客服】。"
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_back_kb())

@bot.callback_query_handler(func=lambda call: call.data == "ui_invite")
def show_invite(call):
    bot_username = bot.get_me().username
    invite_link = f"https://t.me/{bot_username}?start={call.from_user.id}"
    text = f"🎁 <b>邀请奖励</b>\n\n邀请好友注册并使用本机器人，即可获得丰厚返佣。\n\n🔗 <b>您的专属链接：</b>\n<code>{invite_link}</code>"
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_back_kb())

@bot.callback_query_handler(func=lambda call: call.data == "ui_recharge")
def show_recharge(call):
    text = "💳 <b>账户充值</b>\n\n系统正在接入 USDT 自动充值通道，目前如需充值余额，请联系客服。"
    safe_edit(bot, call.message.chat.id, call.message.message_id, text, get_back_kb())

if __name__ == "__main__":
    print("🤖 晴天号铺机器人启动中...", flush=True)
    try:
        bot.remove_webhook()
        print("✅ Webhook 已清理，开始 long polling", flush=True)
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    except KeyboardInterrupt:
        print("🛑 收到停止信号，机器人退出", flush=True)
    except Exception as exc:
        import traceback
        print("❌ 机器人 polling 异常：", repr(exc), flush=True)
        traceback.print_exc()
        raise

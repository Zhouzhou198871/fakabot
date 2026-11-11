# -*- coding: utf-8 -*-
import telebot, time, json, os, re, html
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
    probe = (label + " " + str(kwargs.get("callback_data", ""))).lower()
    if any(x in probe for x in ("充值", "支付", "usdt", "pay_")):
        key = "usdt"
    elif any(x in probe for x in ("苹果", "apple", "list_apple")):
        key = "apple"
    elif any(x in probe for x in ("tg账号", "telegram", "list_tg")):
        key = "tg"
    elif any(x in probe for x in ("ai", "claude", "chatgpt")):
        key = "ai"
    elif any(x in probe for x in ("影音", "视频", "游戏")):
        key = "video"
    elif any(x in probe for x in ("节点", "node")):
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
    return types.InlineKeyboardButton(
        text=label or "打开",
        icon_custom_emoji_id=EMOJI[key],
        **kwargs
    )

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
        users[uid_str] = {"name": name, "join_time": time.time(), "balance": 0.0, "orders_count": 0, "invited_count": 0, "invite_bonus": 0.0, "language": "zh"}
        with open(USERS_FILE, "w", encoding="utf-8") as f: json.dump(users, f, ensure_ascii=False, indent=2)
    users[uid_str].setdefault("language", "zh")
    return users[uid_str]

def set_user_language(uid, language):
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        users = json.load(f)
    users.setdefault(str(uid), {})["language"] = language
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def safe_edit(bot, chat_id, msg_id, text, kb=None):
    try: bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML", reply_markup=kb)
    except: pass

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

def get_account_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(premium_button("苹果ID", callback_data="list_apple"), premium_button("TG账号", callback_data="list_tg"))
    kb.add(premium_button("AI 专区", callback_data="list_ai"), premium_button("影音游戏", callback_data="list_video"))
    kb.add(premium_button("🔙 返回主菜单", callback_data="ui_buy"))
    return kb

def get_products_kb(cat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if cat_id == "apple": products = APPLE_PRODUCTS
    elif cat_id == "tg": products = TG_PRODUCTS
    elif cat_id == "ai": products = AI_PRODUCTS
    elif cat_id == "node": products = NODE_PRODUCTS
    else: products = [{"id": "0", "name": "暂无商品", "price": "0"}]
    for p in products:
        btn = premium_button(f"{p['name']} | {p['price']} U", callback_data=f"buy_{p['id']}")
        kb.add(btn)
    back_btn = "cat_account" if cat_id in ["apple", "tg", "ai", "video"] else "ui_buy"
    kb.add(premium_button("🔙 返回上一级", callback_data=back_btn))
    return kb

def get_back_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(premium_button("🔙 返回主菜单", callback_data="ui_buy"))
    return kb

def generate_main_text(user, uid):
    """丰富的主菜单欢迎页：正文使用 Premium 自定义表情实体。"""
    first_name = html.escape(str(user.get("name") or "老板"))
    balance = float(user.get("balance", 0) or 0)
    orders = int(user.get("orders_count", 0) or 0)
    invited = int(user.get("invited_count", 0) or 0)
    bonus = float(user.get("invite_bonus", 0) or 0)
    return (
        f'{premium_emoji("star", "✨")} <b>欢迎来到晴天号铺，{first_name}！</b>\n'
        f'<i>一站式数字商品自助商城 · 选购方便 · 操作简单</i>\n\n'
        f'{premium_emoji("diamond", "💎")} <b>您的账户</b>\n'
        f'├ 用户 ID：<code>{uid}</code>\n'
        f'├ 账户余额：<b>{balance:.2f} USDT</b>\n'
        f'├ 累计订单：<b>{orders}</b> 单\n'
        f'├ 邀请好友：<b>{invited}</b> 位\n'
        f'└ 邀请奖励：<b>{bonus:.2f} USDT</b>\n\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{premium_emoji("gift", "🎁")} <b>商城精选</b>\n'
        f'苹果账号 · Telegram 账号 · AI 服务 · 影音游戏\n\n'
        f'{premium_emoji("node", "🌐")} <b>简单三步，轻松购买</b>\n'
        f'① 进入账号商城，选择需要的商品\n'
        f'② 查看商品详情，确认规格与价格\n'
        f'③ 按页面提示完成支付，留意订单与发货消息\n\n'
        f'{premium_emoji("gear", "🛟")} <b>需要帮助？</b>\n'
        f'点击「使用帮助」查看说明，或进入「更多服务」联系官方客服。\n\n'
        f'<b>请选择下方菜单，开始选购吧：</b>'
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
    user = get_user(call.from_user.id)
    language = user.get("language", "zh")
    if language == "en":
        text = (
            "☀️ <b>QINGTIAN DIGITAL SERVICES</b>\\n"
            "<i>Your trusted gateway to digital products and online services.</i>\\n\\n"
            "━━━━━━━━━━━━━━━━━━\\n"
            "💎 <b>Explore Our Services</b>\\n"
            "• <b>Node Community</b> — connect with users, exchange setup tips and stay informed about service updates.\\n"
            "• <b>Proxy Channel</b> — follow announcements, availability and product updates.\\n"
            "• <b>Account Store</b> — discover new digital account products and store notices.\\n\\n"
            "🛡 <b>Service Commitment</b>\\n"
            "We aim to provide clear product information, convenient self-service purchasing and responsive after-sales assistance. Please review each product description before ordering.\\n\\n"
            "🤝 <b>Need a hand?</b>\\n"
            "Our support team can help with order questions, product guidance and basic troubleshooting. Choose a service below to continue."
        )
    else:
        text = (
            "☀️ <b>晴天 · 数字生活服务中心</b>\\n"
            "<i>让数字商品选购更简单，让每一次服务都有清晰指引。</i>\\n\\n"
            "━━━━━━━━━━━━━━━━━━\\n"
            "💎 <b>官方服务矩阵</b>\\n"
            "• <b>节点交流群</b> — 与用户交流使用经验，获取配置建议及社群动态。\\n"
            "• <b>代理资讯频道</b> — 关注服务公告、产品动态与重要通知。\\n"
            "• <b>账号铺频道</b> — 浏览数字账号商品资讯、上新消息与使用提醒。\\n\\n"
            "🛡 <b>服务与保障</b>\\n"
            "我们持续完善商品信息展示、自助选购流程与售后沟通渠道。下单前请仔细阅读商品规格、适用范围及使用说明；如遇订单或使用问题，可通过官方客服获得协助。\\n\\n"
            "🤝 <b>专属服务支持</b>\\n"
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
            "☀️ <b>QINGTIAN DIGITAL SERVICES</b>\\n"
            "<i>Your trusted gateway to digital products and online services.</i>\\n\\n"
            "━━━━━━━━━━━━━━━━━━\\n"
            "💎 <b>Explore Our Services</b>\\n"
            "• <b>Node Community</b> — connect with users, exchange setup tips and stay informed about service updates.\\n"
            "• <b>Proxy Channel</b> — follow announcements, availability and product updates.\\n"
            "• <b>Account Store</b> — discover new digital account products and store notices.\\n\\n"
            "🛡 <b>Service Commitment</b>\\n"
            "We aim to provide clear product information, convenient self-service purchasing and responsive after-sales assistance. Please review each product description before ordering.\\n\\n"
            "🤝 <b>Need a hand?</b>\\n"
            "Our support team can help with order questions, product guidance and basic troubleshooting. Choose a service below to continue."
        )
    else:
        text = (
            "☀️ <b>晴天 · 数字生活服务中心</b>\\n"
            "<i>让数字商品选购更简单，让每一次服务都有清晰指引。</i>\\n\\n"
            "━━━━━━━━━━━━━━━━━━\\n"
            "💎 <b>官方服务矩阵</b>\\n"
            "• <b>节点交流群</b> — 与用户交流使用经验，获取配置建议及社群动态。\\n"
            "• <b>代理资讯频道</b> — 关注服务公告、产品动态与重要通知。\\n"
            "• <b>账号铺频道</b> — 浏览数字账号商品资讯、上新消息与使用提醒。\\n\\n"
            "🛡 <b>服务与保障</b>\\n"
            "我们持续完善商品信息展示、自助选购流程与售后沟通渠道。下单前请仔细阅读商品规格、适用范围及使用说明；如遇订单或使用问题，可通过官方客服获得协助。\\n\\n"
            "🤝 <b>专属服务支持</b>\\n"
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
            "💬 <b>OFFICIAL CUSTOMER SUPPORT</b>\\n"
            "<i>Personal assistance from the QingTian support account.</i>\\n\\n"
            "━━━━━━━━━━━━━━━━━━\\n"
            "👤 <b>Support account preview</b>\\n"
            "Account: <b>@qingtian_Boss</b>\\n"
            "Channel: Telegram direct message\\n\\n"
            "📌 <b>What we can help with</b>\\n"
            "• Product details and pre-purchase questions\\n"
            "• Order status and delivery assistance\\n"
            "• Basic usage guidance and after-sales questions\\n\\n"
            "Please include your order number and a short description when contacting support. Never share your password or verification code."
        )
        contact_label, back_label = "Open support chat", "Back to services"
    else:
        text = (
            "💬 <b>晴天官方人工客服</b>\\n"
            "<i>为您提供清晰、及时的订单与商品咨询支持。</i>\\n\\n"
            "━━━━━━━━━━━━━━━━━━\\n"
            "👤 <b>客服账号预览</b>\\n"
            "账号名称：<b>晴天客服</b>\\n"
            "Telegram：<code>@qingtian_Boss</code>\\n"
            "沟通方式：一对一私信\\n\\n"
            "📌 <b>可协助处理</b>\\n"
            "• 商品规格、库存及购买前咨询\\n"
            "• 订单状态、支付与发货问题\\n"
            "• 商品使用指引及售后问题\\n\\n"
            "联系时建议附上订单编号和问题描述，以便客服更快定位。请勿向任何人提供账户密码或短信验证码。"
        )
        contact_label, back_label = "私信联系人工客服", "返回更多服务"
    kb = types.InlineKeyboardMarkup(row_width=1)
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
    kb.add(premium_button("💳 在线支付", callback_data=f"pay_{prod_id}"), premium_button("💬 联系客服", url="https://t.me/qingtian_Boss"))
    kb.add(premium_button("🔙 返回列表", callback_data=back_cb))
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

"""Central registry for configured and live supplier product catalogs."""
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .cloudflare_catalog import refresh_cloudflare_catalog as _refresh_cloudflare_catalog

from .apple import PRODUCTS as APPLE_PRODUCTS
from .telegram import PRODUCTS as TG_PRODUCTS
from .instagram import PRODUCTS as INSTAGRAM_PRODUCTS
from .ai import PRODUCTS as AI_PRODUCTS

PRODUCT_CATALOGS = {
    "apple": APPLE_PRODUCTS,
    "tg": TG_PRODUCTS,
    "instagram": INSTAGRAM_PRODUCTS,
    "ai": AI_PRODUCTS,
}

# Product-side commercial rules; the bot UI will consume this when it is switched.
SALES_POLICY = {
    "product_scope": "finished_accounts_only",
    "customization_allowed": False,
    "returns_allowed": False,
    "refunds_allowed": False,
    "customer_notice": (
        "本店仅销售现货成品账号，不提供定制服务。账号或虚拟商品交付后，除适用法律另有强制规定、"
        "本店未按约交付或交付内容与商品描述不符外，不支持退货、换货或退款；供应商标题中的售后标注"
        "不代表本店提供退货退款。请下单前核对商品地区、账号类型、年限、邮箱及登录方式。"
    ),
}

UPSTREAM_CATEGORIES = []
UPSTREAM_PRODUCT_CATALOGS = {}
UPSTREAM_CATEGORY_BY_KEY = {}
_UPSTREAM_ICON_BY_ALIAS = {
    "facebook": "📘", "twitter": "𝕏", "instagram": "📸", "discord": "🎮",
    "tk": "🎵", "tgapi": "📲", "telegram": "✈️", "appleid": "🍎",
    "apple-card": "🎁", "microsoft": "🪟", "google": "✉️", "yahoo": "💜",
    "mail": "📨", "whatsapp": "💬", "line": "🟢", "textnow": "📱",
    "linkedin": "💼", "microsoftteams": "👥", "youtube": "▶️",
    "steam": "🎮", "netflix": "🎬", "other": "🧩", "apisms": "📩",
    "gv": "📞", "aws": "📦", "walmart": "🏬", "twitch": "🟣",
}


def _load_upstream_catalog():
    """Load the last successful public catalog snapshot fetched from Dahaoshang."""
    path = Path(__file__).with_name("upstream_catalog.json")
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    category_rows = raw.get("categories", [])
    product_rows = raw.get("products", [])
    by_category = {}
    for product in product_rows:
        by_category.setdefault(str(product.get("category_id", "")), []).append(product)

    for index, category in enumerate(category_rows):
        category_id = str(category.get("id", ""))
        key = f"up{index}"
        alias = str(category.get("alias", "")).lower()
        products = []
        for row in by_category.get(category_id, []):
            # A catalog snapshot is for display only when the upstream item is
            # currently on sale and has positive stock. Never show stale/empty SKUs.
            if row.get("is_on_sale") is not True or int(row.get("stock", 0) or 0) < 1:
                continue
            supplier_id = str(row.get("supplier_product_id") or row.get("id") or "")
            if not supplier_id:
                continue
            try:
                cost = Decimal(str(row.get("price_cny", "0")))
                # CNY cost / 4 = USDT sale price at exactly 100% markup
                # under the agreed 8 CNY = 1 USDT conversion. Keep 4 decimals
                # so low-cost SKUs are not rounded below the target margin.
                sell = max(Decimal("0.0001"), (cost / Decimal("4")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
            except Exception:
                continue
            sell_text = format(sell, ".4f")
            cost_text = format(cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
            product_id = str(row.get("id") or supplier_id)
            products.append({
                "id": product_id,
                "name": str(row.get("name") or "未命名商品"),
                "price_cny": cost_text,
                "price_usdt": sell_text,
                "price": sell_text,
                "supplier_product_id": supplier_id,
                "supplier_price_cny": cost_text,
                "supplier_url": f"https://dahaoshang.com/#/product/{supplier_id}",
                "supplier_verified": False,
                "procurement_ready": False,
                "is_upstream": True,
                "supplier_stock": int(row.get("stock", 0) or 0),
                "supplier_icon": str(row.get("icon") or category.get("icon") or ""),
                "display_icon": _UPSTREAM_ICON_BY_ALIAS.get(alias, "🛍️"),
                "category_id": category_id,
                "category_key": key,
                "category_name": str(category.get("name") or "其他商品"),
                "is_on_sale": bool(row.get("is_on_sale", False)),
                "min_buy": int(row.get("min_buy", 0) or 0),
                "max_buy": int(row.get("max_buy", 0) or 0),
            })
        entry = {
            **category,
            "key": key,
            "product_count": len(products),
            "display_icon": _UPSTREAM_ICON_BY_ALIAS.get(alias, "🛍️"),
        }
        UPSTREAM_CATEGORIES.append(entry)
        UPSTREAM_PRODUCT_CATALOGS[key] = products
        UPSTREAM_CATEGORY_BY_KEY[key] = entry

    PRODUCT_CATALOGS.update(UPSTREAM_PRODUCT_CATALOGS)


_load_upstream_catalog()


def get_available_upstream_categories():
    """Return only supplier categories that currently contain in-stock products."""
    return [category for category in UPSTREAM_CATEGORIES if int(category.get("product_count", 0) or 0) > 0]


def get_all_products():
    """Return all configured products, including the latest fetched upstream snapshot."""
    return [product for products in PRODUCT_CATALOGS.values() for product in products]


def refresh_cloudflare_catalog(force=False):
    """Refresh the bot's live supplier catalog from the Cloudflare edge API."""
    return _refresh_cloudflare_catalog(
        PRODUCT_CATALOGS,
        UPSTREAM_CATEGORIES,
        UPSTREAM_PRODUCT_CATALOGS,
        UPSTREAM_CATEGORY_BY_KEY,
        force=force,
    )


def start_cloudflare_catalog_refresh(interval=300):
    """Refresh the edge catalog in the background without blocking bot callbacks."""
    from .cloudflare_catalog import start_refresh_loop
    return start_refresh_loop(refresh_cloudflare_catalog, interval=interval)
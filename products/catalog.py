"""Central registry for configured and live supplier product catalogs."""
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
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
    "tk": "🎵", "tgapi": "✈️", "telegram": "✈️", "appleid": "🍎",
    "apple-card": "🎁", "microsoft": "🪟", "google": "✉️", "yahoo": "💜",
    "mail": "📨", "whatsapp": "💬", "line": "🟢", "textnow": "📱",
    "linkedin": "💼", "microsoftteams": "👥", "russianemail": "📧", "youtube": "▶️",
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

    category_rows = list(raw.get("categories", []))
    product_rows = list(raw.get("products", []))

    # Reclassify Russian mailbox products independently of inconsistent upstream categories.
    mailbox_terms = ("rambler", "mail.ru", "yandex", "俄罗斯邮箱")
    russian_mail = []
    remaining_products = []
    for row in product_rows:
        name = str(row.get("name") or "").casefold()
        if any(term in name for term in mailbox_terms):
            moved = dict(row)
            moved["original_category_id"] = str(row.get("category_id") or "")
            moved["category_id"] = "local-russian-email"
            russian_mail.append(moved)
        else:
            remaining_products.append(row)
    if russian_mail:
        category_rows = [
            c for c in category_rows
            if str(c.get("id") or "") != "local-russian-email"
        ]
        category_rows.append({
            "id": "local-russian-email",
            "name": "俄罗斯邮箱",
            "alias": "russianemail",
            "icon": "📧",
            "sort_order": 999999,
            "is_enabled": True,
            "source_count": len(russian_mail),
            "is_local_virtual_category": True,
        })
        product_rows = remaining_products + russian_mail

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



_supplier_refresh_lock = threading.RLock()
_supplier_refresh_thread_started = False


def refresh_supplier_catalog(force=False):
    """Fetch the complete authenticated supplier catalog and atomically replace the local cache."""
    from supplier_client import DahaoshangClient

    with _supplier_refresh_lock:
        client = DahaoshangClient()
        category_rows = client.get_categories()
        product_rows = client.get_product_list("0")
        categories = [{
            "id": str(row.get("ID") or ""),
            "name": str(row.get("Name") or "未命名分类"),
            "alias": str(row.get("Alias") or ""),
            "icon": str(row.get("Icon") or ""),
            "sort_order": row.get("Order", 0),
            "is_enabled": row.get("IsEnable"),
            "source_count": row.get("Count", 0),
        } for row in category_rows]
        products = [{
            "id": str(row.get("ID") or ""),
            "supplier_product_id": str(row.get("ID") or ""),
            "category_id": str(row.get("CategoryId") or ""),
            "name": str(row.get("Name") or "未命名商品"),
            "price_cny": str(row.get("Price") if row.get("Price") is not None else "0"),
            "stock": int(row.get("InStock") or 0),
            "is_on_sale": row.get("IsOnSale") is True,
            "min_buy": int(row.get("MinBuy") or 0),
            "max_buy": int(row.get("MaxBuy") or 0),
            "icon": str(row.get("Icon") or ""),
            "sort_order": row.get("Order", 0),
            "is_remote": row.get("IsRemote"),
        } for row in product_rows]
        # Extract mailbox SKUs from the supplier's inconsistent source categories.
        mailbox_terms = ("rambler", "mail.ru", "yandex", "俄罗斯邮箱")
        russian_mail = []
        kept_products = []
        for item in products:
            product_name = item["name"].casefold()
            if any(term in product_name for term in mailbox_terms):
                moved = dict(item)
                moved["original_category_id"] = moved["category_id"]
                moved["category_id"] = "local-russian-email"
                russian_mail.append(moved)
            else:
                kept_products.append(item)
        if russian_mail:
            categories = [
                c for c in categories if c["id"] != "local-russian-email"
            ]
            categories.append({
                "id": "local-russian-email",
                "name": "俄罗斯邮箱",
                "alias": "russianemail",
                "icon": "📧",
                "sort_order": 999999,
                "is_enabled": True,
                "source_count": len(russian_mail),
                "is_local_virtual_category": True,
            })
            products = kept_products + russian_mail

        known_ids = {c["id"] for c in categories}
        orphaned = sum(1 for item in products if item["category_id"] not in known_ids)
        if not categories or not products or orphaned:
            raise RuntimeError(
                f"大号商全量目录校验失败：categories={len(categories)}, "
                f"products={len(products)}, orphaned={orphaned}"
            )
        payload = {
            "source": "Dahaoshang authenticated API",
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "category_count": len(categories),
            "product_count": len(products),
            "orphan_product_count": orphaned,
            "categories": categories,
            "products": products,
        }
        path = Path(__file__).with_name("upstream_catalog.json")
        fd, temp_name = tempfile.mkstemp(prefix=".upstream_catalog.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

        UPSTREAM_CATEGORIES.clear()
        UPSTREAM_PRODUCT_CATALOGS.clear()
        UPSTREAM_CATEGORY_BY_KEY.clear()
        for old_key in list(PRODUCT_CATALOGS):
            if str(old_key).startswith("up"):
                PRODUCT_CATALOGS.pop(old_key, None)
        _load_upstream_catalog()
        return {
            "category_count": len(UPSTREAM_CATEGORIES),
            "product_count": len(products),
            "orphan_product_count": orphaned,
            "synced_at": payload["synced_at"],
            "cache_path": str(path),
        }


def start_supplier_catalog_refresh(interval=300, on_error=None):
    """Refresh from the authenticated supplier API periodically; retain last good cache on failure."""
    global _supplier_refresh_thread_started
    if _supplier_refresh_thread_started:
        return False
    _supplier_refresh_thread_started = True

    def run():
        while True:
            time.sleep(max(60, int(interval)))
            try:
                summary = refresh_supplier_catalog(force=True)
                print(
                    "✅ 大号商全量商品目录定时同步完成："
                    f"{summary['category_count']} 个分类 / {summary['product_count']} 个商品",
                    flush=True,
                )
            except Exception as exc:
                print("⚠️ 大号商商品目录同步失败，保留最近一次成功缓存：", repr(exc), flush=True)
                if callable(on_error):
                    try:
                        on_error(exc)
                    except Exception as alert_exc:
                        print("⚠️ 大号商目录异常告警发送失败：", type(alert_exc).__name__, flush=True)

    threading.Thread(target=run, name="supplier-catalog-refresh", daemon=True).start()
    return True

def get_available_upstream_categories():
    """Return only supplier categories that currently contain in-stock products."""
    return [category for category in UPSTREAM_CATEGORIES if int(category.get("product_count", 0) or 0) > 0]


def get_all_products():
    """Return configured products plus the independent local Russian mailbox SKU."""
    rows = [
        product for products in PRODUCT_CATALOGS.values() for product in products
        if str(product.get("category_id") or "") != "local-russian-email"
    ]
    try:
        from .local_inventory import get_local_products
        rows.extend(get_local_products())
    except Exception as exc:
        print("本地俄罗斯邮箱库存读取失败：", type(exc).__name__, flush=True)
    return rows


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

[executed on device: iZj6cakb7vguedvzvbqrvbZ (14411471-502a-43de-a2d7-530edf7bf831)]
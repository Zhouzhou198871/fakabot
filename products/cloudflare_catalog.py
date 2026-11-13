"""Cloudflare edge catalog client with a safe local-snapshot fallback."""
import json
import threading
import time
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

CATALOG_URL = "https://fakabot-storefront.chulingli1988.workers.dev/v1/catalog"
CATEGORIES_URL = "https://fakabot-storefront.chulingli1988.workers.dev/v1/categories"
_refresh_lock = threading.RLock()
_last_success = 0.0
_refresh_thread_started = False

_ICON_BY_ALIAS = {
    "facebook": "📘", "twitter": "𝕏", "instagram": "📸", "discord": "🎮",
    "tk": "🎵", "tgapi": "📲", "telegram": "✈️", "appleid": "🍎",
    "apple-card": "🎁", "microsoft": "🪟", "google": "✉️", "yahoo": "💜",
    "mail": "📨", "whatsapp": "💬", "line": "🟢", "textnow": "📱",
    "linkedin": "💼", "microsoftteams": "👥", "youtube": "▶️",
    "steam": "🎮", "netflix": "🎬", "other": "🧩", "apisms": "📩",
    "gv": "📞", "aws": "📦", "walmart": "🏬", "twitch": "🟣",
}


def _get_json(url):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "QingTianBotCatalog/1.0"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError("Cloudflare catalog returned HTTP " + str(response.status))
        return json.loads(response.read().decode("utf-8"))


def refresh_cloudflare_catalog(product_catalogs, categories_out, products_out, category_map_out, *, force=False):
    """Atomically replace upstream rows only after validating the full edge response."""
    global _last_success
    now = time.monotonic()
    if not force and now - _last_success < 300:
        return True
    with _refresh_lock:
        now = time.monotonic()
        if not force and now - _last_success < 300:
            return True
        catalog = _get_json(CATALOG_URL)
        category_response = _get_json(CATEGORIES_URL)
        rows = catalog.get("items") or []
        source_categories = category_response.get("items") or []
        if catalog.get("ok") is not True or len(rows) < 500:
            raise RuntimeError("Cloudflare catalog failed minimum row validation")
        if category_response.get("ok") is not True or not source_categories:
            raise RuntimeError("Cloudflare categories failed validation")
        by_name = {str(c.get("name") or ""): c for c in source_categories}
        counts = {}
        valid_rows = []
        seen = set()
        for row in rows:
            supplier_id = str(row.get("supplier_product_id") or "").strip()
            title = str(row.get("title") or "").strip()
            category_name = str(row.get("category") or "").strip()
            if not supplier_id or supplier_id in seen or not title or category_name not in by_name:
                raise RuntimeError("Cloudflare catalog contains invalid identifiers or category")
            seen.add(supplier_id)
            if row.get("is_on_sale") is not True or row.get("stock_status") != "in_stock" or int(row.get("stock") or 0) < 1:
                continue
            cost = Decimal(str(row.get("price_cny") or "0"))
            sell = Decimal(str(row.get("sell_price_usdt") or "0"))
            expected = max(Decimal("0.0001"), (cost / Decimal("4")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
            if cost <= 0 or sell != expected:
                raise RuntimeError("Cloudflare catalog price rule validation failed")
            counts[category_name] = counts.get(category_name, 0) + 1
            valid_rows.append((row, cost, sell))
        if len(valid_rows) < 500:
            raise RuntimeError("Cloudflare catalog has too few in-stock products")
        new_categories = []
        new_products = {}
        new_category_map = {}
        category_key_by_name = {}
        for category in source_categories:
            name = str(category.get("name") or "")
            count = counts.get(name, 0)
            if count < 1:
                continue
            alias = str(category.get("alias") or "").lower()
            key = "up" + str(len(new_categories))
            icon = _ICON_BY_ALIAS.get(alias, "🛍️")
            entry = {**category, "key": key, "product_count": count, "display_icon": icon}
            new_categories.append(entry)
            new_products[key] = []
            new_category_map[key] = entry
            category_key_by_name[name] = (key, str(category.get("category_id") or ""), icon)
        for row, cost, sell in valid_rows:
            name = str(row.get("category") or "")
            key, category_id, icon = category_key_by_name[name]
            supplier_id = str(row["supplier_product_id"])
            cost_text = format(cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
            sell_text = format(sell, ".4f")
            new_products[key].append({
                "id": supplier_id,
                "name": str(row["title"]),
                "price_cny": cost_text,
                "price_usdt": sell_text,
                "price": sell_text,
                "supplier_product_id": supplier_id,
                "supplier_price_cny": cost_text,
                "supplier_url": str(row.get("product_url") or ("https://dahaoshang.com/#/product/" + supplier_id)),
                "supplier_verified": False,
                "procurement_ready": False,
                "is_upstream": True,
                "supplier_stock": int(row.get("stock") or 0),
                "display_icon": icon,
                "category_id": category_id,
                "category_key": key,
                "category_name": name,
                "is_on_sale": True,
                "min_buy": int(row.get("min_buy") or 0),
                "max_buy": int(row.get("max_buy") or 0),
                "description": str(row.get("description") or ""),
                "sales_notice": str(row.get("sales_notice") or ""),
                "synced_at": str(row.get("synced_at") or catalog.get("synced_at") or ""),
            })
        for key in list(product_catalogs):
            if key.startswith("up"):
                product_catalogs.pop(key, None)
        product_catalogs.update(new_products)
        categories_out[:] = new_categories
        products_out.clear()
        products_out.update(new_products)
        category_map_out.clear()
        category_map_out.update(new_category_map)
        _last_success = time.monotonic()
        return True


def start_refresh_loop(refresh_callable, interval=300):
    """Start one lightweight background refresher; failures keep the last good snapshot."""
    global _refresh_thread_started
    if _refresh_thread_started:
        return False
    _refresh_thread_started = True

    def run():
        while True:
            time.sleep(max(60, int(interval)))
            try:
                refresh_callable(force=True)
            except Exception as exc:
                print("Cloudflare catalog refresh failed; keeping last good catalog:", repr(exc), flush=True)

    threading.Thread(target=run, name="cloudflare-catalog-refresh", daemon=True).start()
    return True
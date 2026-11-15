"""Fetch the complete live Dahaoshang catalog into the local JSON cache."""
import json
import os
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

from supplier_client import DahaoshangClient
TARGET = ROOT / "upstream_catalog.json"


def main():
    client = DahaoshangClient()
    categories_raw = client.get_categories()
    products_raw = client.get_product_list("0")

    categories = []
    for row in categories_raw:
        categories.append({
            "id": str(row.get("ID") or ""),
            "name": str(row.get("Name") or "未命名分类"),
            "alias": str(row.get("Alias") or ""),
            "icon": str(row.get("Icon") or ""),
            "sort_order": row.get("Order", 0),
            "is_enabled": row.get("IsEnable"),
            "source_count": row.get("Count", 0),
        })

    products = []
    for row in products_raw:
        products.append({
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
        })

    known_ids = {c["id"] for c in categories}
    orphaned = sum(1 for p in products if p["category_id"] not in known_ids)
    payload = {
        "source": "Dahaoshang authenticated API",
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "category_count": len(categories),
        "product_count": len(products),
        "orphan_product_count": orphaned,
        "categories": categories,
        "products": products,
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".upstream_catalog.", suffix=".tmp", dir=str(TARGET.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, TARGET)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    by_id = {c["id"]: c for c in categories}
    print("SYNC_OK")
    print("CATEGORIES", len(categories))
    print("PRODUCTS", len(products))
    print("ORPHAN_PRODUCTS", orphaned)
    for cat in categories:
        rows = [p for p in products if p["category_id"] == cat["id"]]
        live = [p for p in rows if p["is_on_sale"] and p["stock"] > 0]
        print(f'{cat["name"]}\t{cat["alias"]}\tproducts={len(rows)}\ton_sale_in_stock={len(live)}\tstock_units={sum(p["stock"] for p in live)}')
    print("CACHE", str(TARGET), "BYTES", TARGET.stat().st_size)


if __name__ == "__main__":
    main()

[executed on device: iZj6cakb7vguedvzvbqrvbZ (14411471-502a-43de-a2d7-530edf7bf831)]
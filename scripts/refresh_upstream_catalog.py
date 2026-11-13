#!/usr/bin/env python3
"""Refresh the public Dahaoshang catalog snapshot using read-only GET endpoints."""
import json
import os
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supplier_client import DahaoshangClient


def main():
    client = DahaoshangClient()
    client.get_user_info()  # validate the existing session; no account mutation
    raw_categories = client.get_categories()
    raw_products = client.get_product_list("0")
    categories = []
    category_ids = set()
    for row in raw_categories:
        category_id = str(row.get("ID") or "")
        if not category_id:
            continue
        category_ids.add(category_id)
        categories.append({
            "id": category_id,
            "name": str(row.get("Name") or "其他商品"),
            "alias": str(row.get("Alias") or "").lower(),
            "icon": str(row.get("Icon") or ""),
            "count": int(row.get("Count") or 0),
            "enabled": bool(row.get("IsEnable", True)),
        })
    category_names = {row["id"]: row["name"] for row in categories}
    products = []
    for row in raw_products:
        product_id = str(row.get("ID") or "")
        category_id = str(row.get("CategoryId") or "")
        if not product_id or category_id not in category_ids:
            continue
        try:
            price = float(row.get("Price") or 0)
            stock = int(row.get("InStock") or 0)
        except (TypeError, ValueError):
            continue
        products.append({
            "id": product_id,
            "supplier_product_id": product_id,
            "name": str(row.get("Name") or "未命名商品"),
            "icon": str(row.get("Icon") or ""),
            "price_cny": price,
            "stock": stock,
            "category_id": category_id,
            "category_name": category_names.get(category_id, "其他商品"),
            "is_on_sale": row.get("IsOnSale") is True,
            "is_recommend": row.get("IsRecommend") is True,
            "is_hot": row.get("IsHot") is True,
            "is_remote": row.get("IsRemote") is True,
            "remote_price": row.get("RemotePrice") or 0,
            "add_price_factor": row.get("AddPriceFactor") or 0,
            "min_buy": int(row.get("MinBuy") or 0),
            "max_buy": int(row.get("MaxBuy") or 0),
            "raw": row,
        })
    payload = {
        "source": "dahaoshang_live_api",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "price_currency": "CNY",
        "categories": categories,
        "products": products,
    }
    destination = Path(__file__).resolve().parents[1] / "products" / "upstream_catalog.json"
    fd, temp_path = tempfile.mkstemp(prefix="upstream_catalog.", suffix=".tmp", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    active = sum(x["is_on_sale"] and x["stock"] > 0 and x["price_cny"] > 0 for x in products)
    print(f"categories={len(categories)} products={len(products)} sale_and_stock={active}")
    print("snapshot_updated=true purchase_requests_sent=0")


if __name__ == "__main__":
    main()
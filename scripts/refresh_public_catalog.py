#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh the supplier's public catalog without a supplier login session.

Only read-only GET endpoints are used. No purchase, payment, account mutation,
or authenticated endpoint is called. The Cloudflare Worker remains the
production scheduler; this script is a manual/fallback snapshot publisher.
"""
import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "products" / "upstream_catalog.json"
BASE = "https://dahaoshang.com"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (compatible; FakabotPublicCatalog/1.0)",
    "Origin": BASE,
    "Referer": BASE + "/",
}


def get_json(path):
    request = Request(BASE + path, headers=HEADERS, method="GET")
    try:
        with urlopen(request, timeout=25) as response:
            if response.status != 200:
                raise RuntimeError("supplier catalog HTTP " + str(response.status))
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError("supplier catalog HTTP " + str(exc.code)) from exc
    except URLError as exc:
        raise RuntimeError("supplier catalog network error: " + type(exc).__name__) from exc
    if not isinstance(payload, dict) or payload.get("Code") != 200:
        raise RuntimeError("supplier public API returned an unsuccessful response")
    return payload.get("Data")


def build_snapshot():
    category_rows = get_json("/api/syscategory/getcategorylist")
    product_rows = get_json("/api/sysproduct/getproductlist?categoryId=0")
    if not isinstance(category_rows, list) or not isinstance(product_rows, list):
        raise RuntimeError("supplier public API response shape changed")
    categories = []
    category_names = {}
    for row in category_rows:
        category_id = str(row.get("ID") or "").strip()
        if not category_id:
            continue
        name = str(row.get("Name") or "其他商品").strip()
        category_names[category_id] = name
        categories.append({
            "id": category_id,
            "name": name,
            "alias": str(row.get("Alias") or "").lower(),
            "icon": str(row.get("Icon") or ""),
            "count": int(row.get("Count") or 0),
            "enabled": row.get("IsEnable") is True,
        })
    products = []
    seen = set()
    for row in product_rows:
        product_id = str(row.get("ID") or "").strip()
        category_id = str(row.get("CategoryId") or "").strip()
        if not product_id or product_id in seen or category_id not in category_names:
            continue
        seen.add(product_id)
        try:
            price = Decimal(str(row.get("Price") or "0"))
            stock = int(row.get("InStock") or 0)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not price.is_finite() or price < 0 or stock < 0:
            continue
        products.append({
            "id": product_id,
            "supplier_product_id": product_id,
            "name": str(row.get("Name") or "未命名商品"),
            "icon": str(row.get("Icon") or ""),
            "price_cny": float(price),
            "stock": stock,
            "category_id": category_id,
            "category_name": category_names[category_id],
            "is_on_sale": row.get("IsOnSale") is True,
            "is_recommend": row.get("IsRecommend") is True,
            "is_hot": row.get("IsHot") is True,
            "is_remote": row.get("IsRemote") is True,
            "remote_price": row.get("RemotePrice") or 0,
            "add_price_factor": row.get("AddPriceFactor") or 0,
            "min_buy": int(row.get("MinBuy") or 0),
            "max_buy": int(row.get("MaxBuy") or 0),
            "description": "",
            "variants": [],
            "raw": row,
        })
    active = sum(p["is_on_sale"] and p["stock"] > 0 and p["price_cny"] > 0 for p in products)
    if len(categories) < 1 or len(products) < 500 or active < 500:
        raise RuntimeError("catalog safety validation failed; previous snapshot retained")
    return {
        "source": "dahaoshang_public_read_only_api",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "price_currency": "CNY",
        "auth_required": False,
        "categories": categories,
        "products": products,
    }


def atomic_write(payload):
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="upstream_catalog.", suffix=".tmp", dir=str(DESTINATION.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, DESTINATION)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def publish_to_cloudflare():
    # Trigger the Worker-side public-API refresh. The Worker fetches the same
    # read-only endpoints and writes a complete versioned D1 snapshot.
    token_path = ROOT / ".catalog_sync_token"
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 48:
        raise RuntimeError("Cloudflare catalog sync token is missing or invalid")
    request = Request(
        "https://fakabot-procurement.chulingli1988.workers.dev/_internal/refresh",
        data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token, "User-Agent": "FakabotCatalogSync/1.0"},
    )
    with urlopen(request, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("ok") is not True:
        raise RuntimeError("Cloudflare public catalog refresh failed")
    return {k: result.get(k) for k in ("ok", "synced_at", "products", "categories", "active_products")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="publish the validated snapshot to Cloudflare D1")
    args = parser.parse_args()
    payload = build_snapshot()
    atomic_write(payload)
    active = sum(p["is_on_sale"] and p["stock"] > 0 and p["price_cny"] > 0 for p in payload["products"])
    result = {
        "auth_required": False,
        "categories": len(payload["categories"]),
        "products": len(payload["products"]),
        "active_products": active,
        "snapshot": str(DESTINATION),
        "purchase_requests_sent": 0,
    }
    if args.publish:
        result["cloudflare"] = publish_to_cloudflare()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
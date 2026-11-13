#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only supplier session and balance-payment preflight. Never buys."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supplier_client import DahaoshangClient, SupplierError


def main():
    try:
        client = DahaoshangClient()
        client.get_user_info()
        categories = client.get_categories()
        products = client.get_product_list("0")
        candidate = next((
            row for row in products
            if row.get("IsOnSale") is True
            and int(row.get("InStock") or 0) > 0
            and float(row.get("Price") or 0) > 0
            and row.get("CategoryId")
        ), None)
        if not candidate:
            raise SupplierError("没有找到可用于只读支付方式检查的有货商品")
        methods = client.get_payment_methods(candidate["CategoryId"])
        try:
            client._choose_balance_method(methods)
            balance_available = True
        except SupplierError:
            balance_available = False
        print(json.dumps({
            "session_decrypted": True,
            "supplier_account_authenticated": True,
            "categories": len(categories),
            "products": len(products),
            "balance_payment_available": balance_available,
            "payment_method_count": len(methods),
            "purchase_requests_sent": 0,
            "procurement_enabled": False,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            "session_decrypted": False,
            "supplier_account_authenticated": False,
            "error_type": type(exc).__name__,
            "purchase_requests_sent": 0,
            "procurement_enabled": False,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
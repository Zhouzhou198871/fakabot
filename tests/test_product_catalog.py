import unittest
from decimal import Decimal, ROUND_HALF_UP

from products.catalog import PRODUCT_CATALOGS, SALES_POLICY, UPSTREAM_CATEGORIES, get_all_products, get_available_upstream_categories


class ProductCatalogTests(unittest.TestCase):
    def test_sales_policy_is_finished_accounts_only_and_no_returns(self):
        self.assertEqual(SALES_POLICY["product_scope"], "finished_accounts_only")
        self.assertFalse(SALES_POLICY["customization_allowed"])
        self.assertFalse(SALES_POLICY["returns_allowed"])
        self.assertFalse(SALES_POLICY["refunds_allowed"])
        self.assertIn("不支持退货", SALES_POLICY["customer_notice"])
        self.assertIn("供应商标题中的售后标注", SALES_POLICY["customer_notice"])

    def test_registered_categories_include_static_and_live_supplier_rows(self):
        for key in ("apple", "tg", "instagram", "ai"):
            self.assertIn(key, PRODUCT_CATALOGS)
            self.assertGreater(len(PRODUCT_CATALOGS[key]), 0)
        self.assertNotIn("node", PRODUCT_CATALOGS)
        self.assertEqual(len(UPSTREAM_CATEGORIES), 28)
        available = get_available_upstream_categories()
        self.assertGreater(len(available), 0)
        self.assertLessEqual(len(available), len(UPSTREAM_CATEGORIES))
        self.assertTrue(all(category["product_count"] > 0 for category in available))
        self.assertGreater(sum(len(rows) for key, rows in PRODUCT_CATALOGS.items() if key.startswith("up")), 1000)

    def test_product_ids_are_unique(self):
        products = get_all_products()
        ids = [item["id"] for item in products]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_product_has_name_and_positive_usdt_price(self):
        for category, products in PRODUCT_CATALOGS.items():
            with self.subTest(category=category):
                for product in products:
                    self.assertTrue(product.get("name"))
                    self.assertGreater(float(product["price"]), 0)

    def test_upstream_price_uses_cny_cost_divided_by_four(self):
        products = [p for p in get_all_products() if p.get("is_upstream")]
        self.assertGreater(len(products), 1000)
        for product in products:
            expected = max(
                Decimal("0.0001"),
                (Decimal(product["price_cny"]) / Decimal("4")).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                ),
            )
            self.assertEqual(Decimal(product["price"]), expected)
            self.assertFalse(product["supplier_verified"])
            self.assertFalse(product["procurement_ready"])


    def test_low_cost_items_keep_exact_100_percent_markup_precision(self):
        product = next(p for p in get_all_products()
                       if p.get("is_upstream") and p.get("supplier_product_id") == "1494279121381179686")
        self.assertEqual(Decimal(product["price_cny"]), Decimal("0.05"))
        self.assertEqual(Decimal(product["price"]), Decimal("0.0125"))


    def test_inventory_is_not_exposed_as_a_bot_local_stock_counter(self):
        for products in PRODUCT_CATALOGS.values():
            for product in products:
                self.assertNotIn("stock", product)
                self.assertNotIn("inventory", product)


if __name__ == "__main__":
    unittest.main()
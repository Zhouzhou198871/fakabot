import unittest
from decimal import Decimal, ROUND_HALF_UP

from products.catalog import PRODUCT_CATALOGS, UPSTREAM_CATEGORIES, get_all_products


class ProductCatalogTests(unittest.TestCase):
    def test_registered_categories_include_static_and_live_supplier_rows(self):
        for key in ("apple", "tg", "instagram", "ai", "node"):
            self.assertIn(key, PRODUCT_CATALOGS)
            self.assertGreater(len(PRODUCT_CATALOGS[key]), 0)
        self.assertEqual(len(UPSTREAM_CATEGORIES), 28)
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
                Decimal("0.01"),
                (Decimal(product["price_cny"]) / Decimal("4")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
            )
            self.assertEqual(Decimal(product["price"]), expected)
            self.assertFalse(product["supplier_verified"])
            self.assertFalse(product["procurement_ready"])

    def test_inventory_is_not_exposed_as_a_bot_local_stock_counter(self):
        for products in PRODUCT_CATALOGS.values():
            for product in products:
                self.assertNotIn("stock", product)
                self.assertNotIn("inventory", product)


if __name__ == "__main__":
    unittest.main()

import unittest

from products.catalog import PRODUCT_CATALOGS, get_all_products


class ProductCatalogTests(unittest.TestCase):
    def test_registered_categories_have_real_rows(self):
        self.assertEqual(
            {key: len(value) for key, value in PRODUCT_CATALOGS.items()},
            {"apple": 25, "tg": 5, "instagram": 27, "ai": 1, "node": 3},
        )

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

    def test_inventory_is_not_part_of_display_catalog_rows(self):
        for products in PRODUCT_CATALOGS.values():
            for product in products:
                self.assertNotIn("stock", product)
                self.assertNotIn("inventory", product)


if __name__ == "__main__":
    unittest.main()

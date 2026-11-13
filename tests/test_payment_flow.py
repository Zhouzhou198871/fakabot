import unittest
from payment_flow import create_payment_order, handle_confirmed_payment

class PaymentFlowTests(unittest.TestCase):
    def setUp(self):
        self.orders = []

    def load(self):
        return [dict(x) for x in self.orders]

    def save(self, rows):
        self.orders = [dict(x) for x in rows]

    def test_unverified_product_is_blocked_before_payment_order_creation(self):
        with self.assertRaisesRegex(Exception, "自动采购验证"):
            create_payment_order(101, {"id":"p1","name":"商品","price":"2.00","supplier_verified":False}, self.load, self.save)
        self.assertEqual(self.orders, [])

    def test_verified_catalog_without_ready_procurement_is_blocked(self):
        product = {"id":"p1","name":"商品","price":"2.00","supplier_verified":True,"procurement_ready":False,"supplier_product_id":"DH-100","supplier_url":"https://dahaoshang.com/item/100","supplier_price_cny":"8.00"}
        with self.assertRaisesRegex(Exception, "自动采购验证"):
            create_payment_order(101, product, self.load, self.save)
        self.assertEqual(self.orders, [])

    def test_supplier_cost_must_match_two_x_markup(self):
        product = {
            "id":"p1","name":"商品","price":"2.01",
            "supplier_verified":True,"procurement_ready":True,
            "supplier_product_id":"DH-100","supplier_url":"https://dahaoshang.com/item/100",
            "supplier_price_cny":"8.00"
        }
        with self.assertRaisesRegex(Exception, "利润规则不一致"):
            create_payment_order(101, product, self.load, self.save)
        self.assertEqual(self.orders, [])

    def test_order_uses_unique_exact_six_decimal_amount(self):
        import os
        old = os.environ.get("TRON_RECEIVE_ADDRESS")
        os.environ["TRON_RECEIVE_ADDRESS"] = "T" + "A" * 33
        try:
            product = {"id":"p1","name":"商品","price":"2.00","supplier_verified":True,"procurement_ready":True,"supplier_product_id":"DH-100","supplier_url":"https://dahaoshang.com/item/100","supplier_price_cny":"8.00"}
            first, _ = create_payment_order(101, product, self.load, self.save)
            second, _ = create_payment_order(102, product, self.load, self.save)
            self.assertNotEqual(first["amount_raw"], second["amount_raw"])
            self.assertEqual(len(first["amount_raw"]), len(str(int(float(first["amount"]) * 1000000))))
            self.assertEqual(first["payment_status"], "pending")
        finally:
            if old is None: os.environ.pop("TRON_RECEIVE_ADDRESS", None)
            else: os.environ["TRON_RECEIVE_ADDRESS"] = old

    def test_confirmed_payment_is_idempotent(self):
        self.orders = [{"order_id":"QT123","user_id":"1","amount":2.000001,"payment_status":"pending","status":"pending_payment"}]
        class FakeBot:
            def __init__(self): self.messages=[]
            def send_message(self, *args, **kwargs): self.messages.append((args,kwargs))
        bot = FakeBot()
        transfer = {"txid":"abc","from":"Tfrom","to":"Tto","amount_raw":"2000001","block_timestamp_ms":123,"contract":"contract"}
        self.assertTrue(handle_confirmed_payment(bot,"QT123",transfer,self.load,self.save,""))
        self.assertFalse(handle_confirmed_payment(bot,"QT123",transfer,self.load,self.save,""))
        self.assertEqual(self.orders[0]["payment_status"],"paid")
        self.assertEqual(self.orders[0]["txid"],"abc")

if __name__ == "__main__": unittest.main()
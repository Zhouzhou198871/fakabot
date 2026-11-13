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

    def test_paid_order_auto_delivers_once_when_explicitly_enabled(self):
        import os
        from payment_flow import handle_confirmed_payment
        old = os.environ.get("SUPPLIER_AUTOPROCUREMENT_ENABLED")
        os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = "1"
        class FakeSupplier:
            def __init__(self): self.calls = 0
            def buy_with_balance(self, product_id, quantity=1, model_id=None, email=""):
                self.calls += 1
                return {
                    "supplier_order_id": "UP-1",
                    "supplier_order_no": "NO-1",
                    "content": "test-delivery-content",
                }
        class FakeBot:
            def __init__(self): self.messages = []
            def send_message(self, *args, **kwargs): self.messages.append((args, kwargs))
        supplier, bot = FakeSupplier(), FakeBot()
        self.orders = [{
            "order_id": "QT-AUTO-1", "user_id": "1", "amount": 2.000001,
            "payment_status": "pending", "status": "pending_payment",
            "supplier_product_id": "P1", "supplier_quantity": 1,
        }]
        try:
            self.assertTrue(handle_confirmed_payment(
                bot, "QT-AUTO-1",
                {"txid": "tx-auto", "amount_raw": "2000001"},
                self.load, self.save, supplier_client_factory=lambda: supplier,
            ))
            self.assertEqual(self.orders[0]["procurement_status"], "delivered")
            self.assertEqual(self.orders[0]["status"], "delivered")
            self.assertEqual(self.orders[0]["delivery_content"], "test-delivery-content")
            self.assertEqual(supplier.calls, 1)
            self.assertFalse(handle_confirmed_payment(
                bot, "QT-AUTO-1", {"txid": "tx-auto"},
                self.load, self.save, supplier_client_factory=lambda: supplier,
            ))
            self.assertEqual(supplier.calls, 1)
            self.assertTrue(any("test-delivery-content" in str(x) for x in bot.messages))
        finally:
            if old is None: os.environ.pop("SUPPLIER_AUTOPROCUREMENT_ENABLED", None)
            else: os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = old

    def test_supplier_order_pending_delivery_is_never_purchased_twice(self):
        import os
        from payment_flow import handle_confirmed_payment
        from supplier_client import SupplierDeliveryPending
        old = os.environ.get("SUPPLIER_AUTOPROCUREMENT_ENABLED")
        os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = "1"
        class FakeSupplier:
            def __init__(self): self.calls = 0
            def buy_with_balance(self, *args, **kwargs):
                self.calls += 1
                raise SupplierDeliveryPending("UP-PENDING-1")
        class FakeBot:
            def send_message(self, *args, **kwargs): pass
        supplier = FakeSupplier()
        self.orders = [{
            "order_id": "QT-AUTO-2", "user_id": "1", "amount": 2.000001,
            "payment_status": "pending", "status": "pending_payment",
            "supplier_product_id": "P1", "supplier_quantity": 1,
        }]
        try:
            self.assertTrue(handle_confirmed_payment(
                FakeBot(), "QT-AUTO-2", {"txid": "tx-auto-2"},
                self.load, self.save, supplier_client_factory=lambda: supplier,
            ))
            self.assertEqual(self.orders[0]["procurement_status"], "supplier_order_pending_delivery")
            self.assertEqual(self.orders[0]["supplier_order_id"], "UP-PENDING-1")
            self.assertFalse(handle_confirmed_payment(
                FakeBot(), "QT-AUTO-2", {"txid": "tx-auto-2"},
                self.load, self.save, supplier_client_factory=lambda: supplier,
            ))
            self.assertEqual(supplier.calls, 1)
        finally:
            if old is None: os.environ.pop("SUPPLIER_AUTOPROCUREMENT_ENABLED", None)
            else: os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = old


    def test_pending_supplier_order_is_reconciled_without_repurchase(self):
        from payment_flow import reconcile_pending_supplier_deliveries
        class FakeSupplier:
            def __init__(self): self.reads = []; self.purchases = 0
            def get_order(self, order_id):
                self.reads.append(order_id)
                return {"supplier_order_id": order_id, "content": "recovered-delivery", "supplier_status": "已发货"}
            def buy_with_balance(self, *args, **kwargs):
                self.purchases += 1
                raise AssertionError("reconciliation must never buy again")
        class FakeBot:
            def __init__(self): self.messages = []
            def send_message(self, *args, **kwargs): self.messages.append((args, kwargs))
        supplier, bot = FakeSupplier(), FakeBot()
        self.orders = [{
            "order_id": "QT-RECOVER-1", "user_id": "1", "payment_status": "paid",
            "status": "paid", "procurement_status": "supplier_order_pending_delivery",
            "supplier_order_id": "UP-EXISTING-1",
        }]
        count = reconcile_pending_supplier_deliveries(
            bot, self.load, self.save, supplier_client_factory=lambda: supplier,
        )
        self.assertEqual(count, 1)
        self.assertEqual(supplier.reads, ["UP-EXISTING-1"])
        self.assertEqual(supplier.purchases, 0)
        self.assertEqual(self.orders[0]["procurement_status"], "delivered")
        self.assertEqual(self.orders[0]["delivery_content"], "recovered-delivery")
        self.assertTrue(any("recovered-delivery" in str(x) for x in bot.messages))


if __name__ == "__main__":
    unittest.main()

[executed on device: iZj6cakb7vguedvzvbqrvbZ (14411471-502a-43de-a2d7-530edf7bf831)]

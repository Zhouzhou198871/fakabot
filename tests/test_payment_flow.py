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

    def test_supplier_price_validation_uses_four_decimal_markup(self):
        import os
        old = os.environ.get("TRON_RECEIVE_ADDRESS")
        os.environ["TRON_RECEIVE_ADDRESS"] = "T" + "A" * 33
        product = {"id":"p-low","name":"低价商品","price":"0.0125",
            "supplier_verified":True,"procurement_ready":True,
            "supplier_product_id":"DH-LOW","supplier_url":"https://dahaoshang.com/item/low",
            "supplier_price_cny":"0.05"}
        try:
            order, _ = create_payment_order(101, product, self.load, self.save)
            self.assertEqual(order["supplier_price_cny"], "0.05")
        finally:
            if old is None: os.environ.pop("TRON_RECEIVE_ADDRESS", None)
            else: os.environ["TRON_RECEIVE_ADDRESS"] = old

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

    def test_end_to_end_simulated_order_payment_procurement_and_telegram_delivery(self):
        """Exercise the complete business chain without a real transfer or supplier charge."""
        import os
        from decimal import Decimal
        old_switch = os.environ.get("SUPPLIER_AUTOPROCUREMENT_ENABLED")
        old_address = os.environ.get("TRON_RECEIVE_ADDRESS")
        os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = "1"
        os.environ["TRON_RECEIVE_ADDRESS"] = "T" + "A" * 33
        class FakeSupplier:
            def __init__(self): self.purchases=0
            def buy_with_balance(self, product_id, quantity=1, model_id=None, email="", expected_price_cny=None):
                self.purchases += 1
                self.assertions = (product_id, quantity, model_id, email)
                return {"supplier_order_id":"UP-E2E-1","supplier_order_no":"NO-E2E-1",
                        "content":"SIMULATED-CARD-CODE-123"}
        class FakeBot:
            def __init__(self): self.messages=[]
            def send_message(self, *args, **kwargs): self.messages.append((args,kwargs))
        supplier, bot = FakeSupplier(), FakeBot()
        product = {"id":"P-E2E","name":"E2E测试商品","price":"0.0125",
            "supplier_verified":True,"procurement_ready":True,
            "supplier_product_id":"DH-E2E","supplier_url":"https://dahaoshang.com/item/e2e",
            "supplier_price_cny":"0.05"}
        try:
            order, _ = create_payment_order(12345, product, self.load, self.save)
            raw_amount = str(int((Decimal(str(order["amount"])) * Decimal("1000000")).to_integral_value()))
            transfer = {"txid":"SIMULATED-TRON-TX","amount_raw":raw_amount,
                "from":"T-SIMULATED-SENDER","to":"T-SIMULATED-RECEIVER",
                "block_timestamp_ms":123456789,"contract":"SIMULATED-USDT-CONTRACT"}
            self.assertTrue(handle_confirmed_payment(bot, order["order_id"], transfer,
                self.load, self.save, supplier_client_factory=lambda:supplier))
            saved = self.orders[0]
            self.assertEqual(saved["payment_status"],"paid")
            self.assertEqual(saved["procurement_status"],"delivered")
            self.assertEqual(saved["supplier_order_id"],"UP-E2E-1")
            self.assertEqual(saved["delivery_content"],"SIMULATED-CARD-CODE-123")
            self.assertEqual(saved["delivery_notification_status"],"sent")
            self.assertEqual(supplier.purchases,1)
            self.assertTrue(any("SIMULATED-CARD-CODE-123" in str(x) for x in bot.messages))
            self.assertFalse(handle_confirmed_payment(bot, order["order_id"], transfer,
                self.load, self.save, supplier_client_factory=lambda:supplier))
            self.assertEqual(supplier.purchases,1)
        finally:
            if old_switch is None: os.environ.pop("SUPPLIER_AUTOPROCUREMENT_ENABLED",None)
            else: os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"]=old_switch
            if old_address is None: os.environ.pop("TRON_RECEIVE_ADDRESS",None)
            else: os.environ["TRON_RECEIVE_ADDRESS"]=old_address

    def test_paid_order_auto_delivers_once_when_explicitly_enabled(self):
        import os
        from payment_flow import handle_confirmed_payment
        old = os.environ.get("SUPPLIER_AUTOPROCUREMENT_ENABLED")
        os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = "1"
        class FakeSupplier:
            def __init__(self): self.calls = 0
            def buy_with_balance(self, product_id, quantity=1, model_id=None, email="", expected_price_cny=None):
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
            "supplier_verified": True, "procurement_ready": True,
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
            "supplier_verified": True, "procurement_ready": True,
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


    def test_reconciled_supplier_delivery_notification_failure_is_retried_without_purchase(self):
        from payment_flow import reconcile_pending_supplier_deliveries
        class FakeSupplier:
            def __init__(self): self.reads=[]; self.purchases=0
            def get_order(self, order_id):
                self.reads.append(order_id)
                return {"supplier_order_id":order_id,"content":"late-secret","supplier_status":"已发货"}
            def buy_with_balance(self, *args, **kwargs): self.purchases += 1; raise AssertionError("must not repurchase")
        class FlakyBot:
            def __init__(self): self.calls=0; self.messages=[]
            def send_message(self,*args,**kwargs):
                self.calls += 1
                if self.calls == 1: raise RuntimeError("Telegram down")
                self.messages.append((args,kwargs))
        supplier, bot=FakeSupplier(),FlakyBot()
        self.orders=[{"order_id":"QT-LATE-NOTIFY","user_id":"123","payment_status":"paid",
            "procurement_status":"supplier_order_pending_delivery","supplier_order_id":"UP-LATE"}]
        try:
            self.assertEqual(reconcile_pending_supplier_deliveries(bot,self.load,self.save,
                supplier_client_factory=lambda:supplier),1)
            self.assertEqual(self.orders[0]["delivery_notification_status"],"pending")
            self.assertEqual(supplier.purchases,0)
            reconcile_pending_supplier_deliveries(bot,self.load,self.save,
                supplier_client_factory=lambda:supplier)
            self.assertEqual(self.orders[0]["delivery_notification_status"],"sent")
            self.assertEqual(supplier.purchases,0)
            self.assertTrue(any("late-secret" in str(x) for x in bot.messages))
        finally:
            pass

    def test_long_delivery_is_split_into_telegram_safe_plain_text_chunks(self):
        from payment_flow import _notify
        class FakeBot:
            def __init__(self): self.messages=[]
            def send_message(self, *args, **kwargs): self.messages.append((args, kwargs))
        bot = FakeBot()
        content = "A" * 8000
        self.assertTrue(_notify(bot, "123", content))
        self.assertGreaterEqual(len(bot.messages), 3)
        self.assertTrue(all(len(x[0][1]) <= 3500 for x in bot.messages))
        self.assertTrue(all(x[1].get("parse_mode") is None for x in bot.messages))
        self.assertEqual("".join(x[0][1] for x in bot.messages), content)

    def test_failed_telegram_delivery_is_marked_pending_and_retried_without_repurchase(self):
        import os
        from payment_flow import handle_confirmed_payment, reconcile_pending_supplier_deliveries
        old = os.environ.get("SUPPLIER_AUTOPROCUREMENT_ENABLED")
        os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = "1"
        class FakeSupplier:
            def __init__(self): self.purchases=0
            def buy_with_balance(self, *args, **kwargs):
                self.purchases += 1
                return {"supplier_order_id":"UP-NOTIFY-1", "content":"secret-delivery"}
        class FlakyBot:
            def __init__(self): self.calls=0; self.messages=[]
            def send_message(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1: raise RuntimeError("temporary Telegram failure")
                self.messages.append((args, kwargs))
        supplier, bot = FakeSupplier(), FlakyBot()
        self.orders = [{"order_id":"QT-NOTIFY-1", "user_id":"123", "amount":1.0,
            "payment_status":"pending", "status":"pending_payment", "supplier_product_id":"P1",
            "supplier_verified":True, "procurement_ready":True}]
        try:
            self.assertTrue(handle_confirmed_payment(bot, "QT-NOTIFY-1", {"txid":"tx"},
                self.load, self.save, supplier_client_factory=lambda: supplier))
            self.assertEqual(self.orders[0]["procurement_status"], "delivered")
            self.assertEqual(self.orders[0]["delivery_notification_status"], "pending")
            self.assertEqual(supplier.purchases, 1)
            reconcile_pending_supplier_deliveries(bot, self.load, self.save,
                supplier_client_factory=lambda: supplier)
            self.assertEqual(self.orders[0]["delivery_notification_status"], "sent")
            self.assertEqual(supplier.purchases, 1)
            self.assertTrue(any("secret-delivery" in str(x) for x in bot.messages))
        finally:
            if old is None: os.environ.pop("SUPPLIER_AUTOPROCUREMENT_ENABLED", None)
            else: os.environ["SUPPLIER_AUTOPROCUREMENT_ENABLED"] = old



if __name__ == "__main__":
    unittest.main()


class DualPaymentTests(unittest.TestCase):
    def test_fixed_exchange_rate_and_alipay_order(self):
        import os
        from payment_flow import price_cny, create_alipay_order
        self.assertEqual(str(price_cny("1.25")), "10.00")
        orders = []
        product = {"id":"p-alipay", "name":"测试商品", "price":"2.00",
            "supplier_verified":True, "procurement_ready":True,
            "supplier_product_id":"DH-1", "supplier_url":"https://example.com/item",
            "supplier_price_cny":"8.00"}
        from unittest.mock import patch
        with patch("alipay_gateway.build_page_pay_url", return_value="https://sandbox.example/pay?out_trade_no=ALTEST"):
            order, text = create_alipay_order(123, product, lambda: list(orders),
                lambda rows: orders.__setitem__(slice(None), rows))
        self.assertEqual(order["payment_method"], "alipay_gateway")
        self.assertEqual(order["amount_cny"], "16.00")
        self.assertEqual(order["exchange_rate"], "8")
        self.assertEqual(order["payment_verification"], "provider_notification_required")
        self.assertIn("¥16.00", text)
        self.assertEqual(order["payment_url"], "https://sandbox.example/pay?out_trade_no=ALTEST")
        self.assertEqual(order["payment_status"], "pending")
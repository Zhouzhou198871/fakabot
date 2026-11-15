import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class ProductionSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("BOT_TOKEN", "123456:placeholder")
        os.environ.setdefault("ADMIN_ID", "7867116527")
        import bot
        cls.bot_module = bot

    def test_purchase_and_query_debounce(self):
        bot = self.bot_module
        uid = "safety-test-user"
        self.assertTrue(bot._action_debounce_allowed(uid, "purchase", 3.0))
        self.assertFalse(bot._action_debounce_allowed(uid, "purchase", 3.0))
        self.assertTrue(bot._action_debounce_allowed(uid, "product_search", 3.0))
        self.assertFalse(bot._action_debounce_allowed(uid, "product_search", 3.0))

    def test_critical_alert_dispatches_to_admin_without_blocking_caller(self):
        bot = self.bot_module
        delivered = threading.Event()
        captured = []
        original = bot.bot.send_message

        def fake_send(chat_id, text, **kwargs):
            captured.append((chat_id, text))
            delivered.set()

        bot.bot.send_message = fake_send
        try:
            self.assertTrue(bot._notify_admin_critical(
                "unit-test-critical", RuntimeError("archive write failed"), "unit test"
            ))
            self.assertTrue(delivered.wait(2))
        finally:
            bot.bot.send_message = original
        self.assertEqual(captured[0][0], 7867116527)
        self.assertIn("系统严重异常告警", captured[0][1])

    def test_archive_persist_lock_has_a_timeout(self):
        from telegram_storage import TelegramArchiveStore

        class BusyLock:
            def acquire(self, timeout=None):
                return False
            def release(self):
                raise AssertionError("release must not run when acquire fails")

        store = TelegramArchiveStore(bot=self.bot_module.bot, chat_id=-1004459109788)
        store.persist_lock = BusyLock()
        with self.assertRaises(TimeoutError):
            store.flush("unit test")

    def test_inventory_file_lock_has_a_timeout_and_alert_callback(self):
        import products.local_inventory as inventory

        alerts = []
        with tempfile.TemporaryDirectory() as directory:
            old_path = inventory.INVENTORY_PATH
            inventory.INVENTORY_PATH = Path(directory) / "inventory.json"
            inventory.set_critical_error_handler(alerts.append)
            try:
                with patch("products.local_inventory.fcntl.flock", side_effect=BlockingIOError), \
                     patch("products.local_inventory.time.monotonic", side_effect=[0.0, 20.0]), \
                     patch("products.local_inventory.time.sleep", return_value=None):
                    with self.assertRaises(TimeoutError):
                        with inventory._file_lock():
                            self.fail("lock unexpectedly acquired")
            finally:
                inventory.INVENTORY_PATH = old_path
                inventory.set_critical_error_handler(None)
        self.assertEqual(len(alerts), 1)
        self.assertIsInstance(alerts[0], TimeoutError)


if __name__ == "__main__":
    unittest.main()

[executed on device: iZj6cakb7vguedvzvbqrvbZ (14411471-502a-43de-a2d7-530edf7bf831)]
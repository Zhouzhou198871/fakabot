import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from telegram_storage import TelegramArchiveStore


class FakeBot:
    def __init__(self):
        self.pinned_message = None
        self.documents = {}
        self.messages = {}
        self.next_id = 1
        self.fail_send = False
        self.fail_edit = False

    def get_chat(self, chat_id):
        return SimpleNamespace(type="channel", username=None, pinned_message=self.pinned_message)

    def send_document(self, chat_id, document, caption, disable_notification=True):
        if self.fail_send:
            raise RuntimeError("simulated Telegram outage")
        file_id = "file-" + str(self.next_id)
        message_id = self.next_id
        self.next_id += 1
        self.documents[file_id] = document.read()
        message = SimpleNamespace(message_id=message_id, document=SimpleNamespace(file_id=file_id))
        self.messages[message_id] = message
        return message

    def pin_chat_message(self, chat_id, message_id, disable_notification=True):
        self.pinned_message = self.messages[message_id]

    def edit_message_media(self, media, chat_id, message_id):
        if self.fail_edit:
            raise RuntimeError("simulated Telegram edit outage")
        file_id = "file-edit-" + str(self.next_id)
        self.next_id += 1
        self.documents[file_id] = media.media.read()
        message = SimpleNamespace(message_id=message_id, document=SimpleNamespace(file_id=file_id))
        self.messages[message_id] = message
        if self.pinned_message and self.pinned_message.message_id == message_id:
            self.pinned_message = message
        return message

    def get_file(self, file_id):
        return SimpleNamespace(file_path=file_id)

    def download_file(self, file_path):
        return self.documents[file_path]


class TelegramArchiveStoreTests(unittest.TestCase):
    def test_migrates_legacy_files_and_recovers_from_pinned_document(self):
        bot = FakeBot()
        with tempfile.TemporaryDirectory() as directory:
            paths = {
                "users": os.path.join(directory, "users.json"),
                "orders": os.path.join(directory, "orders.json"),
                "withdrawals": os.path.join(directory, "withdrawals.json"),
            }
            with open(paths["users"], "w", encoding="utf-8") as handle:
                json.dump({"123": {"balance": 7.5}}, handle)
            with open(paths["orders"], "w", encoding="utf-8") as handle:
                json.dump([{"order_id": "A1"}], handle)
            with open(paths["withdrawals"], "w", encoding="utf-8") as handle:
                json.dump([], handle)

            first = TelegramArchiveStore(bot, -1004459109788, paths)
            first.initialize()

            self.assertEqual(first.load("users", {}), {"123": {"balance": 7.5}})
            self.assertEqual(first.load("orders", []), [{"order_id": "A1"}])
            self.assertTrue(all(not os.path.exists(path) for path in paths.values()))

            recovered = TelegramArchiveStore(bot, -1004459109788)
            recovered.initialize()
            self.assertEqual(recovered.load("users", {}), {"123": {"balance": 7.5}})
            self.assertEqual(recovered.load("orders", []), [{"order_id": "A1"}])

    def test_failed_upload_rolls_back_in_memory_change(self):
        bot = FakeBot()
        store = TelegramArchiveStore(bot, -1004459109788)
        store.initialize()
        store.save("users", {"123": {"balance": 2}})
        bot.fail_send = True
        bot.fail_edit = True
        with self.assertRaises(RuntimeError):
            store.save("users", {"123": {"balance": 99}})
        self.assertEqual(store.load("users", {}), {"123": {"balance": 2}})


    def test_meta_dictionary_can_be_saved_and_recovered(self):
        bot = FakeBot()
        store = TelegramArchiveStore(bot, -1004459109788)
        store.initialize()
        store.save("meta", {"last_daily_report": "2026-11-12"})
        recovered = TelegramArchiveStore(bot, -1004459109788)
        recovered.initialize()
        self.assertEqual(
            recovered.load("meta", {}),
            {"last_daily_report": "2026-11-12"},
        )


    def test_updates_single_pinned_archive_instead_of_posting_new_messages(self):
        bot = FakeBot()
        store = TelegramArchiveStore(bot, -1004459109788)
        store.initialize()
        first_message_id = store.archive_message_id
        self.assertEqual(len(bot.messages), 1)

        store.save("users", {"123": {"balance": 10}})
        store.save("meta", {"last_daily_report": "2026-11-12"})

        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(store.archive_message_id, first_message_id)
        self.assertEqual(bot.pinned_message.message_id, first_message_id)

if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Telegram private-channel snapshot storage.

Business data is held in process memory only and durably snapshotted as a JSON
document in a private Telegram channel. The latest document is pinned so the
Bot API can recover it after restart without local database/files.
"""
import copy
import io
import json
import os
import threading
from datetime import datetime, timezone


class TelegramArchiveStore:
    KEYS = ("users", "orders", "withdrawals", "meta")

    def __init__(self, bot, chat_id, legacy_paths=None):
        self.bot = bot
        self.chat_id = int(chat_id)
        self.legacy_paths = dict(legacy_paths or {})
        self.lock = threading.RLock()
        self.persist_lock = threading.Lock()
        self.data = {"schema_version": 1, "users": {}, "orders": [], "withdrawals": [], "meta": {}}
        self.initialized = False
        self.archive_message_id = None
        self.dirty = False
        self.version = 0

    def _get_pinned_snapshot(self):
        chat = self.bot.get_chat(self.chat_id)
        if getattr(chat, "type", None) != "channel":
            raise RuntimeError("归档目标必须是 Telegram 频道，拒绝向其他聊天写入业务数据")
        if getattr(chat, "username", None):
            raise RuntimeError("归档频道检测到公开用户名；为保护用户隐私，拒绝写入公开频道")
        pinned = getattr(chat, "pinned_message", None)
        document = getattr(pinned, "document", None) if pinned else None
        if not document:
            return None
        self.archive_message_id = getattr(pinned, "message_id", None)
        file_info = self.bot.get_file(document.file_id)
        raw = self.bot.download_file(file_info.file_path)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise RuntimeError("频道置顶归档文件格式不受支持；为防止覆盖旧数据，已停止启动")
        for key, default in (("users", {}), ("orders", []), ("withdrawals", []), ("meta", {})):
            value = payload.get(key, default)
            if not isinstance(value, type(default)):
                raise RuntimeError("频道归档数据结构异常：" + key)
        return payload

    def _legacy_snapshot(self):
        imported = {}
        found = False
        for key, path in self.legacy_paths.items():
            if path and os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as handle:
                    value = json.load(handle)
                expected = dict if key in ("users", "meta") else list
                if not isinstance(value, expected):
                    raise RuntimeError("旧数据格式异常，拒绝迁移：" + key)
                imported[key] = value
                found = True
        if found:
            self.data.update(imported)
        return found

    def initialize(self):
        with self.lock:
            existing = self._get_pinned_snapshot()
            if existing is not None:
                self.data = existing
                # The pinned Telegram snapshot is authoritative. Remove any
                # leftover legacy files only after the archive was downloaded
                # and validated successfully.
                for path in self.legacy_paths.values():
                    if path and os.path.isfile(path):
                        os.remove(path)
                self.initialized = True
                return
            # One-time migration: keep legacy files until Telegram confirms the
            # snapshot upload AND pin. Then remove only the successfully migrated files.
            self._legacy_snapshot()
            self._persist("初始化 / 旧数据迁移")
            for path in self.legacy_paths.values():
                if path and os.path.isfile(path):
                    os.remove(path)
            self.initialized = True

    def load(self, key, default):
        with self.lock:
            if not self.initialized:
                raise RuntimeError("Telegram 归档存储尚未初始化")
            value = self.data.get(key, default)
            return copy.deepcopy(value)

    def save(self, key, value):
        if key not in self.KEYS:
            raise ValueError("未知归档数据类型：" + str(key))
        expected = dict if key in ("users", "meta") else list
        if not isinstance(value, expected):
            raise TypeError(key + " 数据类型不正确")
        with self.lock:
            if not self.initialized:
                raise RuntimeError("Telegram 归档存储尚未初始化")
            self.data[key] = copy.deepcopy(value)
            self.version += 1
            self.dirty = True

    def increment_counter(self, name, amount=1):
        """Atomically increment a lightweight operational counter without network I/O."""
        with self.lock:
            if not self.initialized:
                raise RuntimeError("Telegram 归档存储尚未初始化")
            meta = self.data.setdefault("meta", {})
            counters = meta.setdefault("metrics_counters", {})
            counters[str(name)] = int(counters.get(str(name), 0) or 0) + int(amount)
            self.version += 1
            self.dirty = True

    def take_counters(self):
        """Atomically read and reset interval counters."""
        with self.lock:
            meta = self.data.setdefault("meta", {})
            counters = dict(meta.get("metrics_counters", {}))
            meta["metrics_counters"] = {}
            self.version += 1
            self.dirty = True
            return counters

    def flush(self, reason="定时归档"):
        """Persist a consistent snapshot without blocking readers/writers on Telegram network I/O."""
        with self.persist_lock:
            with self.lock:
                if not self.initialized or not self.dirty:
                    return False
                snapshot = copy.deepcopy(self.data)
                version = self.version
            self._persist_snapshot(snapshot, reason)
            with self.lock:
                if self.version == version:
                    self.dirty = False
            return True

    def flush_loop(self, interval=5):
        while True:
            try:
                self.flush()
            except Exception as exc:
                print("Telegram archive background flush failed:", repr(exc), flush=True)
            time.sleep(max(1, int(interval)))

    def send_report_snapshot(self, report_text, report_title="工作汇报"):
        """Publish a human-readable report together with the matching business-data JSON."""
        with self.lock:
            if not self.initialized:
                raise RuntimeError("Telegram 归档存储尚未初始化")
            snapshot = dict(self.data)
            snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
            encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            buffer = io.BytesIO(encoded)
            buffer.name = "qingtian_business_archive.json"
            # Keep the complete report as a channel message; attach the exact data snapshot next to it.
            self.bot.send_message(self.chat_id, report_text, parse_mode="HTML", disable_web_page_preview=True)
            caption = (
                "☀️ 晴天号铺 · " + str(report_title) + " · 配套业务数据文件\n"
                f"用户：{len(snapshot.get('users', {}))} 条；订单：{len(snapshot.get('orders', []))} 条；"
                f"提现：{len(snapshot.get('withdrawals', []))} 条\n"
                "本附件为发送该汇报时的业务数据快照，请勿转发。"
            )
            self.bot.send_document(
                self.chat_id, buffer, caption=caption, disable_notification=True
            )

    def _persist(self, reason):
        with self.lock:
            snapshot = copy.deepcopy(self.data)
        self._persist_snapshot(snapshot, reason)

    def _persist_snapshot(self, snapshot, reason):
        snapshot = copy.deepcopy(snapshot)
        snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        buffer = io.BytesIO(encoded)
        buffer.name = "qingtian_business_archive.json"
        users = len(snapshot.get("users", {}))
        orders = len(snapshot.get("orders", []))
        withdrawals = len(snapshot.get("withdrawals", []))
        caption = (
            "☀️ 晴天号铺 · 数据归档工作汇报\n"
            "状态：归档快照已生成\n"
            "更新模块：" + reason + "\n"
            f"用户档案：{users} 条 · 订单记录：{orders} 条 · 提现记录：{withdrawals} 条\n"
            "说明：完整业务数据见本条 JSON 附件；请勿转发。"
        )
        # Reuse one pinned archive message instead of spamming a new file per update.
        if self.archive_message_id is not None:
            editor = getattr(self.bot, "edit_message_media", None)
            if callable(editor):
                try:
                    from telebot import types
                    media = types.InputMediaDocument(media=buffer, caption=caption)
                    editor(
                        media=media,
                        chat_id=self.chat_id,
                        message_id=self.archive_message_id,
                    )
                    return
                except Exception as exc:
                    print("Telegram archive edit failed; creating replacement:", repr(exc), flush=True)
                    buffer.seek(0)

        message = self.bot.send_document(
            self.chat_id,
            buffer,
            caption=caption,
            disable_notification=True,
        )
        self.bot.pin_chat_message(
            self.chat_id,
            message.message_id,
            disable_notification=True,
        )
        self.archive_message_id = message.message_id

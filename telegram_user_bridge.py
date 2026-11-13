"""Optional, least-privilege Telegram Premium user-account bridge.

The bot remains the primary account. This client is only for MTProto operations
that Bot API cannot perform (for example, resolving a user's custom-emoji pack).
Never commit the .session file or credentials.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterable

_LOCK = threading.RLock()
_CLIENT = None
_START_ERROR = None


def _build_client():
    api_id = os.getenv("TG_USER_API_ID", "").strip()
    api_hash = os.getenv("TG_USER_API_HASH", "").strip()
    session_string = os.getenv("TG_USER_SESSION", "").strip()
    session_file = os.getenv("TG_USER_SESSION_FILE", "/root/fakabot/private/tg_user")
    if not api_id or not api_hash:
        return None, "missing_api_credentials"
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        if session_string:
            session = StringSession(session_string)
        else:
            Path(session_file).parent.mkdir(parents=True, exist_ok=True)
            session = session_file
        client = TelegramClient(session, int(api_id), api_hash)
        client.connect()
        if not client.is_user_authorized():
            client.disconnect()
            return None, "user_authorization_required"
        return client, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def initialize():
    """Initialize once. No interactive login or OTP collection is attempted."""
    global _CLIENT, _START_ERROR
    with _LOCK:
        if _CLIENT is not None and _CLIENT.is_connected():
            return True, "ready"
        _CLIENT, _START_ERROR = _build_client()
        return (_CLIENT is not None), (_START_ERROR or "ready")


def is_ready() -> bool:
    with _LOCK:
        return _CLIENT is not None and _CLIENT.is_connected() and _CLIENT.is_user_authorized()


def get_custom_emoji_documents(document_ids: Iterable[int]):
    """Fetch custom emoji documents available to the authorized user account."""
    ids = [int(value) for value in document_ids if str(value).isdigit()]
    if not ids:
        return []
    with _LOCK:
        if not is_ready():
            raise RuntimeError("Telegram user bridge is not authorized")
        from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
        return _CLIENT(GetCustomEmojiDocumentsRequest(document_id=ids))


def disconnect():
    global _CLIENT
    with _LOCK:
        if _CLIENT is not None:
            _CLIENT.disconnect()
            _CLIENT = None

"""Local-only Russian mailbox stock. This module never contacts any supplier."""
import hashlib
import json
import os
import tempfile
import fcntl
from contextlib import contextmanager
import threading
import time
import uuid
from pathlib import Path

INVENTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "russian_email_inventory.json"
PRICE_CNY = "2.00"
PRICE_USDT = "0.2500"  # Store wallet unit: 1 USDT = CNY 8
_LOCK = threading.RLock()
_CRITICAL_ERROR_HANDLER = None

def set_critical_error_handler(handler):
    global _CRITICAL_ERROR_HANDLER
    _CRITICAL_ERROR_HANDLER = handler if callable(handler) else None



@contextmanager
def _file_lock():
    """Coordinate the bot and the standalone importer across processes."""
    lock_path = str(INVENTORY_PATH) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        os.chmod(lock_path, 0o600)
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    exc = TimeoutError("本地邮箱库存文件锁等待超过 10 秒")
                    if callable(_CRITICAL_ERROR_HANDLER):
                        try:
                            _CRITICAL_ERROR_HANDLER(exc)
                        except Exception:
                            pass
                    raise exc
                time.sleep(0.1)
        yield
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read():
    if not INVENTORY_PATH.exists():
        return {"version": 1, "cards": []}
    with INVENTORY_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
        raise RuntimeError("本地俄罗斯邮箱库存文件格式错误")
    return data


def _write(data):
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(INVENTORY_PATH.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".russian-email-", suffix=".tmp", dir=str(INVENTORY_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, INVENTORY_PATH)
        os.chmod(INVENTORY_PATH, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def stock_count():
    with _LOCK, _file_lock():
        return sum(1 for x in _read()["cards"] if x.get("status") == "available")


def get_email_domains(limit=3):
    """Return the most common email domains in currently available local stock."""
    from collections import Counter
    with _LOCK, _file_lock():
        data = _read()
        counts = Counter()
        for card in data["cards"]:
            if card.get("status") != "available":
                continue
            address = str(card.get("content") or "").strip()
            if "@" not in address:
                continue
            domain = address.rsplit("@", 1)[-1].strip().lower()
            if domain and all(ch.isalnum() or ch in ".-" for ch in domain):
                counts[domain] += 1
        return ["@" + domain for domain, _ in counts.most_common(max(1, int(limit)))]


def get_local_products():
    count = stock_count()
    if count <= 0:
        return []
    return [{
        "id": "russianmail_single",
        "name": "俄罗斯邮箱账号（单个）",
        "price": PRICE_USDT,
        "price_usdt": PRICE_USDT,
        "price_cny": PRICE_CNY,
        "supplier_stock": count,
        "is_local_inventory": True,
        "supplier_verified": True,
        "procurement_ready": True,
        "category_key": "russianmail",
        "category_name": "俄罗斯邮箱",
    }]


def reserve_one(order_id):
    """Atomically reserve one available card; reserved cards cannot be sold twice."""
    with _LOCK, _file_lock():
        data = _read()
        for card in data["cards"]:
            if card.get("status") == "available":
                card["status"] = "reserved"
                card["order_id"] = str(order_id)
                card["reserved_at"] = time.time()
                _write(data)
                return card["content"]
    return None


def finalize_reservation(order_id):
    with _LOCK, _file_lock():
        data = _read()
        for card in data["cards"]:
            if card.get("status") == "reserved" and card.get("order_id") == str(order_id):
                card["status"] = "sold"
                card["sold_at"] = time.time()
                _write(data)
                return True
    return False


def release_reservation(order_id):
    with _LOCK, _file_lock():
        data = _read()
        changed = False
        for card in data["cards"]:
            if card.get("status") == "reserved" and card.get("order_id") == str(order_id):
                card["status"] = "available"
                card.pop("order_id", None)
                card.pop("reserved_at", None)
                changed = True
        if changed:
            _write(data)
        return changed


def import_cards(lines):
    """Import one credential per non-empty line; exact duplicates are skipped."""
    cleaned = [str(line).strip() for line in lines if str(line).strip()]
    with _LOCK, _file_lock():
        data = _read()
        seen = {x.get("sha256") for x in data["cards"]}
        added = 0
        duplicates = 0
        for content in cleaned:
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest in seen:
                duplicates += 1
                continue
            data["cards"].append({
                "id": uuid.uuid4().hex,
                "sha256": digest,
                "content": content,
                "status": "available",
                "imported_at": time.time(),
            })
            seen.add(digest)
            added += 1
        _write(data)
        return {"input": len(cleaned), "added": added, "duplicates": duplicates,
                "available": sum(1 for x in data["cards"] if x.get("status") == "available")}

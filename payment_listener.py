# -*- coding: utf-8 -*-
"""TRON TRC20 USDT payment watcher.

This module ONLY reads public chain data. A private key is neither required nor
accepted. It polls confirmed TRC20 transfers for the configured receiving
address and matches exact integer token units to pending orders.

Run it in the same process as bot.py (PaymentWatcher.start()). The shared
FileLock protects JSON updates between threads/processes using this code.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
from filelock import FileLock

logger = logging.getLogger("fakabot.payment")

TRONGRID_BASE_URL = os.getenv("TRONGRID_BASE_URL", "https://api.trongrid.io").rstrip("/")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
TRON_RECEIVE_ADDRESS = os.getenv("TRON_RECEIVE_ADDRESS", "")
USDT_CONTRACT = os.getenv(
    "TRON_USDT_CONTRACT",
    "TR7NHqjeKQxGTCi8q8ZY4p4L8otSzgjLj6t"
)
USDT_DECIMALS = int(os.getenv("USDT_DECIMALS", "6"))
POLL_INTERVAL_SECONDS = int(os.getenv("PAYMENT_POLL_SECONDS", "8"))
REQUEST_TIMEOUT_SECONDS = 15
PAGE_LIMIT = 200


def iso_to_ms(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def utc_now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class TronGridClient:
    def __init__(self, api_key, base_url=TRONGRID_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "TRON-PRO-API-KEY": api_key,
            "User-Agent": "FakaBot/2.0"
        })

    def incoming_usdt_transfers(self, address, min_timestamp_ms):
        """Fetch confirmed incoming USDT transfers, following pagination."""
        if not address:
            raise RuntimeError("TRON_RECEIVE_ADDRESS 未配置")
        url = f"{self.base_url}/v1/accounts/{address}/transactions/trc20"
        params = {
            "only_confirmed": "true",
            "only_to": "true",
            "limit": PAGE_LIMIT,
            "order_by": "block_timestamp,desc",
            "min_timestamp": max(0, int(min_timestamp_ms)),
            "contract_address": USDT_CONTRACT,
        }
        all_rows = []
        fingerprint = None

        # Bound pagination to prevent an unexpectedly large API scan.
        for _ in range(5):
            query = dict(params)
            if fingerprint:
                query["fingerprint"] = fingerprint
            response = self._get_with_retry(url, params=query)
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("TronGrid 返回的数据格式不正确")
            all_rows.extend(payload["data"])
            meta = payload.get("meta") or {}
            fingerprint = meta.get("fingerprint")
            if not fingerprint or not payload["data"]:
                break
        return all_rows

    def _get_with_retry(self, url, params):
        last_error = None
        for attempt in range(4):
            try:
                response = self.session.get(
                    url, params=params, timeout=REQUEST_TIMEOUT_SECONDS
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"TronGrid HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                return response
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                delay = min(2 ** attempt, 20)
                logger.warning("TronGrid 查询失败，第 %s 次重试：%s", attempt + 1, exc)
                if attempt < 3:
                    time.sleep(delay)
        raise RuntimeError(f"TronGrid 连续查询失败：{last_error}") from last_error


class PaymentWatcher:
    """Poll chain transfers and call on_payment(order_id, transfer)."""

    def __init__(
        self,
        orders_file,
        lock_file,
        on_payment,
        on_late_payment=None,
        on_expired=None,
        poll_interval=POLL_INTERVAL_SECONDS,
    ):
        if not TRONGRID_API_KEY:
            raise RuntimeError("缺少 TRONGRID_API_KEY 环境变量")
        if not TRON_RECEIVE_ADDRESS:
            raise RuntimeError("缺少 TRON_RECEIVE_ADDRESS 环境变量")
        if USDT_DECIMALS != 6:
            raise RuntimeError("TRON USDT 主网通常为 6 位精度，请核实 USDT_DECIMALS")
        self.orders_file = orders_file
        self.lock = FileLock(lock_file, timeout=20)
        self.client = TronGridClient(TRONGRID_API_KEY)
        self.on_payment = on_payment
        self.on_late_payment = on_late_payment
        self.on_expired = on_expired
        self.poll_interval = max(5, int(poll_interval))
        self.stop_event = threading.Event()
        self.thread = None
        self.seen_txids = set()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self.run_forever, name="trongrid-payment-watcher", daemon=True
        )
        self.thread.start()
        logger.info("TRC20 USDT watcher started")

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def run_forever(self):
        while not self.stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                logger.exception("链上支付轮询异常；稍后自动重试")
            self.stop_event.wait(self.poll_interval)

    def _read_orders(self):
        import json
        with open(self.orders_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("orders.json 根节点必须是数组")
        return data

    def _write_orders(self, orders):
        import json
        from bot_storage import atomic_write_text
        atomic_write_text(
            self.orders_file,
            json.dumps(orders, ensure_ascii=False, indent=2) + "\n"
        )

    def _pending_snapshot(self):
        with self.lock:
            orders = self._read_orders()
        pending = [
            o for o in orders
            if o.get("payment_status") == "pending"
            and o.get("status") == "pending_payment"
        ]
        return orders, pending

    def scan_once(self):
        orders, pending = self._pending_snapshot()
        now_ms = utc_now_ms()

        # Expire first; the deadline is enforced against block timestamp too.
        expired_ids = []
        with self.lock:
            latest = self._read_orders()
            changed = False
            for order in latest:
                if order.get("payment_status") != "pending":
                    continue
                if now_ms >= int(order.get("expires_at_ms", 0)):
                    order["payment_status"] = "expired"
                    order["status"] = "expired"
                    order["expired_at"] = datetime.now(timezone.utc).isoformat()
                    expired_ids.append(order.get("order_id"))
                    changed = True
            if changed:
                self._write_orders(latest)

        if self.on_expired:
            for oid in expired_ids:
                try:
                    self.on_expired(oid)
                except Exception:
                    logger.exception("通知订单过期失败：%s", oid)

        # Refresh list after expiry writes.
        with self.lock:
            orders = self._read_orders()

        active = [
            o for o in orders
            if o.get("payment_status") == "pending"
            and o.get("status") == "pending_payment"
        ]
        # Also inspect recently expired orders to flag late deposits for manual review.
        recently_expired = [
            o for o in orders
            if o.get("payment_status") == "expired"
            and now_ms - int(o.get("expires_at_ms", 0)) <= 24 * 60 * 60 * 1000
        ]
        candidates = active + recently_expired
        if not candidates:
            return

        oldest_ms = min(iso_to_ms(o["created_at"]) for o in candidates)
        transfers = self.client.incoming_usdt_transfers(
            TRON_RECEIVE_ADDRESS, max(0, oldest_ms - 120_000)
        )

        # Index exact payment amount (raw integer) to candidate orders.
        amount_index = {}
        for order in candidates:
            amount_index.setdefault(str(order.get("amount_raw")), []).append(order)

        for tx in transfers:
            try:
                txid = str(tx.get("transaction_id") or tx.get("txID") or "")
                if not txid or txid in self.seen_txids:
                    continue

                token = tx.get("token_info") or {}
                token_contract = str(token.get("address") or "").lower()
                if token_contract != USDT_CONTRACT.lower():
                    continue

                recipient = str(tx.get("to") or "").strip()
                if recipient != TRON_RECEIVE_ADDRESS:
                    continue

                raw_value = str(tx.get("value", ""))
                if not raw_value.isdigit():
                    continue
                block_ms = int(tx.get("block_timestamp") or 0)
                if block_ms <= 0:
                    continue

                possible = amount_index.get(raw_value, [])
                for order in possible:
                    created_ms = iso_to_ms(order["created_at"])
                    expires_ms = int(order["expires_at_ms"])
                    if block_ms < created_ms:
                        continue
                    if txid in self.seen_txids:
                        break

                    transfer = {
                        "txid": txid,
                        "from": tx.get("from", ""),
                        "to": recipient,
                        "amount_raw": raw_value,
                        "block_timestamp_ms": block_ms,
                        "confirmed": True,
                        "contract": token.get("address", "")
                    }

                    if block_ms <= expires_ms and order.get("payment_status") == "pending":
                        self.seen_txids.add(txid)
                        self.on_payment(order["order_id"], transfer)
                        break

                    if block_ms > expires_ms and order.get("payment_status") == "expired":
                        self.seen_txids.add(txid)
                        if self.on_late_payment:
                            self.on_late_payment(order["order_id"], transfer)
                        break
            except Exception:
                logger.exception("解析单笔链上转账失败")


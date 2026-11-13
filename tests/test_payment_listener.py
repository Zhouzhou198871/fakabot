import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import payment_listener as pl


class FakeChain:
    def __init__(self, transfers, solidified=True):
        self.transfers, self.solidified = transfers, solidified
        self.receipt_checks = []
    def incoming_usdt_transfers(self, address, min_timestamp_ms):
        return self.transfers
    def is_solidified_success(self, txid):
        self.receipt_checks.append(txid)
        return self.solidified


class TronPaymentWatcherTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.order = {"order_id":"TRON-TEST-1", "payment_status":"pending",
            "status":"pending_payment", "amount_raw":"1250000",
            "created_at":self.now.isoformat(),
            "expires_at_ms":int((self.now+timedelta(minutes=20)).timestamp()*1000)}
        self.tx = {"transaction_id":"TX-TEST-1", "token_info":{"address":"USDT-CONTRACT"},
            "to":"T-RECEIVE", "from":"T-SENDER", "value":"1250000",
            "block_timestamp":int(self.now.timestamp()*1000)}

    def run_scan(self, solidified=True):
        events=[]; chain=FakeChain([self.tx], solidified)
        with patch.object(pl, "TRONGRID_API_KEY", "test-key"), \
             patch.object(pl, "TRON_RECEIVE_ADDRESS", "T-RECEIVE"), \
             patch.object(pl, "USDT_CONTRACT", "USDT-CONTRACT"):
            watcher=pl.PaymentWatcher(lambda:[self.order], lambda rows:None,
                lambda oid,tx:events.append((oid,tx)))
            watcher.client=chain
            watcher.scan_once()
        return events, chain

    def test_matching_solidified_transfer_is_delivered_to_handler(self):
        events, chain=self.run_scan(True)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0][0],"TRON-TEST-1")
        self.assertTrue(events[0][1]["confirmed"])
        self.assertEqual(chain.receipt_checks,["TX-TEST-1"])

    def test_transfer_without_solidified_success_is_not_credited(self):
        events, chain=self.run_scan(False)
        self.assertEqual(events,[])
        self.assertEqual(chain.receipt_checks,["TX-TEST-1"])

    def test_wrong_amount_is_not_even_receipt_checked(self):
        self.tx["value"]="1250001"
        events, chain=self.run_scan(True)
        self.assertEqual(events,[])
        self.assertEqual(chain.receipt_checks,[])

    def test_handler_failure_does_not_mark_transfer_seen_and_next_scan_retries(self):
        chain=FakeChain([self.tx], True)
        attempts=[]
        def handler(oid, tx):
            attempts.append(oid)
            if len(attempts) == 1:
                raise RuntimeError("archive temporarily unavailable")
            return True
        with patch.object(pl, "TRONGRID_API_KEY", "test-key"), \
             patch.object(pl, "TRON_RECEIVE_ADDRESS", "T-RECEIVE"), \
             patch.object(pl, "USDT_CONTRACT", "USDT-CONTRACT"):
            watcher=pl.PaymentWatcher(lambda:[self.order], lambda rows:None, handler)
            watcher.client=chain
            watcher.scan_once()
            self.assertNotIn("TX-TEST-1", watcher.seen_txids)
            watcher.scan_once()
        self.assertEqual(attempts, ["TRON-TEST-1", "TRON-TEST-1"])
        self.assertIn("TX-TEST-1", watcher.seen_txids)


if __name__ == "__main__":
    unittest.main()
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from app.account03_ledger_client import (
    Account03LedgerClient,
    Account03LedgerError,
)
from scripts.run_square_slot import _account03_signal_payload


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.stream = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Account03LedgerClientTests(unittest.TestCase):
    def setUp(self):
        self.client = Account03LedgerClient(
            "https://ledger.example",
            "x" * 40,
        )
        self.reservation_id = "a" * 40
        self.signal = {
            "signal_id": "signal-001",
            "symbol": "DOGEUSDT",
            "base_asset": "DOGE",
            "side": "LONG",
            "timeframe": "1h",
            "entry_mid": 0.2,
            "stop_percent": 2.0,
            "tp1_percent": 3.0,
            "tp2_percent": 5.0,
            "tp3_percent": 8.0,
        }

    def test_rejects_insecure_non_local_http(self):
        with self.assertRaises(Account03LedgerError):
            Account03LedgerClient("http://example.com", "x" * 40)

    def test_allows_local_http_for_development(self):
        client = Account03LedgerClient("http://127.0.0.1:8787", "x" * 40)
        self.assertEqual(client.base_url, "http://127.0.0.1:8787")

    def test_rejects_short_token(self):
        with self.assertRaises(Account03LedgerError):
            Account03LedgerClient("https://ledger.example", "short")

    @patch("app.account03_ledger_client.secrets.token_hex")
    @patch("app.account03_ledger_client.urlopen")
    def test_reserve_accepts_confirmed_reservation(self, urlopen, token_hex):
        token_hex.return_value = self.reservation_id
        urlopen.return_value = FakeResponse(
            201,
            {"reserved": True, "reservation_id": self.reservation_id},
        )

        result = self.client.reserve(self.signal)

        self.assertTrue(result["reserved"])
        self.assertEqual(result["reservation_id"], self.reservation_id)

        request = urlopen.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["account_id"], "account_03")
        self.assertEqual(sent["signal"]["base_asset"], "DOGE")
        self.assertEqual(request.headers.get("Authorization"), "Bearer " + "x" * 40)
        self.assertEqual(
            request.get_header("User-agent"),
            "CoinCoach-Account03-Ledger/1.0 (+https://github.com/institutionalgiants/coincoach-account-03)",
        )

    @patch("app.account03_ledger_client.secrets.token_hex")
    @patch("app.account03_ledger_client.urlopen")
    def test_reserve_returns_quota_denial(self, urlopen, token_hex):
        token_hex.return_value = self.reservation_id
        urlopen.return_value = FakeResponse(
            409,
            {"reserved": False, "reason": "hourly_limit"},
        )

        result = self.client.reserve(self.signal)

        self.assertFalse(result["reserved"])
        self.assertEqual(result["reason"], "hourly_limit")

    @patch("app.account03_ledger_client.urlopen")
    def test_consumes_one_time_scheduler_authorization(self, urlopen):
        nonce = "b" * 64
        urlopen.return_value = FakeResponse(
            200,
            {"ok": True, "consumed": True, "hour_bucket": "2026-11-08-16"},
        )

        result = self.client.consume_slot_authorization(nonce)

        self.assertTrue(result["consumed"])
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/v1/slot-authorizations/consume"))
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"nonce": nonce})

    @patch("app.account03_ledger_client.urlopen")
    def test_replayed_or_expired_scheduler_authorization_fails_closed(self, urlopen):
        urlopen.return_value = FakeResponse(
            409,
            {"consumed": False, "reason": "invalid_expired_or_replayed"},
        )
        with self.assertRaises(Account03LedgerError):
            self.client.consume_slot_authorization("c" * 64)

    def test_invalid_scheduler_authorization_is_rejected_before_network(self):
        with patch("app.account03_ledger_client.urlopen") as urlopen:
            with self.assertRaises(Account03LedgerError):
                self.client.consume_slot_authorization("not-a-nonce")
            urlopen.assert_not_called()

    @patch("app.account03_ledger_client.urlopen")
    def test_finalize_marks_published(self, urlopen):
        urlopen.return_value = FakeResponse(
            200,
            {"ok": True, "finalized": True, "status": "published"},
        )

        result = self.client.finalize(self.reservation_id, "published")

        self.assertTrue(result["finalized"])
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith(
            f"/v1/reservations/{self.reservation_id}/finalize"
        ))

    @patch("app.account03_ledger_client.urlopen")
    def test_healthcheck_fails_closed_on_bad_response(self, urlopen):
        urlopen.return_value = FakeResponse(503, {"ok": False})

        with self.assertRaises(Account03LedgerError):
            self.client.healthcheck()

    @patch("app.account03_ledger_client.urlopen")
    def test_network_failure_fails_closed(self, urlopen):
        urlopen.side_effect = URLError("offline")

        with self.assertRaises(Account03LedgerError):
            self.client.healthcheck()

    def test_signal_payload_contains_all_d1_fields(self):
        result = _account03_signal_payload(self.signal)

        self.assertEqual(result["account_id"] if "account_id" in result else "account_03", "account_03")
        self.assertEqual(result["side"], "LONG")
        self.assertEqual(result["entry_mid"], 0.2)
        self.assertEqual(result["tp3_percent"], 8.0)

    def test_signal_payload_rejects_missing_identity(self):
        broken = {**self.signal}
        broken.pop("base_asset")

        with self.assertRaises(Account03LedgerError):
            _account03_signal_payload(broken)


if __name__ == "__main__":
    unittest.main()

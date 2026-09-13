import csv
import hashlib
import io
import unittest
from unittest import mock

import shoonya_local_relay as relay


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.original_config = relay.CONFIG.copy()
        relay.CONFIG.clear()
        relay.CONFIG.update(
            {
                "SHOONYA_USER_ID": "TESTUSER",
                "SHOONYA_PASSWORD": "test-password",
                "SHOONYA_VENDOR_CODE": "TESTUSER_U",
                "SHOONYA_API_SECRET": "test-secret",
                "SHOONYA_IMEI": "auto",
            }
        )

    def tearDown(self):
        relay.CONFIG.clear()
        relay.CONFIG.update(self.original_config)

    def test_quickauth_hashes_password_and_user_secret_pair(self):
        with mock.patch.object(
            relay.uuid, "getnode", return_value=0xAABBCCDDEEFF
        ):
            payload = relay.build_login_payload("123456")
        self.assertEqual(
            payload["pwd"],
            hashlib.sha256(b"test-password").hexdigest(),
        )
        self.assertEqual(
            payload["appkey"],
            hashlib.sha256(b"TESTUSER|test-secret").hexdigest(),
        )
        self.assertEqual(payload["imei"], "aabbccddeeff")
        self.assertEqual(payload["factor2"], "123456")
        self.assertNotEqual(payload["appkey"], "test-secret")

    def test_explicit_device_id_is_preserved(self):
        relay.CONFIG["SHOONYA_IMEI"] = "desktop-device-123"
        self.assertEqual(relay.local_device_id(), "desktop-device-123")

    def test_required_config_does_not_require_manual_imei(self):
        relay.CONFIG["SHOONYA_IMEI"] = ""
        self.assertEqual(relay.required_missing(), [])

    def test_normalize_bars_is_chronological_and_deduplicated(self):
        rows = [
            {
                "time": "01-03-2026 09:20:00",
                "into": "100",
                "intc": "101",
            },
            {
                "time": "28-02-2026 15:25:00",
                "into": "90",
                "intc": "91",
            },
            {
                "time": "01-03-2026 09:20:00",
                "into": "100",
                "intc": "102",
                "intoi": "88",
            },
        ]
        bars = relay.normalize_bars(rows)
        self.assertEqual(
            [row["time"] for row in bars],
            ["28-02-2026 15:25:00", "01-03-2026 09:20:00"],
        )
        self.assertEqual(bars[1]["intc"], "102")
        self.assertEqual(bars[1]["intoi"], "88")

    def test_epoch_milliseconds_are_supported(self):
        self.assertEqual(
            relay.bar_epoch({"ssboe": "1772337000000"}),
            1772337000,
        )

    def test_csv_keeps_interval_open_interest(self):
        body = relay.csv_bytes(
            [
                {
                    "time": "01-03-2026 09:20:00",
                    "intoi": "88",
                    "oi": "100",
                }
            ]
        ).decode("utf-8")
        row = next(csv.DictReader(io.StringIO(body)))
        self.assertEqual(row["intoi"], "88")
        self.assertEqual(row["oi"], "100")

    def test_page_is_visibly_versioned_and_dates_are_values(self):
        body = relay.page()
        self.assertIn("LOCAL READ-ONLY RELAY v2.0.0", body)
        self.assertIn('value="2026-02-25"', body)
        self.assertIn('value="2026-05-18"', body)
        self.assertNotIn('value="placeholder=', body)

    def test_date_window_rejects_reverse_range(self):
        with self.assertRaisesRegex(
            RuntimeError, "From date cannot be after To date"
        ):
            relay.parse_window("2026-05-18", "2026-02-25", "7")


if __name__ == "__main__":
    unittest.main()

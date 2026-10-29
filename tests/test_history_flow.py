from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import usd_cny_daily as daily
from daily_trend import load_history


STAMP = datetime(2026, 10, 29, 9, 0, tzinfo=daily.BEIJING)
RATES = {"现汇买入价": Decimal("700"), "现钞买入价": Decimal("699"),
         "现汇卖出价": Decimal("702"), "现钞卖出价": Decimal("703")}


class RateHistoryTests(unittest.TestCase):
    def test_purchase_uses_selling_price_and_correct_units(self):
        title, body = daily.build_message(RATES, STAMP, STAMP)
        self.assertIn("现汇卖出价 7.0200", title)
        self.assertIn("1美元 = 7.0200元人民币", body)
        self.assertIn("1000美元约需7020.00元", body)
        self.assertNotIn("现钞买入价 |", body)
        self.assertNotIn("现钞卖出价 |", body)

    def run_daily(self, path, preview=False, stamp=STAMP):
        clock = patch.object(daily, "datetime", wraps=datetime)
        with clock as now, patch.dict(os.environ, {"XZKEY": "XZunitTest"}), \
             patch.object(daily, "fetch_usd", return_value=(RATES, stamp)), \
             patch.object(daily, "send_once") as send, redirect_stdout(io.StringIO()):
            now.now.return_value = STAMP
            args = ["--history-file", str(path)] + (["--dry-run"] if preview else [])
            self.assertEqual(daily.main(args), 0)
        return send

    def test_one_send_and_sell_price_history_persists_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            for _ in range(2):
                send = self.run_daily(path)
                send.assert_called_once()
                self.assertIn("近7天趋势", send.call_args.args[2])
            data = load_history(path, "boc-usd-cny-fx-sell-per-usd")
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["value"], "7.02")

    def test_preview_does_not_write_or_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            self.run_daily(path, preview=True).assert_not_called()
            self.assertFalse(path.exists())

    def test_stale_quote_is_stored_under_source_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            self.run_daily(path, stamp=STAMP.replace(day=28))
            data = load_history(path, "boc-usd-cny-fx-sell-per-usd")
            self.assertEqual(data[0]["date"], "2026-10-28")

    def test_future_quote_not_added_to_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            self.run_daily(path, stamp=STAMP.replace(day=30))
            self.assertEqual(load_history(path, "boc-usd-cny-fx-sell-per-usd"), [])


if __name__ == "__main__":
    unittest.main()

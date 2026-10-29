from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import contextlib
import io
import json
import tempfile
import unittest

from daily_trend import build_summary, load_history, prepare_summary, save_history, update_history


TODAY = date(2026, 10, 29)
ZONE = timezone(timedelta(hours=8))


def point(day, value="7.02", hour=9):
    return {"date": day.isoformat(), "value": value,
            "timestamp": datetime.combine(day, datetime.min.time(), ZONE).replace(hour=hour).isoformat()}


class TrendTests(unittest.TestCase):
    def test_seven_calendar_days_with_gaps_and_range(self):
        data = [point(TODAY - timedelta(days=6), "7.00"), point(TODAY, "6.90")]
        text = build_summary(data, TODAY, digits=4, unit="元/美元")
        self.assertIn("10/23–10/29", text)
        self.assertIn("█ · · · · · ▁", text)
        self.assertIn("有效记录2/7天", text)
        self.assertIn("最低6.9000，最高7.0000", text)
        self.assertIn("-0.1000元/美元（-1.43%）", text)

    def test_window_excludes_old_and_future_records(self):
        data = [point(TODAY - timedelta(days=7), "100"), point(TODAY, "7"),
                point(TODAY + timedelta(days=1), "200")]
        text = build_summary(data, TODAY, digits=4, unit="元/美元")
        self.assertIn("有效记录1/7天", text)
        self.assertNotIn("100.0000", text)
        self.assertNotIn("200.0000", text)
        self.assertIn("暂不计算", text)

    def test_flat_history_has_no_division_by_zero(self):
        text = build_summary([point(TODAY - timedelta(days=i)) for i in range(7)],
                             TODAY, digits=4, unit="元/美元")
        self.assertIn("▄ ▄ ▄ ▄ ▄ ▄ ▄", text)
        self.assertIn("+0.00%", text)

    def test_midnight_crosses_month_and_year(self):
        text = build_summary([], date(2027, 1, 2), digits=2, unit="元/克")
        self.assertIn("12/27–01/02", text)
        self.assertIn("暂没有", text)

    def test_update_replaces_day_only_with_later_quote(self):
        data = [point(TODAY, "7.02", hour=10)]
        earlier = update_history(data, TODAY, point(TODAY, "7.01", hour=9))
        later = update_history(earlier, TODAY, point(TODAY, "7.03", hour=11))
        self.assertEqual(earlier[0]["value"], "7.02")
        self.assertEqual(later[0]["value"], "7.03")
        self.assertEqual(len(later), 1)
        self.assertEqual(data[0]["value"], "7.02")

    def test_update_retains_thirty_days_and_rejects_future(self):
        data = [point(TODAY - timedelta(days=i)) for i in range(40)]
        self.assertEqual(len(update_history(data, TODAY)), 30)
        self.assertEqual(update_history([], TODAY, point(TODAY + timedelta(days=1))), [])

    def test_persists_across_runs_and_series_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            self.assertEqual(load_history(path, "sell"), [])
            save_history(path, "sell", [point(TODAY)])
            self.assertEqual(load_history(path, "sell"), [point(TODAY)])
            with self.assertRaises(ValueError):
                load_history(path, "buy")

    def test_zero_invalid_dates_and_nonfinite_values_never_enter_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            for value in ["0", "-1", "NaN", "Infinity"]:
                save_history(path, "test", [point(TODAY, value)])
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_history(path, "test")

    def test_duplicate_days_and_naive_timestamps_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            save_history(path, "test", [point(TODAY), point(TODAY)])
            with self.assertRaises(ValueError):
                load_history(path, "test")
            bad = point(TODAY)
            bad["timestamp"] = "2026-10-29T09:00:00"
            save_history(path, "test", [bad])
            with self.assertRaises(ValueError):
                load_history(path, "test")

    def test_corrupt_file_not_overwritten_or_echoed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text("secret invalid json", encoding="utf-8")
            log = io.StringIO()
            with contextlib.redirect_stderr(log):
                text = prepare_summary(path, "test", TODAY, digits=2, unit="元/克",
                                       point=point(TODAY), persist=True)
            self.assertIn("暂不可用", text)
            self.assertEqual(path.read_text(), "secret invalid json")
            self.assertNotIn("secret", log.getvalue())

    def test_preview_does_not_create_or_change_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state/history.json"
            prepare_summary(path, "test", TODAY, digits=2, unit="元/克", point=point(TODAY))
            self.assertFalse(path.exists())
            save_history(path, "test", [point(TODAY, "800")])
            before = path.read_bytes()
            prepare_summary(path, "test", TODAY, digits=2, unit="元/克", point=point(TODAY, "810", 10))
            self.assertEqual(path.read_bytes(), before)

    def test_no_quote_does_not_create_zero_or_forward_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            save_history(path, "test", [point(TODAY - timedelta(days=1), "800")])
            text = prepare_summary(path, "test", TODAY, digits=2, unit="元/克", persist=True)
            data = load_history(path, "test")
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["date"], "2026-10-28")
            self.assertIn("· · · · · ▄ ·", text)


if __name__ == "__main__":
    unittest.main()

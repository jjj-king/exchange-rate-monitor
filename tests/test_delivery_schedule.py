import base64
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from contextlib import redirect_stdout, redirect_stderr

import delivery_schedule as guard


ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 30, 10, 40, tzinfo=ZONE)


def ledger_blob(attempts):
    return {"sha": "a" * 40, "encoding": "base64", "content": base64.b64encode(
        json.dumps({"version": 1, "attempts": attempts}).encode()).decode()}


class GuardTests(unittest.TestCase):
    def call_guard(self, now=NOW, event="schedule", force=False, ledger=None, runner=None):
        ledger = ledger or MagicMock()
        ledger.claim.return_value = True
        runner = runner or MagicMock(return_value=MagicMock(returncode=0))
        factory = MagicMock(return_value=ledger)
        with redirect_stdout(io.StringIO()):
            code = guard.guarded_run(["python", "report.py"], now, "10:30", "13:30",
                                     event, force, factory, runner)
        return code, ledger, runner, factory

    def test_delayed_midnight_event_never_reads_state_or_runs_report(self):
        code, ledger, runner, factory = self.call_guard(now=NOW.replace(hour=1))
        self.assertEqual(code, 0)
        factory.assert_not_called()
        runner.assert_not_called()

    def test_window_boundaries(self):
        for hour, minute, expected in [(10, 29, False), (10, 30, True), (13, 29, True), (13, 30, False)]:
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(guard.in_window(NOW.replace(hour=hour, minute=minute), "10:30", "13:30"), expected)

    def test_window_uses_beijing_time_not_runner_timezone(self):
        utc = NOW.astimezone(timezone.utc)
        self.assertTrue(guard.in_window(utc, "10:30", "13:30"))

    def test_invalid_or_overnight_windows_rejected(self):
        for start, end in [("25:00", "26:00"), ("9:00", "12:00"), ("22:00", "08:00"), ("10:30", "10:30")]:
            with self.subTest(start=start), self.assertRaises(ValueError):
                guard.in_window(NOW, start, end)

    def test_day_is_claimed_before_report_runs(self):
        order = []
        ledger = MagicMock()
        ledger.claim.side_effect = lambda now: order.append("claim") or True
        runner = MagicMock(side_effect=lambda *a, **k: order.append("send") or MagicMock(returncode=0))
        self.call_guard(ledger=ledger, runner=runner)
        self.assertEqual(order, ["claim", "send"])

    def test_already_claimed_day_never_runs_report(self):
        ledger = MagicMock()
        ledger.claim.side_effect = lambda now: False
        code, _, runner, _ = self.call_guard(ledger=ledger)
        self.assertEqual(code, 0)
        runner.assert_not_called()

    def test_state_failure_does_not_send(self):
        ledger = MagicMock()
        ledger.claim.side_effect = guard.StateError("unavailable")
        runner = MagicMock()
        with self.assertRaises(guard.StateError):
            self.call_guard(ledger=ledger, runner=runner)
        runner.assert_not_called()

    def test_scheduled_force_does_not_bypass_day_marker_or_window(self):
        ledger = MagicMock()
        ledger.claim.side_effect = lambda now: False
        _, _, runner, _ = self.call_guard(force=True, ledger=ledger)
        runner.assert_not_called()
        _, _, runner, factory = self.call_guard(now=NOW.replace(hour=1), force=True)
        runner.assert_not_called()
        factory.assert_not_called()

    def test_explicit_manual_force_allows_one_extra_without_touching_marker(self):
        _, ledger, runner, factory = self.call_guard(now=NOW.replace(hour=1), event="workflow_dispatch", force=True)
        factory.assert_not_called()
        runner.assert_called_once()

    def test_manual_without_force_still_claims_day(self):
        _, ledger, runner, _ = self.call_guard(now=NOW.replace(hour=16), event="workflow_dispatch")
        ledger.claim.assert_called_once()
        runner.assert_called_once()

    def test_failed_report_is_not_automatically_retried(self):
        runner = MagicMock(return_value=MagicMock(returncode=1))
        code, ledger, _, _ = self.call_guard(runner=runner)
        self.assertEqual(code, 1)
        runner.assert_called_once()
        ledger.claim.assert_called_once()

    def test_preflight_needs_no_credentials_and_claims_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "outputs"
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_OUTPUT": str(output)}, clear=True), \
                 patch.object(guard, "datetime", wraps=datetime) as clock, \
                 patch.object(guard, "GitHubLedger") as ledger, redirect_stdout(io.StringIO()):
                clock.now.return_value = NOW.replace(hour=1)
                self.assertEqual(guard.main(["--start", "19:00", "--end", "21:00", "--check-window"]), 0)
                ledger.assert_not_called()
            self.assertEqual(output.read_text(), "allowed=false\n")


class LedgerTests(unittest.TestCase):
    def ledger(self, responses):
        ledger = guard.GitHubLedger("owner/repo", "SECRET-test-token", "a" * 40)
        ledger.api = MagicMock(side_effect=responses)
        return ledger

    def test_first_run_creates_branch_and_persists_attempt_before_claiming(self):
        ledger = self.ledger([(404, None), (201, {}), (201, {})])
        self.assertTrue(ledger.claim(NOW))
        method, path, payload = ledger.api.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual(payload["branch"], guard.STATE_BRANCH)
        data = json.loads(base64.b64decode(payload["content"]))
        self.assertIn("2026-10-30", data["attempts"])
        self.assertNotIn("SECRET", json.dumps(payload))

    def test_same_beijing_day_blocks_second_attempt(self):
        ledger = self.ledger([(200, ledger_blob({"2026-10-30": NOW.isoformat()}))])
        self.assertFalse(ledger.claim(NOW.replace(hour=11)))
        ledger.api.assert_called_once()

    def test_next_beijing_day_can_claim(self):
        ledger = self.ledger([(200, ledger_blob({"2026-10-30": NOW.isoformat()})), (200, {})])
        self.assertTrue(ledger.claim(NOW + timedelta(days=1)))

    def test_write_race_rechecks_day_and_does_not_duplicate(self):
        ledger = self.ledger([(200, ledger_blob({})), (409, None),
                              (200, ledger_blob({"2026-10-30": NOW.isoformat()}))])
        self.assertFalse(ledger.claim(NOW))
        self.assertEqual(ledger.api.call_count, 3)

    def test_write_network_ambiguity_never_retried(self):
        ledger = self.ledger([(200, ledger_blob({})), guard.StateError("network")])
        with self.assertRaises(guard.StateError):
            ledger.claim(NOW)
        self.assertEqual(ledger.api.call_count, 2)

    def test_malformed_state_not_overwritten(self):
        for blob in [ledger_blob({"bad-date": NOW.isoformat()}), {"sha": "a", "encoding": "base64", "content": "invalid"}]:
            ledger = self.ledger([(200, blob)])
            with self.assertRaises(guard.StateError):
                ledger.claim(NOW)
            ledger.api.assert_called_once()

    def test_ledger_initialization_failure_blocks_delivery(self):
        ledger = self.ledger([(404, None), (409, None)])
        with self.assertRaises(guard.StateError):
            ledger.claim(NOW)

    def test_redirects_cannot_forward_github_credentials(self):
        self.assertIsNone(guard.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test"))

    def test_network_errors_never_expose_token(self):
        ledger = guard.GitHubLedger("owner/repo", "SECRET-test-token", "a" * 40)
        with patch.object(guard, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("SECRET-test-token")
            with self.assertRaises(guard.StateError) as caught:
                ledger.api("GET", "/contents/delivery.json")
        self.assertNotIn("SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

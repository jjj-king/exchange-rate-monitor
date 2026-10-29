import tempfile
from pathlib import Path
from decimal import Decimal
from datetime import datetime
import contextlib, io, os, sys, json, unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usd_cny_daily as daily

TABLE = '''<table><tr><th>货币名称</th><th>现汇买入价</th><th>现钞买入价</th>
<th>现汇卖出价</th><th>现钞卖出价</th><th>中行折算价</th><th>发布日期</th><th>发布时间</th></tr>
<tr><td>美元</td><td>700.00</td><td>699.00</td><td>702.00</td><td>703.00</td><td>701</td><td>2026.10.21</td><td>09:00:00</td></tr></table>'''

class Tests(unittest.TestCase):
    def setUp(self):
        self.history_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.history_dir.cleanup)
        self.args = ["--history-file", str(Path(self.history_dir.name) / "history.json")]

    def test_parse_and_units(self):
        rates, stamp = daily.parse_usd(TABLE)
        self.assertEqual(rates['现汇买入价'], Decimal('700.00'))
        self.assertEqual(stamp.hour, 9)
        _, text = daily.build_message(rates, stamp, stamp)
        self.assertIn('| 现汇买入价 | 7.0000 元 |', text)
        self.assertIn('| 现汇卖出价 | 7.0200 元 |', text)

    def test_fail_closed(self):
        for html in (TABLE.replace('<td>700.00</td>', '<td></td>'),
                     TABLE.replace('<td>700.00</td>', '<td>NaN</td>'),
                     TABLE.replace('<td>美元</td>', '<td>欧元</td>'),
                     TABLE.replace('2026.10.21', 'bad date')):
            with self.assertRaises(ValueError): daily.parse_usd(html)

    def test_stale_note(self):
        rates, stamp = daily.parse_usd(TABLE)
        _, text = daily.build_message(rates, stamp, datetime(2026,10,23,9,tzinfo=daily.BEIJING))
        self.assertIn('超过24小时', text)

    def test_one_send_and_no_key_log(self):
        rates, stamp = daily.parse_usd(TABLE)
        stream = io.StringIO()
        with patch.dict(os.environ, {'XZKEY':'XZTESTEXAMPLE1234'}), patch.object(daily, 'fetch_usd', return_value=(rates, stamp)), patch.object(daily,'send_once') as send, contextlib.redirect_stdout(stream):
            self.assertEqual(daily.main(self.args),0)
            self.assertEqual(send.call_count,1)
        self.assertNotIn('XZTESTEXAMPLE1234',stream.getvalue())

    def test_fetch_failure_one_notice(self):
        with patch.dict(os.environ, {'XZKEY':'XZTESTEXAMPLE1234'}), patch.object(daily, 'fetch_usd', side_effect=RuntimeError), patch.object(daily,'send_once') as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daily.main(self.args),1)
            self.assertEqual(send.call_count,1)
            self.assertIn('获取失败',send.call_args.args[1])

    def test_dryrun_never_sends(self):
        with patch.dict(os.environ, {'XZKEY':''}), patch.object(daily,'fetch_usd',return_value=daily.parse_usd(TABLE)), patch.object(daily,'send_once') as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daily.main(self.args + ['--dry-run']),0)
            send.assert_not_called()

    def test_missing_key(self):
        with patch.dict(os.environ, {'XZKEY':''}), patch.object(daily,'fetch_usd') as fetch, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(daily.main(self.args),1)
            fetch.assert_not_called()



class NotificationTests(unittest.TestCase):
    def response(self, payload):
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__.return_value = response
        return response

    def test_xizhi_endpoint_and_json_body(self):
        for key in ["XZunitTest", "unit-test", "unit_test"]:
            with patch.object(daily, "build_opener") as opener:
                opener.return_value.open.return_value = self.response({"code": 200})
                daily.send_once(key, "测试标题", "正文&换行\n下一行")
                args, kwargs = opener.return_value.open.call_args
                self.assertEqual(args[0].full_url, f"https://xizhi.qqoq.net/{key}.send")
                self.assertEqual(args[0].method, "POST")
                self.assertEqual(kwargs["timeout"], 20)
                self.assertEqual(json.loads(args[0].data.decode()), {"title": "测试标题", "content": "正文&换行\n下一行"})
                self.assertIn("application/json", args[0].get_header("Content-type"))
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_server_rejection_or_bad_response_not_success(self):
        for result in [{"code": 1}, {"code": 0}, {"code": False}, {"code": "200"}, {}, []]:
            with patch.object(daily, "build_opener") as opener:
                opener.return_value.open.return_value = self.response(result)
                with self.assertRaises(RuntimeError):
                    daily.send_once("XZunitTest", "title", "body")
                opener.return_value.open.assert_called_once()

    def test_network_error_never_retried_or_leaks_secret(self):
        key = "XZtestNeverExpose"
        with patch.object(daily, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("secret URL: " + key)
            with self.assertRaises(RuntimeError) as caught:
                daily.send_once(key, "title", "body")
            self.assertNotIn(key, str(caught.exception))
            opener.return_value.open.assert_called_once()

    def test_http_error_does_not_echo_key(self):
        with patch.object(daily, "build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("XZtestNeverExpose", 403, "secret", {}, None)
            with self.assertRaises(RuntimeError) as caught:
                daily.send_once("XZtestNeverExpose", "title", "body")
            self.assertIn("403", str(caught.exception))
            self.assertNotIn("XZtestNeverExpose", str(caught.exception))

    def test_reject_invalid_key_before_network(self):
        for key in ["", " ", "https://secret.send", "key/path", "key\n", "key?x=1"]:
            with self.subTest(key=key), patch.object(daily, "build_opener") as opener:
                with self.assertRaises(ValueError):
                    daily.send_once(key, "title", "body")
                opener.assert_not_called()

    def test_redirects_are_rejected(self):
        self.assertIsNone(daily._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))



if __name__ == '__main__':
    unittest.main()

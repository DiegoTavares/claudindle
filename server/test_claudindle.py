import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest import mock
from zoneinfo import ZoneInfo

import claudindle
import stats


def http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("url", code, "err", {}, io.BytesIO(body))


class TokenRefreshTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            json.dump({"access_token": "a", "refresh_token": "r", "expires_at": 0}, f)
        patcher = mock.patch.object(claudindle, "CREDS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.remove, self.path)

    def test_invalid_grant_raises_login_expired(self):
        err = http_error(400, b'{"error": "invalid_grant"}')
        with mock.patch.object(claudindle, "_post_json", side_effect=err):
            with self.assertRaises(claudindle.LoginExpired) as cm:
                claudindle.token_from_creds()
        self.assertIn("claudindle.py login", str(cm.exception))

    def test_other_http_errors_propagate(self):
        err = http_error(500, b"oops")
        with mock.patch.object(claudindle, "_post_json", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                claudindle.token_from_creds()


class RenderPagesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch.object(claudindle, "STATE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failure_is_logged_and_rendered(self):
        with mock.patch.object(claudindle, "fetch_usage", side_effect=claudindle.LoginExpired()), \
                mock.patch.object(claudindle, "render_limits", wraps=claudindle.render_limits) as render, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            pages = claudindle.render_pages()
        self.assertEqual(list(pages), ["limits"])
        self.assertTrue(pages["limits"].startswith(b"\x89PNG"))
        self.assertIn("Login expired", stderr.getvalue())
        render.assert_called_once_with(None, None, error="Login expired: run `claudindle.py login`")

    def test_activity_page_appears_once_a_machine_reports(self):
        now = int(time.time())
        stats.save_report(self.dir, {"host": "robin", "prompts": [[now, "thock", "s1"]],
                                     "tokens": [[now, "thock", "claude-opus-5-5", 1000]],
                                     "tools": [[now, "mcp__thock__read", 3]]})
        usage = {"limits": [{"kind": "session", "percent": 40}]}
        with mock.patch.object(claudindle, "fetch_usage", return_value=usage):
            pages = claudindle.render_pages()
        self.assertEqual(sorted(pages), ["activity", "limits"])


class NextPageTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(claudindle.State, pages={"activity": b"A", "limits": b"L"}, turn=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_alternates_and_honors_explicit_page(self):
        self.assertEqual([claudindle.next_page("") for _ in range(3)], [b"A", b"L", b"A"])
        self.assertEqual(claudindle.next_page("page=limits"), b"L")
        self.assertEqual(claudindle.next_page(""), b"L")  # an explicit page doesn't advance the turn
        self.assertIsNone(claudindle.next_page("page=nope"))


class StatsTest(unittest.TestCase):
    TZ = ZoneInfo("America/Edmonton")

    def test_model_labels(self):
        self.assertEqual(stats.model_label("claude-opus-5-5"), "Opus 5.5")
        self.assertEqual(stats.model_label("claude-fable-5-2"), "Fable 5.2")
        self.assertEqual(stats.model_label("claude-3-5-sonnet-20241022"), "Sonnet 3.5")

    def test_save_report_rejects_unsafe_hosts(self):
        with self.assertRaises(ValueError):
            stats.save_report(tempfile.mkdtemp(), {"host": "../x", "prompts": [], "tokens": [], "tools": []})

    def test_summarize_merges_hosts_within_window(self):
        now = datetime(2026, 11, 18, 12, 0, tzinfo=self.TZ)
        ts = lambda d, h: int(datetime(2026, 11, d, h, tzinfo=self.TZ).timestamp())
        reports = [
            {"host": "robin", "prompts": [[ts(18, 9), "thock", "s1"], [ts(18, 9), "thock", "s1"],
                                          [ts(11, 9), "old", "s0"]],
             "tokens": [[ts(18, 9), "thock", "claude-opus-5-5", 300], [ts(11, 9), "old", "claude-opus-5-5", 999]],
             "tools": [[ts(18, 9), "mcp__thock__read", 2], [ts(18, 9), "Bash", 5]]},
            {"host": "mac", "prompts": [[ts(12, 23), "lulyca", "s1"]],
             "tokens": [[ts(12, 23), "lulyca", "claude-fable-5-2", 100]], "tools": []},
        ]
        s = stats.summarize(reports, now)
        self.assertEqual(s["prompts"], 3)
        self.assertEqual(s["sessions"], 2)  # same session id on two hosts counts twice
        self.assertEqual(s["days"][0], ("Th", 1))
        self.assertEqual(s["days"][-1], ("We", 2))
        self.assertEqual(s["heat"], {(0, 23): 1, (6, 9): 2})
        self.assertEqual(s["models"], [("Opus 5.5", 300), ("Fable 5.2", 100)])
        self.assertEqual(s["projects"], [("thock", 300), ("lulyca", 100)])
        self.assertEqual(s["tools"], [("Bash", 5), ("thock", 2)])
        self.assertEqual(s["hosts"], ["mac", "robin"])

    def test_summarize_is_none_without_recent_activity(self):
        self.assertIsNone(stats.summarize([], datetime.now(self.TZ)))


class ReportEndpointTest(unittest.TestCase):
    def test_post_report_is_saved(self):
        d = tempfile.mkdtemp()
        server = ThreadingHTTPServer(("127.0.0.1", 0), claudindle.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/report"
        body = json.dumps({"host": "hp", "prompts": [], "tokens": [], "tools": []}).encode()
        with mock.patch.object(claudindle, "STATE_DIR", d), mock.patch("builtins.print"):
            with urllib.request.urlopen(urllib.request.Request(url, data=body)) as r:
                self.assertEqual(r.status, 204)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(url, data=b"{}"))
        cm.exception.close()
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(stats.load_reports(d)[0]["host"], "hp")


if __name__ == "__main__":
    unittest.main()

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import claudindle


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


class RenderPngTest(unittest.TestCase):
    def test_failure_is_logged_and_rendered(self):
        with mock.patch.object(claudindle, "fetch_usage", side_effect=claudindle.LoginExpired()), \
                mock.patch.object(claudindle, "render", wraps=claudindle.render) as render, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            png = claudindle.render_png()
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIn("Login expired", stderr.getvalue())
        render.assert_called_once_with(None, error="Login expired: run `claudindle.py login`")


if __name__ == "__main__":
    unittest.main()

"""Ascii2D 反 403 客户端单元测试。

覆盖：CSRF token / 会话 cookie 采集、结果页解析、Cloudflare 拦截识别、
完整提交流程（本地 mock 站点）、bovw 补检与降级错误信息。
"""

import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.ascii2d import (  # noqa: E402
    Ascii2DEngine,
    classify_response,
    extract_ray_id,
    find_bovw_url,
    parse_cookie_string,
    parse_results,
    parse_tokens,
)

HOME_HTML = """<!DOCTYPE html><html><body>
<form class="text-xs-center m-t-2" action="/search/uri" accept-charset="UTF-8" method="post">
  <input name="utf8" type="hidden" value="&#x2713;" />
  <input type="hidden" name="authenticity_token" value="URI_TOKEN_AAA" />
  <input class='form-control' id='uri-form' name='uri' placeholder='画像のURL' type='url'>
</form>
<form id="file_upload" action="/search/file" enctype="multipart/form-data" method="post">
  <input name="utf8" type="hidden" value="&#x2713;" />
  <input name="authenticity_token" type="hidden" value="FILE_TOKEN_BBB" />
  <input class='form-control' id='file-form' name='file' type='file'>
</form>
</body></html>"""


def _result_row(image, hash_value, detail, links, mark="", external=True):
    anchors = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    mark_html = f"<small>{mark}</small>" if mark else ""
    if external:
        inner = f'<div class="external">{anchors}{mark_html}</div>'
    else:
        inner = f"{anchors}{mark_html}"
    return f"""
  <div class="row item-box">
    <div class="col-md-2"><img src="{image}" class="image" /></div>
    <div class="col-md-10">
      <div class="hash">{hash_value}</div>
      <small>{detail}</small>
      <div class="detail-box gray-link">{inner}</div>
    </div>
  </div>"""


QUERY_ROW = """
  <div class="row item-box">
    <div class="col-md-2"><img src="/uploads/thumb/query.jpg" class="image" /></div>
    <div class="col-md-10">
      <div class="hash">queryhash000</div>
      <small>800×600 PNG 128KB</small>
      <h6>あなたが検索した画像</h6>
    </div>
  </div>"""

COLOR_HTML = f"""<!DOCTYPE html><html><body><div class="container">
{QUERY_ROW}
{_result_row("/uploads/thumb/1.jpg", "aaa111", "1200×900 PNG 512KB",
             [("https://www.pixiv.net/artworks/12345678", "夜景のイラスト"),
              ("https://www.pixiv.net/users/999", "画师A")], "pixiv")}
{_result_row("/uploads/thumb/2.jpg", "bbb222", "900×1200 JPG 256KB",
             [("https://twitter.com/artist_handle/status/9876543210", "@artist_handle")], "twitter")}
<div class="pull-xs-right"><a href="/search/file/bovw">特徴検索</a></div>
</div></body></html>"""

BOVW_HTML = f"""<!DOCTYPE html><html><body><div class="container">
{QUERY_ROW}
{_result_row("/uploads/thumb/3.jpg", "ccc333", "1000×1000 PNG 400KB",
             [("https://www.fanbox.cc/@creator/posts/5555", "支援者限定イラスト"),
              ("https://www.fanbox.cc/@creator", "creator")], "fanbox")}
</div></body></html>"""

BLOCK_HTML = """<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head>
<body><h1>Sorry, you have been blocked</h1>
<p>Cloudflare Ray ID: <strong class="cf-ray">a57d0783dc0595ad</strong></p></body></html>"""

CHALLENGE_HTML = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div id="cf-challenge-running">Verifying you are human</div>
<script>window._cf_chl_opt={cvId: '3'};</script></body></html>"""


class _MockState:
    def __init__(self):
        self.requests = []
        self.force_block = False
        self.home_status = 200
        self.tokens = {"uri": "URI_TOKEN_AAA", "file": "FILE_TOKEN_BBB"}
        self.session_cookie = "session-ok-123"


class _Handler(BaseHTTPRequestHandler):
    state: _MockState = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, status, body, headers=None):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        state = self.state
        state.requests.append(("GET", self.path, dict(self.headers), b""))
        if state.force_block:
            self._send(403, BLOCK_HTML)
            return
        if self.path == "/":
            cookie = f"_session_id={state.session_cookie}; path=/; HttpOnly"
            self._send(state.home_status, HOME_HTML, {"Set-Cookie": cookie})
            return
        if self.path.startswith("/search/file/bovw"):
            self._send(200, BOVW_HTML)
            return
        self._send(404, "<html><body>not found</body></html>")

    def do_POST(self):
        state = self.state
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        state.requests.append(("POST", self.path, dict(self.headers), body))
        if state.force_block:
            self._send(403, BLOCK_HTML)
            return

        cookie_header = self.headers.get("Cookie") or ""
        if f"_session_id={state.session_cookie}" not in cookie_header:
            self._send(422, "<html><body>Can't verify CSRF token authenticity</body></html>")
            return

        if self.path == "/search/uri":
            expected = state.tokens["uri"].encode()
        elif self.path == "/search/file":
            expected = state.tokens["file"].encode()
        else:
            self._send(404, "<html><body>not found</body></html>")
            return

        if expected not in body:
            self._send(422, "<html><body>Can't verify CSRF token authenticity</body></html>")
            return

        self._send(200, COLOR_HTML)


class _MockSite:
    def __init__(self):
        self.state = _MockState()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class TestAscii2DParsing(unittest.TestCase):
    """纯解析逻辑（离线）"""

    def test_parse_tokens(self):
        tokens = parse_tokens(HOME_HTML)
        self.assertEqual(tokens["uri"], "URI_TOKEN_AAA")
        self.assertEqual(tokens["file"], "FILE_TOKEN_BBB")

    def test_parse_tokens_value_before_name(self):
        html = '<form action="/search/file" method="post">' \
               '<input value="ZZZ" name="authenticity_token" /></form>'
        self.assertEqual(parse_tokens(html)["file"], "ZZZ")

    def test_parse_cookie_string(self):
        cookies = parse_cookie_string("cf_clearance=abc; _session_id=xyz;")
        self.assertEqual(cookies, {"cf_clearance": "abc", "_session_id": "xyz"})

    def test_classify_blocked_and_challenge(self):
        self.assertEqual(classify_response(403, BLOCK_HTML), "blocked")
        self.assertEqual(classify_response(403, CHALLENGE_HTML), "challenge")
        self.assertEqual(classify_response(200, COLOR_HTML), "ok")
        self.assertEqual(
            classify_response(403, "{}", {"cf-mitigated": "challenge"}), "challenge"
        )

    def test_extract_ray_id(self):
        self.assertEqual(extract_ray_id(BLOCK_HTML), "a57d0783dc0595ad")
        self.assertEqual(extract_ray_id("<html>no ray</html>"), "")

    def test_parse_results(self):
        rows = parse_results(COLOR_HTML, base_url="https://ascii2d.net")
        self.assertEqual(len(rows), 2, "搜索图那一行必须被跳过")

        first = rows[0]
        self.assertEqual(first["url"], "https://www.pixiv.net/artworks/12345678")
        self.assertEqual(first["title"], "夜景のイラスト")
        self.assertEqual(first["author"], "画师A")
        self.assertEqual(first["author_url"], "https://www.pixiv.net/users/999")
        self.assertEqual(first["source"], "pixiv")
        self.assertEqual(first["detail"], "1200×900 PNG 512KB")
        self.assertEqual(first["thumbnail"], "https://ascii2d.net/uploads/thumb/1.jpg")

        second = rows[1]
        self.assertEqual(second["source"], "twitter")
        self.assertIn("artist_handle", second["url"])

    def test_find_bovw_url(self):
        self.assertEqual(
            find_bovw_url(COLOR_HTML, "https://ascii2d.net/search/file/xxx/color", "https://ascii2d.net"),
            "https://ascii2d.net/search/file/bovw",
        )
        self.assertEqual(
            find_bovw_url("<html></html>", "https://ascii2d.net/search/x/color", "https://ascii2d.net"),
            "https://ascii2d.net/search/x/bovw",
        )


class TestAscii2DSearchFlow(unittest.IsolatedAsyncioTestCase):
    """对本地 mock 站点跑真实请求，验证 token / cookie / 解析 / 降级"""

    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_sample_ascii2d.png")
        with open(self._tmp, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    def tearDown(self):
        try:
            os.remove(self._tmp)
        except OSError:
            pass

    def _engine(self, base_url, **kwargs):
        kwargs.setdefault("browser_fallback", False)
        kwargs.setdefault("timeout", 10.0)
        return Ascii2DEngine(base_url=base_url, **kwargs)

    async def test_search_file_upload_hits_token_and_cookie(self):
        with _MockSite() as site:
            engine = self._engine(site.base_url)
            result = await engine.search(
                "https://example.com/pic.png", local_path=self._tmp, max_results=2
            )

            self.assertEqual(result.engine_name, "Ascii2D")
            self.assertIsNone(result.error)
            self.assertEqual(len(result.items), 2)
            self.assertEqual(result.items[0].platform, "Pixiv")
            self.assertEqual(result.items[0].author, "画师A")
            self.assertEqual(result.items[1].platform, "X (Twitter)")
            self.assertEqual(result.best_similarity, 75.0)

            posts = [r for r in site.state.requests if r[0] == "POST"]
            self.assertTrue(posts, "必须真正提交过搜索请求")
            method, path, headers, body = posts[0]
            self.assertEqual(path, "/search/file")
            self.assertIn(b"FILE_TOKEN_BBB", body)
            self.assertIn(b"multipart/form-data", headers.get("Content-Type", "").encode())
            self.assertIn("_session_id=session-ok-123", headers.get("Cookie", ""))
            self.assertEqual(headers.get("Origin"), site.base_url)
            self.assertEqual(headers.get("Referer"), site.base_url + "/")
            self.assertIn("Sec-Fetch-Site", headers)

    async def test_search_uri_mode(self):
        with _MockSite() as site:
            engine = self._engine(site.base_url, max_attempts=2)
            result = await engine.search("https://example.com/pic.png", local_path=None, max_results=2)
            self.assertEqual(len(result.items), 2)
            posts = [r for r in site.state.requests if r[0] == "POST"]
            self.assertEqual(posts[0][1], "/search/uri")
            self.assertIn(b"URI_TOKEN_AAA", posts[0][3])
            self.assertIn(b"example.com", posts[0][3])

    async def test_bovw_supplement(self):
        with _MockSite() as site:
            engine = self._engine(site.base_url)
            result = await engine.search(
                "https://example.com/pic.png", local_path=self._tmp, max_results=3
            )
            self.assertEqual(len(result.items), 3)
            self.assertEqual(result.items[2].platform, "Fanbox")
            self.assertEqual(result.items[2].extra_info["mode"], "bovw")
            self.assertTrue(
                any(r[0] == "GET" and "bovw" in r[1] for r in site.state.requests),
                "应补发一次 bovw 特征检索",
            )

    async def test_blocked_site_returns_actionable_error(self):
        with _MockSite() as site:
            site.state.force_block = True
            engine = self._engine(site.base_url, max_attempts=2)
            result = await engine.search(
                "https://example.com/pic.png", local_path=self._tmp, max_results=2
            )
            self.assertEqual(result.items, [])
            self.assertIsNotNone(result.error)
            self.assertIn("Cloudflare", result.error)
            self.assertIn("ascii2d_cookie", result.error)

    async def test_httpx_transport_also_works(self):
        with _MockSite() as site:
            engine = self._engine(site.base_url, max_attempts=6)
            # 直接从 httpx 通道提交，验证无 curl_cffi 时的降级路径
            submission = await engine._submit("file", "httpx", "", "", self._tmp)
            self.assertEqual(submission.kind, "ok")
            items = engine._to_items(
                engine._parse_page(submission, "file")[0], "file", 2
            )
            self.assertEqual(len(items), 2)


if __name__ == "__main__":
    unittest.main()

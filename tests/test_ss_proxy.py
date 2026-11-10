"""内置 Shadowsocks 隧道 + 本地混合代理（SOCKS5 / HTTP CONNECT）端到端测试。

用一个内存里的 "mock SS 服务器" 当节点：它按 SIP004 解密客户端流量、把明文原样
回显（Echo），于是不依赖任何外网就能验证：地址封装、分块、nonce、两种代理协议、
以及和 Ascii2D 引擎的接线。
"""

import asyncio
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from proxy.shadowsocks import (  # noqa: E402
    MixedProxyServer,
    ShadowsocksError,
    ShadowsocksProxy,
    SSNode,
    _parse_simple_yaml,
    encode_address,
    evp_bytes_to_key,
    load_proxy_nodes,
    parse_ss_uri,
    select_proxy_node,
)
from proxy.ss_crypto import (  # noqa: E402
    METHOD_AES_128_GCM,
    METHOD_CHACHA20_POLY1305,
    AeadCipher,
    TAG_LEN,
    hkdf_sha1,
    key_length,
)


class MockShadowsocksServer:
    """最小的 Shadowsocks AEAD 服务端：解密客户端流并原样回显。"""

    def __init__(self, method: str = METHOD_AES_128_GCM, password: str = "test-password") -> None:
        self.method = method
        self.password = password
        self.key_len = key_length(method)
        self.addresses = []
        self.port = 0
        self._server = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader, writer) -> None:
        try:
            master = evp_bytes_to_key(self.password, self.key_len)
            salt = await reader.readexactly(self.key_len)
            dec = AeadCipher(self.method, hkdf_sha1(master, salt, length=self.key_len))

            chunk_len = struct.unpack(">H", dec.decrypt(await reader.readexactly(2 + TAG_LEN)))[0]
            address = dec.decrypt(await reader.readexactly(chunk_len + TAG_LEN))
            self.addresses.append(address)

            out_salt = os.urandom(self.key_len)
            enc = AeadCipher(self.method, hkdf_sha1(master, out_salt, length=self.key_len))
            writer.write(out_salt)
            await writer.drain()

            while True:
                header = await reader.readexactly(2 + TAG_LEN)
                size = struct.unpack(">H", dec.decrypt(header))[0]
                payload = dec.decrypt(await reader.readexactly(size + TAG_LEN))
                writer.write(enc.encrypt(struct.pack(">H", len(payload))))
                writer.write(enc.encrypt(payload))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as exc:  # 协议错误直接暴露给测试
            self.last_error = exc
        finally:
            try:
                writer.close()
            except Exception:
                pass


def decode_address(raw: bytes):
    atyp = raw[0]
    if atyp == 1:
        host = ".".join(str(b) for b in raw[1:5])
        offset = 5
    elif atyp == 3:
        length = raw[1]
        host = raw[2 : 2 + length].decode()
        offset = 2 + length
    else:
        host = str(raw[1:17])
        offset = 17
    port = struct.unpack(">H", raw[offset : offset + 2])[0]
    return host, port


class TestUriParsing(unittest.TestCase):
    def test_sip002_base64_userinfo(self):
        node = parse_ss_uri("ss://YWVzLTEyOC1nY206c2VjcmV0@1.2.3.4:8388#tokyo")
        self.assertEqual((node.server, node.port), ("1.2.3.4", 8388))
        self.assertEqual(node.method, "aes-128-gcm")
        self.assertEqual(node.password, "secret")
        self.assertEqual(node.tag, "tokyo")

    def test_plain_userinfo_with_escapes(self):
        node = parse_ss_uri("ss://chacha20-ietf-poly1305:p%40ss%3Aword@example.com:443")
        self.assertEqual(node.password, "p@ss:word")
        self.assertEqual(node.method, METHOD_CHACHA20_POLY1305)

    def test_legacy_format(self):
        node = parse_ss_uri("9.9.9.9:8388:aes-256-gcm:secret")
        self.assertEqual((node.server, node.port, node.method), ("9.9.9.9", 8388, "aes-256-gcm"))

    def test_full_base64_body(self):
        import base64

        body = base64.urlsafe_b64encode(b"aes-128-gcm:pw@1.1.1.1:8388").decode().rstrip("=")
        node = parse_ss_uri(f"ss://{body}")
        self.assertEqual((node.server, node.port, node.password), ("1.1.1.1", 8388, "pw"))

    def test_errors(self):
        with self.assertRaises(ShadowsocksError):
            parse_ss_uri("")
        with self.assertRaises(ShadowsocksError):
            parse_ss_uri("ss://cmM0LW1kNTpwdw@1.2.3.4:8388")  # rc4-md5 不支持
        with self.assertRaises(ShadowsocksError):
            parse_ss_uri("1.2.3.4:8388")  # 缺方法/密码

    def test_encode_address(self):
        self.assertEqual(
            encode_address("1.2.3.4", 80), b"\x01" + bytes([1, 2, 3, 4]) + b"\x00P"
        )
        self.assertEqual(
            encode_address("example.com", 443),
            b"\x03" + bytes([11]) + b"example.com" + struct.pack(">H", 443),
        )
        self.assertEqual(encode_address("::1", 8080)[0], 0x04)


CLASH_JSON = """{
  "port": 7890,
  "mode": "rule",
  "proxies": [
    {"name": "trojan-A", "type": "trojan", "server": "1.1.1.1", "port": 443, "password": "x"},
    {"name": "vmess-B", "type": "vmess", "server": "1.1.1.2", "port": 443, "uuid": "u"},
    {"name": "JP-RES-01", "type": "ss", "server": "2.2.2.2", "port": 8388,
     "cipher": "aes-128-gcm", "password": "pw1"},
    {"name": "JP-RES-02", "type": "ss", "server": "3.3.3.3", "port": 8389,
     "cipher": "chacha20-ietf-poly1305", "password": "pw2"}
  ],
  "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": ["JP-RES-01", "JP-RES-02"]}],
  "rules": ["MATCH,PROXY"]
}"""

CLASH_YAML = """
port: 7890
proxies:
  - name: "trojan-A"
    type: trojan
    server: 1.1.1.1
    port: 443
    password: x
  - name: "JP-YAML-01"   # 住宅节点
    type: ss
    server: 4.4.4.4
    port: 8390
    cipher: aes-256-gcm
    password: "pw3"
proxy-groups:
  - name: PROXY
    type: select
    proxies: [JP-YAML-01]
"""

SINGBOX_JSON = """{
  "outbounds": [
    {"type": "direct", "tag": "direct"},
    {"type": "shadowsocks", "tag": "jp-box", "server": "5.5.5.5", "server_port": 8391,
     "method": "chacha20-ietf-poly1305", "password": "pw4"}
  ]
}"""


class TestProxyConfigSources(unittest.TestCase):
    """Clash JSON / YAML、sing-box、base64 订阅、链接混排"""

    def test_clash_json_skips_unsupported_types(self):
        nodes = load_proxy_nodes(CLASH_JSON)
        self.assertEqual([n.tag for n in nodes], ["JP-RES-01", "JP-RES-02"])
        self.assertEqual(nodes[0].server, "2.2.2.2")
        self.assertEqual(nodes[1].method, METHOD_CHACHA20_POLY1305)

    def test_clash_json_select_by_name(self):
        nodes = load_proxy_nodes(CLASH_JSON)
        self.assertEqual(select_proxy_node(nodes).tag, "JP-RES-01")
        self.assertEqual(select_proxy_node(nodes, "res-02").tag, "JP-RES-02")
        self.assertEqual(select_proxy_node(nodes, "RES-01").tag, "JP-RES-01")
        with self.assertRaises(ShadowsocksError) as ctx:
            select_proxy_node(nodes, "nope")
        self.assertIn("JP-RES-01", str(ctx.exception))

    def test_single_proxy_object(self):
        nodes = load_proxy_nodes(
            '{"name":"solo","type":"ss","server":"6.6.6.6","port":1080,'
            '"cipher":"aes-128-gcm","password":"pw"}'
        )
        self.assertEqual((nodes[0].server, nodes[0].tag), ("6.6.6.6", "solo"))

    def test_singbox_outbounds(self):
        nodes = load_proxy_nodes(SINGBOX_JSON)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].tag, "jp-box")
        self.assertEqual(nodes[0].port, 8391)

    def test_json_array_of_ss_links(self):
        link = "ss://YWVzLTEyOC1nY206cHc@7.7.7.7:8388"
        nodes = load_proxy_nodes(f'["{link}", "trojan://x@1.1.1.1:443"]')
        self.assertEqual(nodes[0].server, "7.7.7.7")

    def test_yaml_block_style(self):
        nodes = load_proxy_nodes(CLASH_YAML)
        self.assertEqual([n.tag for n in nodes], ["JP-YAML-01"])
        self.assertEqual(nodes[0].method, "aes-256-gcm")
        self.assertEqual(nodes[0].password, "pw3")

    def test_yaml_fallback_parser_without_pyyaml(self):
        # 直接测兜底解析器（不依赖 PyYAML 是否安装）
        data = _parse_simple_yaml(CLASH_YAML)
        self.assertEqual(data["proxies"][1]["name"], "JP-YAML-01")
        self.assertEqual(data["proxies"][1]["port"], 8390)

    def test_base64_subscription(self):
        body = "\n".join(
            [
                "ss://YWVzLTEyOC1nY206cHc@8.8.8.8:8388#sub-1",
                "ss://YWVzLTI1Ni1nY206cHc@9.9.9.9:8389#sub-2",
            ]
        )
        nodes = load_proxy_nodes(_b64(body))
        self.assertEqual([n.tag for n in nodes], ["sub-1", "sub-2"])

    def test_multiline_links(self):
        nodes = load_proxy_nodes(
            "ss://YWVzLTEyOC1nY206cHc@1.1.1.1:1#a\nss://YWVzLTEyOC1nY206cHc@2.2.2.2:2#b"
        )
        self.assertEqual([n.tag for n in nodes], ["a", "b"])

    def test_invalid_json_reports_clearly(self):
        with self.assertRaises(ShadowsocksError) as ctx:
            load_proxy_nodes('{"proxies": [ }')
        self.assertIn("JSON", str(ctx.exception))

    def test_config_without_ss_node(self):
        with self.assertRaises(ShadowsocksError):
            load_proxy_nodes('{"proxies":[{"type":"trojan","server":"1.1.1.1","port":443}]}')


class TestTunnelAndProxy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = MockShadowsocksServer()
        await self.upstream.start()
        self.node = SSNode("127.0.0.1", self.upstream.port, METHOD_AES_128_GCM, "test-password")
        self.proxy = MixedProxyServer(self.node)
        await self.proxy.start()

    async def asyncTearDown(self):
        await self.proxy.stop()
        await self.upstream.stop()

    async def test_socks5_roundtrip(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), b"\x05\x00")

        host = b"example.com"
        writer.write(
            b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack(">H", 8080)
        )
        await writer.drain()
        reply = await reader.readexactly(10)
        self.assertEqual(reply[:2], b"\x05\x00")

        payload = b"ping-through-tunnel"
        writer.write(payload)
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(len(payload)), timeout=5)
        self.assertEqual(echoed, payload)

        target = decode_address(self.upstream.addresses[0])
        self.assertEqual(target, ("example.com", 8080))
        writer.close()

    async def test_http_connect_roundtrip(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        self.assertIn(b"200 Connection Established", head)

        writer.write(b"hello")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(5), timeout=5), b"hello")
        self.assertEqual(decode_address(self.upstream.addresses[0]), ("example.com", 443))
        writer.close()

    async def test_http_absolute_uri_rewrites_request(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        writer.write(b"GET http://example.com/path?q=1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        await writer.drain()
        # Echo 服务把改写后的请求原样送回，检查请求行已被改成 origin-form
        echoed = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        self.assertTrue(echoed.startswith(b"GET /path?q=1 HTTP/1.1"), echoed)
        writer.close()

    async def test_chacha20_node(self):
        upstream = MockShadowsocksServer(METHOD_CHACHA20_POLY1305, "pw2")
        await upstream.start()
        node = SSNode("127.0.0.1", upstream.port, METHOD_CHACHA20_POLY1305, "pw2")
        server = MixedProxyServer(node)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            await reader.readexactly(2)
            writer.write(b"\x05\x01\x00\x01\x01\x02\x03\x04" + struct.pack(">H", 80))
            await writer.drain()
            await reader.readexactly(10)
            writer.write(b"chacha")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(6), timeout=5), b"chacha")
            writer.close()
        finally:
            await server.stop()
            await upstream.stop()

    async def test_facade_lifecycle(self):
        facade = ShadowsocksProxy(
            f"ss://{_b64('aes-128-gcm:test-password')}@127.0.0.1:{self.upstream.port}"
        )
        self.assertFalse(facade.running)
        self.assertTrue(await facade.ensure_started())
        self.assertTrue(facade.running)
        self.assertTrue(facade.socks_url.startswith("socks5h://127.0.0.1:"))
        self.assertTrue(facade.http_url.startswith("http://127.0.0.1:"))
        await facade.stop()
        self.assertFalse(facade.running)

    async def test_bad_password_fails_cleanly(self):
        node = SSNode("127.0.0.1", self.upstream.port, METHOD_AES_128_GCM, "wrong-password")
        server = MixedProxyServer(node)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"CONNECT example.com:80 HTTP/1.1\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            self.assertIn(b"200", head)  # 握手阶段不校验密码
            writer.write(b"data")
            await writer.drain()
            with self.assertRaises((asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError)):
                await asyncio.wait_for(reader.readexactly(4), timeout=3)
            writer.close()
        finally:
            await server.stop()


def _b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class TestEngineWiring(unittest.IsolatedAsyncioTestCase):
    async def test_engine_starts_tunnel_and_uses_it(self):
        from engines.ascii2d import Ascii2DEngine

        upstream = MockShadowsocksServer()
        await upstream.start()
        try:
            uri = f"ss://{_b64('aes-128-gcm:test-password')}@127.0.0.1:{upstream.port}"
            engine = Ascii2DEngine(ss_uri=uri, timeout=5, browser_fallback=False)
            socks_url, http_url = await engine._proxy_pair()
            self.assertTrue(socks_url.startswith("socks5h://127.0.0.1:"))
            self.assertTrue(http_url.startswith("http://127.0.0.1:"))
            # 混合口两种协议都指向同一端口
            self.assertEqual(socks_url.rsplit(":", 1)[1], http_url.rsplit(":", 1)[1])
            await engine.aclose()
            self.assertFalse(engine._ss_proxy.running)
        finally:
            await upstream.stop()

    async def test_engine_applies_node_selector(self):
        from engines.ascii2d import Ascii2DEngine

        upstream = MockShadowsocksServer()
        await upstream.start()
        try:
            config = (
                '{"proxies": ['
                '{"name": "other", "type": "ss", "server": "1.1.1.1", "port": 1,'
                ' "cipher": "aes-128-gcm", "password": "x"},'
                f'{{"name": "jp-pick", "type": "ss", "server": "127.0.0.1", "port": {upstream.port},'
                ' "cipher": "aes-128-gcm", "password": "test-password"}]}'
            )
            engine = Ascii2DEngine(ss_uri=config, ss_node="jp-pick", timeout=5)
            self.assertTrue(await engine._ss_proxy.ensure_started())
            self.assertEqual(engine._ss_proxy.node.tag, "jp-pick")
            self.assertEqual(engine._ss_proxy.node.port, upstream.port)
            await engine.aclose()
        finally:
            await upstream.stop()

    async def test_engine_without_ss_uri_falls_back(self):
        from engines.ascii2d import Ascii2DEngine

        engine = Ascii2DEngine(proxy="http://127.0.0.1:9", timeout=5)
        self.assertEqual(await engine._proxy_pair(), ("http://127.0.0.1:9", "http://127.0.0.1:9"))


if __name__ == "__main__":
    unittest.main()

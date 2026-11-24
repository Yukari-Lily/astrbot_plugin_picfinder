"""Clash SS/Trojan node parsing and local proxy lifecycle tests."""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from proxy import ProxyService, load_proxy_nodes, select_proxy_node  # noqa: E402
from proxy.trojan import TrojanTunnel, _encode_address  # noqa: E402

NODES = """
  - {name: t1, server: 1.2.3.4, port: 10111, type: trojan, password: pwd, sni: m.ctrip.com, skip-cert-verify: true}
  - {name: s1, server: 5.6.7.8, port: 495, type: ss, cipher: aes-128-gcm, password: pwd2}
"""


class TestProxyNodes(unittest.TestCase):
    def test_parse_clash_nodes(self):
        nodes = load_proxy_nodes(NODES)
        self.assertEqual([node.kind for node in nodes], ["trojan", "ss"])
        trojan, ss = nodes
        self.assertEqual(trojan.sni, "m.ctrip.com")
        self.assertTrue(trojan.skip_cert_verify)
        self.assertEqual(ss.method, "aes-128-gcm")
        self.assertEqual(ss.port, 495)

    def test_select_by_name(self):
        nodes = load_proxy_nodes(NODES)
        self.assertEqual(select_proxy_node(nodes, "s1").kind, "ss")
        self.assertEqual(select_proxy_node(nodes, "").kind, "trojan")

        async def select_second():
            service = ProxyService(NODES, select="2")
            self.assertTrue(await service.ensure_started())
            self.assertEqual(service.node.kind, "ss")
            await service.stop()

        asyncio.run(select_second())

    def test_trojan_uri(self):
        node = load_proxy_nodes("trojan://pwd@example.com:443?sni=a.example&allowInsecure=1")[0]
        self.assertEqual(node.kind, "trojan")
        self.assertTrue(node.skip_cert_verify)
        self.assertEqual(node.sni, "a.example")

    def test_address_encoding(self):
        self.assertEqual(_encode_address("1.2.3.4", 80), b"\x01\x01\x02\x03\x04\x00P")
        payload = _encode_address("example.com", 443)
        self.assertEqual(payload[0], 0x03)
        self.assertEqual(payload[1], len("example.com"))

    def test_local_proxy_lifecycle(self):
        async def run():
            service = ProxyService(NODES, select="s1")
            self.assertTrue(service.socks_url.startswith("socks5h://127.0.0.1:"))
            self.assertTrue(service.browser_url.startswith("socks5://127.0.0.1:"))
            self.assertTrue(await service.ensure_started())
            await service.stop()

        asyncio.run(run())

    def test_trojan_tunnel_type(self):
        node = load_proxy_nodes(NODES)[0]
        tunnel = TrojanTunnel(node)
        self.assertEqual(tunnel.node.kind, "trojan")


if __name__ == "__main__":
    unittest.main()

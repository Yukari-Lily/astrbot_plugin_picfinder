"""出口自检（代理是否真的生效）测试，不访问网络。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio  # noqa: E402

from core.egress import (  # noqa: E402
    parse_ipinfo,
    parse_ipip,
    parse_ipify,
    parse_ipwhois,
    parse_ipapi,
    probe,
)


class TestParse(unittest.TestCase):
    def test_ipinfo(self):
        info = parse_ipinfo(
            {
                "ip": "203.0.113.9",
                "city": "Tokyo",
                "region": "Tokyo",
                "country": "JP",
                "org": "AS2516 KDDI",
            }
        )
        self.assertTrue(info.ok)
        self.assertEqual(info.ip, "203.0.113.9")
        self.assertEqual(info.where(), "JP Tokyo")
        self.assertIn("KDDI", info.line())

    def test_ipip(self):
        info = parse_ipip(
            {
                "ret": "ok",
                "data": {
                    "ip": "198.51.100.7",
                    "location": ["日本", "东京都", "东京", "", "al-pha.jp"],
                },
            }
        )
        self.assertEqual(info.ip, "198.51.100.7")
        self.assertEqual(info.country, "日本")
        self.assertEqual(info.city, "东京")
        self.assertEqual(info.org, "al-pha.jp")

    def test_ipip_error(self):
        info = parse_ipip({"ret": "failed", "msg": "额度用完了"})
        self.assertFalse(info.ok)
        self.assertEqual(info.error, "额度用完了")

    def test_ipwhois(self):
        info = parse_ipwhois(
            {
                "ip": "126.8.250.40",
                "success": True,
                "country": "Japan",
                "region": "Tokyo",
                "city": "Tokyo",
                "connection": {"isp": "SoftBank Corp."},
            }
        )
        self.assertEqual(info.ip, "126.8.250.40")
        self.assertEqual(info.country, "Japan")
        self.assertEqual(info.org, "SoftBank Corp.")

    def test_ipwhois_failure(self):
        info = parse_ipwhois({"success": False, "message": "reserved range"})
        self.assertFalse(info.ok)
        self.assertIn("reserved range", info.error)

    def test_ipapi(self):
        info = parse_ipapi(
            {
                "status": "success",
                "country": "Japan",
                "regionName": "Tokyo",
                "city": "Tokyo",
                "isp": "SoftBank",
                "query": "126.8.250.40",
            }
        )
        self.assertEqual(info.ip, "126.8.250.40")
        self.assertEqual(info.region, "Tokyo")
        self.assertEqual(info.org, "SoftBank")

    def test_ipify(self):
        self.assertEqual(parse_ipify({"ip": "192.0.2.1"}).ip, "192.0.2.1")
        self.assertEqual(parse_ipify({"ip": "192.0.2.1"}).line(), "192.0.2.1")


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    """按 URL 关键字返回预设响应；值为 Exception 时抛出。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        for key, value in self.mapping.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise RuntimeError("no route")


class TestProbe(unittest.TestCase):
    def test_merges_sources_when_one_fails(self):
        client = FakeClient(
            {
                "ipinfo.io": RuntimeError("blocked"),
                "myip.ipip.net": FakeResp(200, {"ret": "ok", "data": {"ip": "126.8.250.40", "location": ["日本", "东京都", "东京"]}}),
                "ipify": FakeResp(200, {"ip": "126.8.250.40"}),
            }
        )
        info = asyncio.run(probe(client))
        self.assertEqual(info.ip, "126.8.250.40")
        self.assertEqual(info.country, "日本")
        self.assertEqual(info.city, "东京")
        self.assertTrue(info.ok)

    def test_fills_geo_from_second_source(self):
        client = FakeClient(
            {
                "ipinfo.io": FakeResp(200, {"ip": "1.2.3.4", "country": "US"}),
                "myip.ipip.net": FakeResp(200, {"ret": "ok", "data": {"ip": "1.2.3.4", "location": ["美国", "加州", "洛杉矶"]}}),
                "ipify": FakeResp(200, {"ip": "1.2.3.4"}),
            }
        )
        info = asyncio.run(probe(client))
        self.assertEqual(info.ip, "1.2.3.4")
        self.assertEqual(info.country, "US")

    def test_all_fail_reports_error(self):
        client = FakeClient(
            {
                "ipinfo.io": FakeResp(403, {}),
                "myip.ipip.net": FakeResp(200, {"ret": "failed", "msg": "限额"}),
                "ipify": RuntimeError("timeout"),
            }
        )
        info = asyncio.run(probe(client))
        self.assertFalse(info.ok)
        self.assertTrue(info.error)


if __name__ == "__main__":
    unittest.main()

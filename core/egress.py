"""出口自检：确认流量到底从哪个 IP 出去。

「填了代理」不等于「走了代理」：本地 SOCKS 端口没监听、节点密码错、
浏览器没带代理参数，都会静默退回直连。插件启动后会用这里采集一次出口
IP/地区，和直连对比后写进日志。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional


_IPINFO = "https://ipinfo.io/json"
_IPIP = "https://myip.ipip.net/json"
_IPWHOIS = "https://ipwho.is/"
_IPAPI = "http://ip-api.com/json/?fields=status,country,regionName,city,isp,query"
_IPIFY = "https://api.ipify.org?format=json"


@dataclass

class Egress:
    """一条出口的身份。"""

    ip: str = ""
    country: str = ""
    region: str = ""
    city: str = ""
    org: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.ip)

    def where(self) -> str:
        parts: List[str] = []
        for value in (self.country, self.region, self.city):
            value = (value or "").strip()
            if value and value not in parts:
                parts.append(value)
        return " ".join(parts)

    def line(self) -> str:
        if not self.ip:
            return f"探测失败（{self.error or '无响应'}）"
        text = self.ip
        where = self.where()
        if where:
            text += f" · {where}"
        if self.org:
            text += f" · {self.org}"
        return text


def parse_ipinfo(payload: dict) -> Egress:
    data = payload or {}
    return Egress(
        ip=str(data.get("ip") or ""),
        country=str(data.get("country") or ""),
        region=str(data.get("region") or ""),
        city=str(data.get("city") or ""),
        org=str(data.get("org") or ""),
    )


def parse_ipip(payload: dict) -> Egress:
    data = (payload or {}).get("data") or {}
    if str((payload or {}).get("ret") or "").lower() not in ("", "ok"):
        return Egress(error=str((payload or {}).get("msg") or "ipip 返回异常"))
    location = data.get("location") or []
    if not isinstance(location, list):
        location = []
    fields = [str(item or "") for item in location[:3]]
    return Egress(
        ip=str(data.get("ip") or ""),
        country=fields[0] if len(fields) > 0 else "",
        region=fields[1] if len(fields) > 1 else "",
        city=fields[2] if len(fields) > 2 else "",
        org=str(location[4] if len(location) > 4 else ""),
    )


def parse_ipwhois(payload: dict) -> Egress:
    data = payload or {}
    if data.get("success") is False:
        return Egress(error=f"ipwho.is: {data.get('message') or '查询失败'}")
    connection = data.get("connection") or {}
    return Egress(
        ip=str(data.get("ip") or ""),
        country=str(data.get("country") or ""),
        region=str(data.get("region") or ""),
        city=str(data.get("city") or ""),
        org=str(connection.get("isp") or connection.get("org") or ""),
    )


def parse_ipapi(payload: dict) -> Egress:
    data = payload or {}
    if str(data.get("status") or "").lower() == "fail":
        return Egress(error=f"ip-api: {data.get('message') or '查询失败'}")
    return Egress(
        ip=str(data.get("query") or ""),
        country=str(data.get("country") or ""),
        region=str(data.get("regionName") or ""),
        city=str(data.get("city") or ""),
        org=str(data.get("isp") or ""),
    )


def parse_ipify(payload: dict) -> Egress:
    return Egress(ip=str((payload or {}).get("ip") or ""))


async def _fetch(client, url: str, parser, timeout: float) -> Egress:
    host = url.split("/")[2]
    try:
        resp = await client.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except Exception as exc:
        return Egress(error=f"{host}: {exc}"[:80])
    if resp.status != 200:
        return Egress(error=f"{host}: HTTP {resp.status}")
    try:
        data = resp.json()
    except Exception:
        return Egress(error=f"{host}: 返回不是 JSON")
    info = parser(data)
    if not info.ok:
        info.error = f"{host}: 无 IP 字段"
    return info


async def probe(client, *, timeout: float = 8.0) -> Egress:
    """采集当前客户端的出口信息。

    多个回显服务并发问：有的服务在部分代理出口下会被墙/限流（机房 IP 尤其常见），
    只要有一个回了 IP 就算成功，剩下的用来补地区与运营商。
    """
    endpoints = (
        (_IPINFO, parse_ipinfo),
        (_IPIP, parse_ipip),
        (_IPWHOIS, parse_ipwhois),
        (_IPAPI, parse_ipapi),
        (_IPIFY, parse_ipify),
    )
    answers = await asyncio.gather(*(_fetch(client, url, parser, timeout) for url, parser in endpoints))
    best: Optional[Egress] = None
    errors: List[str] = []
    for info in answers:
        if not info.ok:
            if info.error:
                errors.append(info.error)
            continue
        if best is None:
            best = info
            continue
        best.country = best.country or info.country
        best.region = best.region or info.region
        best.city = best.city or info.city
        best.org = best.org or info.org
    return best or Egress(error="；".join(errors[-2:]) or "探测失败")


__all__ = ["Egress", "parse_ipapi", "parse_ipinfo", "parse_ipip", "parse_ipify", "parse_ipwhois", "probe"]

"""Trojan 协议 TCP 客户端。

Trojan 把普通 TCP 流量藏进 TLS：
    hex(SHA224(password)) CRLF
    0x01 (CONNECT) + SOCKS 地址 + CRLF
    之后就是原始字节。
只实现 TCP CONNECT；UDP/WebSocket/gRPC 节点不支持。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import ssl
import struct
from typing import Optional

CONNECT_TIMEOUT = 20.0


class TrojanError(RuntimeError):
    pass


def _encode_address(host: str, port: int) -> bytes:
    try:
        raw = host.encode("idna")
    except UnicodeError:
        raw = host.encode("utf-8")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv4Address):
        return b"\x01" + address.packed + struct.pack(">H", port)
    if isinstance(address, ipaddress.IPv6Address):
        return b"\x04" + address.packed + struct.pack(">H", port)
    if len(raw) > 255:
        raise TrojanError("域名过长")
    return b"\x03" + bytes([len(raw)]) + raw + struct.pack(">H", port)


class TrojanTunnel:
    def __init__(self, node, timeout: float = CONNECT_TIMEOUT):
        self.node = node
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def open(self, host: str, port: int) -> None:
        context = ssl.create_default_context()
        if getattr(self.node, "skip_cert_verify", False):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        sni = getattr(self.node, "sni", "") or self.node.server
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.node.server,
                    self.node.port,
                    ssl=context,
                    server_hostname=sni,
                ),
                timeout=self.timeout,
            )
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
            raise TrojanError(f"连接 Trojan 服务器 {self.node.server}:{self.node.port} 失败: {exc}") from exc

        auth = hashlib.sha224(self.node.password.encode("utf-8")).hexdigest().encode("ascii")
        self._writer.write(auth + b"\r\n" + b"\x01" + _encode_address(host, port) + b"\r\n")
        await self._writer.drain()

    async def read(self, size: int = 65536) -> bytes:
        if self._reader is None:
            return b""
        try:
            return await self._reader.read(size)
        except (ConnectionError, asyncio.IncompleteReadError):
            return b""

    async def write(self, data: bytes) -> None:
        if self._writer is None or not data:
            return
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

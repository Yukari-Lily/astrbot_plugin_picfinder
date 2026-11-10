"""插件内置的极简 Shadowsocks 客户端 + 本地混合代理（SOCKS5 / HTTP CONNECT）。

为什么放在插件里：Ascii2D 对机房 ASN 风控极严，要么配住宅线路，要么别想进搜索页。
让用户为了一个引擎再去装 clash / v2ray 太折腾，所以这里自带一条 SS 隧道：
解析 ``ss://`` 链接 → 在 127.0.0.1 起一个混合代理口 → 引擎把代理指过去。

只实现 TCP over AEAD（aes-128-gcm / aes-256-gcm / chacha20-ietf-poly1305），
即 SIP004 协议；不碰 UDP、插件、obfs，够用且好审。

单独调试也可以直接跑::

    python -m proxy.shadowsocks --uri "ss://..." --port 1080
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from .ss_crypto import (
    AeadCipher,
    TAG_LEN,
    hkdf_sha1,
    is_supported_method,
    key_length,
)

logger = logging.getLogger("astrbot_plugin_picfinder.ss_proxy")

MAX_CHUNK = 0x3FFF
RELAY_BUFFER = 65536
CONNECT_TIMEOUT = 20.0


class ShadowsocksError(RuntimeError):
    """隧道/协议层错误。"""


# ----------------------------------------------------------------------
# ss:// 链接解析
# ----------------------------------------------------------------------


@dataclass
class SSNode:
    server: str
    port: int
    method: str
    password: str
    tag: str = ""

    @property
    def masked(self) -> str:
        return f"{self.server}:{self.port} ({self.method})"


def _b64decode_loose(text: str) -> str:
    text = text.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ShadowsocksError(f"ss:// 链接的 base64 部分无法解析: {exc}") from exc


def parse_ss_uri(uri: str) -> SSNode:
    """解析 SIP002 ``ss://`` 链接，同时兼容 ``host:port:method:password`` 写法。"""
    raw = (uri or "").strip()
    if not raw:
        raise ShadowsocksError("ss 链接为空")

    if not raw.lower().startswith("ss://"):
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, method, password = parts
            return SSNode(host.strip(), int(port), method.strip().lower(), password.strip())
        raise ShadowsocksError("ss 链接格式不对，应为 ss://... 或 host:port:method:password")

    body = raw[5:]
    tag = ""
    if "#" in body:
        body, _, tag = body.partition("#")
        tag = unquote(tag)
    body = body.split("?")[0]

    if "@" in body:
        userinfo, _, hostport = body.rpartition("@")
        if ":" not in _safe_unquote(userinfo):
            userinfo = _b64decode_loose(userinfo)
        else:
            userinfo = _safe_unquote(userinfo)
        method, _, password = userinfo.partition(":")
        if not method or not password:
            raise ShadowsocksError("ss 链接缺少加密方法或密码")
        parsed = urlsplit("//" + hostport)
        host, port = parsed.hostname or "", parsed.port
    else:
        decoded = _b64decode_loose(body)
        method, _, rest = decoded.partition(":")
        if "@" not in rest:
            raise ShadowsocksError("ss 链接缺少服务器地址")
        password, _, hostport = rest.rpartition("@")
        parsed = urlsplit("//" + hostport)
        host, port = parsed.hostname or "", parsed.port

    method = (method or "").strip().lower()
    if not host or not port:
        raise ShadowsocksError("ss 链接缺少服务器地址或端口")
    if not is_supported_method(method):
        raise ShadowsocksError(
            f"不支持 {method or '未知'} 加密（仅支持 aes-128-gcm / aes-256-gcm / chacha20-ietf-poly1305）"
        )
    return SSNode(str(host), int(port), method, password, tag)


def _safe_unquote(text: str) -> str:
    try:
        return unquote(text)
    except Exception:  # pragma: no cover
        return text


# ----------------------------------------------------------------------
# 多种代理配置来源：ss:// 链接、Clash JSON / Clash YAML、sing-box outbounds、
# base64 订阅、host:port:method:password
# ----------------------------------------------------------------------


def _entry_to_node(entry: Any, index: int) -> Optional[SSNode]:
    """把 Clash / sing-box 的一个 proxy 条目转成 SSNode；非 ss 类型返回 None。"""
    if isinstance(entry, str):
        try:
            return parse_ss_uri(entry)
        except ShadowsocksError:
            return None
    if not isinstance(entry, dict):
        return None

    kind = str(entry.get("type") or entry.get("protocol") or "").strip().lower()
    if kind not in ("ss", "shadowsocks"):
        return None

    server = str(entry.get("server") or entry.get("address") or "").strip()
    port = entry.get("port") or entry.get("server_port") or entry.get("server-port")
    method = str(entry.get("cipher") or entry.get("method") or "").strip().lower()
    password = entry.get("password")
    name = str(entry.get("name") or entry.get("tag") or entry.get("remarks") or "").strip()

    try:
        port_int = int(port)
    except (TypeError, ValueError):
        return None
    if not server or not method or password is None:
        return None
    if not is_supported_method(method):
        logger.debug(f"跳过不支持的加密方法: {method}（节点 {name or index}）")
        return None
    return SSNode(server, port_int, method, str(password), name or f"node{index}")


def _nodes_from_data(data: Any) -> List[SSNode]:
    """从已解析的 JSON/YAML 结构里收集所有可用的 ss 节点。"""
    entries: List[Any] = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for key in ("proxies", "outbounds", "Proxy", "proxy"):
            value = data.get(key)
            if isinstance(value, list):
                entries.extend(value)
        if not entries:
            entries = [data]  # 单个节点对象
    nodes: List[SSNode] = []
    for index, entry in enumerate(entries, start=1):
        node = _entry_to_node(entry, index)
        if node is not None:
            nodes.append(node)
    return nodes


def _strip_yaml_comment(line: str) -> str:
    quote = ""
    for position, char in enumerate(line):
        if char in "\"'":
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
        elif char == "#" and not quote and (position == 0 or line[position - 1] in " \t"):
            return line[:position]
    return line


def _yaml_scalar(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("", "~", "null"):
        return ""
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    """极简 YAML 兜底解析：只认 ``proxies:`` / ``outbounds:`` 下的块状列表。

    装了 PyYAML 时不会走到这里；没装也能读 clash 那种两层的 proxies 列表
    （``- name: x`` 换行 ``type: ss`` …… 或单行 ``- {..}`` 流式写法）。
    """
    result: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_item: Optional[Dict[str, Any]] = None

    def flush() -> None:
        nonlocal current_item
        if current_item is not None and current_key:
            result.setdefault(current_key, []).append(current_item)
        current_item = None

    for raw_line in text.splitlines():
        line = _strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if indent == 0 and stripped.endswith(":"):
            flush()
            current_key = stripped[:-1].strip()
            result.setdefault(current_key, [])
            continue
        if current_key is None:
            continue
        if indent == 0:
            flush()
            current_key = None
            continue
        if stripped.startswith("- "):
            flush()
            body = stripped[2:].strip()
            current_item = {}
            if body.startswith("{"):
                try:
                    parsed = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    parsed = {}
                if isinstance(parsed, dict):
                    current_item.update(parsed)
                continue
            if body:
                key, _, value = body.partition(":")
                current_item[key.strip()] = _yaml_scalar(value)
            continue
        if current_item is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            current_item[key.strip()] = _yaml_scalar(value)
    flush()
    return result


def _load_structured_config(text: str) -> Any:
    """JSON 优先；否则按 YAML 处理（PyYAML 可用就用它）。"""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ShadowsocksError(f"代理配置 JSON 解析失败: {exc}") from exc
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if data is not None:
            return data
    except ImportError:
        pass
    except Exception:
        pass
    return _parse_simple_yaml(text)


def _looks_like_base64(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return 8 <= len(compact) <= 8192 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact) is not None


def load_proxy_nodes(text: str) -> List[SSNode]:
    """把任意受支持的代理配置解析成 ss 节点列表。

    支持的写法：单条 ``ss://``、多行 ``ss://``、``host:port:method:password``、
    Clash JSON（``{"proxies": [...]}``）、Clash YAML（``proxies:`` 块）、
    sing-box JSON（``{"outbounds": [...]}``）、以及 base64 订阅内容。
    """
    raw = (text or "").strip()
    if not raw:
        raise ShadowsocksError("代理配置为空")

    if raw.lower().startswith("ss://") and "\n" not in raw:
        return [parse_ss_uri(raw)]

    stripped = raw.lstrip()
    structured = (
        stripped.startswith(("{", "["))
        or re.search(r"^\s*(proxies|outbounds|Proxy)\s*:", raw, re.M) is not None
    )
    if structured:
        nodes = _nodes_from_data(_load_structured_config(raw))
        if nodes:
            return nodes
        raise ShadowsocksError("配置里没有可用的 shadowsocks 节点（只支持 ss / shadowsocks 类型）")

    if "ss://" in raw:
        nodes = [
            parse_ss_uri(line.strip())
            for line in raw.splitlines()
            if line.strip().lower().startswith("ss://")
        ]
        if nodes:
            return nodes

    if _looks_like_base64(raw):
        decoded = _b64decode_loose(raw)
        if "ss://" in decoded or decoded.lstrip().startswith(("{", "[")):
            return load_proxy_nodes(decoded)

    return [parse_ss_uri(raw)]


def select_proxy_node(nodes: List[SSNode], select: str = "") -> SSNode:
    """按名字子串挑节点；没指定就取第一个。"""
    if not nodes:
        raise ShadowsocksError("代理配置里没有可用节点")
    key = (select or "").strip().lower()
    if not key:
        return nodes[0]
    for node in nodes:
        if key in (node.tag or "").lower():
            return node
    available = ", ".join(node.tag or node.server for node in nodes[:8])
    raise ShadowsocksError(f"没找到名字包含「{select}」的节点；当前配置里的节点: {available}")


def load_proxy_nodes_cached(text: str) -> List[SSNode]:
    key = text.strip()
    cached = _nodes_cache.get(key)
    if cached is None:
        cached = load_proxy_nodes(key)
        _nodes_cache[key] = cached
    return cached


def evp_bytes_to_key(password: str, key_len: int) -> bytes:
    """Shadowsocks 的密钥派生（OpenSSL EVP_BytesToKey，MD5 迭代、无盐）。"""
    seed = password.encode("utf-8")
    material = b""
    previous = b""
    while len(material) < key_len:
        previous = hashlib.md5(previous + seed).digest()
        material += previous
    return material[:key_len]


# ----------------------------------------------------------------------
# Shadowsocks AEAD 隧道
# ----------------------------------------------------------------------


def encode_address(host: str, port: int) -> bytes:
    """SOCKS 地址格式：ATYP + ADDR + PORT。"""
    try:
        socket_addr = host.encode("idna")
    except UnicodeError:
        socket_addr = host.encode("utf-8")
    if _looks_like_ipv4(host):
        return b"\x01" + bytes(int(part) for part in host.split(".")) + struct.pack(">H", port)
    if ":" in host and _looks_like_ipv6(host):
        return b"\x04" + _pack_ipv6(host) + struct.pack(">H", port)
    if len(socket_addr) > 255:
        raise ShadowsocksError("域名过长")
    return b"\x03" + bytes([len(socket_addr)]) + socket_addr + struct.pack(">H", port)


def _looks_like_ipv4(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4:
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _looks_like_ipv6(host: str) -> bool:
    try:
        import ipaddress

        ipaddress.IPv6Address(host)
        return True
    except ValueError:
        return False


def _pack_ipv6(host: str) -> bytes:
    import ipaddress

    return ipaddress.IPv6Address(host).packed


class ShadowsocksTunnel:
    """一条到目标的加密 TCP 流（TCP over AEAD，客户端方向）。"""

    def __init__(self, node: SSNode, timeout: float = CONNECT_TIMEOUT) -> None:
        self.node = node
        self.timeout = timeout
        self.key_len = key_length(node.method)
        self._master_key = evp_bytes_to_key(node.password, self.key_len)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._enc: Optional[AeadCipher] = None
        self._dec: Optional[AeadCipher] = None
        self._plain_buffer = bytearray()

    async def open(self, host: str, port: int) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.node.server, self.node.port),
                timeout=self.timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ShadowsocksError(
                f"连接 SS 服务器 {self.node.masked} 失败: {exc}"
            ) from exc
        salt = os.urandom(self.key_len)
        self._enc = AeadCipher(
            self.node.method, hkdf_sha1(self._master_key, salt, length=self.key_len)
        )
        self._writer.write(salt)
        self._write_chunk(encode_address(host, port))
        assert self._writer is not None
        await self._writer.drain()

    def _write_chunk(self, payload: bytes) -> None:
        assert self._enc is not None and self._writer is not None
        for start in range(0, len(payload), MAX_CHUNK):
            chunk = payload[start : start + MAX_CHUNK]
            self._writer.write(self._enc.encrypt(struct.pack(">H", len(chunk))))
            self._writer.write(self._enc.encrypt(chunk))

    async def _ensure_decryptor(self) -> None:
        if self._dec is not None:
            return
        assert self._reader is not None
        salt = await self._reader.readexactly(self.key_len)
        self._dec = AeadCipher(
            self.node.method, hkdf_sha1(self._master_key, salt, length=self.key_len)
        )

    async def read(self, size: int = RELAY_BUFFER) -> bytes:
        """读出最多 size 字节明文；EOF 返回 b""。"""
        if self._plain_buffer:
            data = bytes(self._plain_buffer[:size])
            del self._plain_buffer[:size]
            return data
        await self._ensure_decryptor()
        assert self._reader is not None and self._dec is not None
        try:
            length_ct = await self._reader.readexactly(2 + TAG_LEN)
        except asyncio.IncompleteReadError:
            return b""
        length = struct.unpack(">H", self._dec.decrypt(length_ct))[0]
        try:
            payload_ct = await self._reader.readexactly(length + TAG_LEN)
        except asyncio.IncompleteReadError:
            return b""
        payload = self._dec.decrypt(payload_ct)
        self._plain_buffer += payload
        data = bytes(self._plain_buffer[:size])
        del self._plain_buffer[:size]
        return data

    async def write(self, data: bytes) -> None:
        if not data:
            return
        self._write_chunk(data)
        assert self._writer is not None
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # pragma: no cover
                pass
        self._reader = None
        self._writer = None


# ----------------------------------------------------------------------
# 本地混合代理（SOCKS5 + HTTP CONNECT）
# ----------------------------------------------------------------------


class MixedProxyServer:
    """一个监听口同时说 SOCKS5 与 HTTP CONNECT，出口走 SS 隧道。"""

    def __init__(
        self,
        node: SSNode,
        host: str = "127.0.0.1",
        port: int = 0,
        timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self.node = node
        self.host = host
        self.port = port
        self.timeout = timeout
        self._server: Optional[asyncio.AbstractServer] = None
        self._connections = 0

    @property
    def socks_url(self) -> str:
        return f"socks5h://{self.host}:{self.port}"

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        for sock in self._server.sockets or []:
            self.port = sock.getsockname()[1]
            break
        logger.info(f"内置 Shadowsocks 代理已启动: {self.socks_url} → {self.node.masked}")
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connections += 1
        tunnel: Optional[ShadowsocksTunnel] = None
        try:
            first = await reader.readexactly(1)
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        try:
            if first == b"\x05":
                tunnel = await self._serve_socks5(reader, writer)
            else:
                tunnel = await self._serve_http(reader, writer, first)
            if tunnel is not None:
                await self._relay(reader, writer, tunnel)
        except Exception as exc:  # 代理层失败不该拖垮引擎
            logger.debug(f"代理连接处理失败: {exc}")
        finally:
            if tunnel is not None:
                await tunnel.close()
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass

    # -- SOCKS5 -------------------------------------------------------

    async def _serve_socks5(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> Optional[ShadowsocksTunnel]:
        methods_len = (await reader.readexactly(1))[0]
        await reader.readexactly(methods_len)
        writer.write(b"\x05\x00")  # 无需认证
        await writer.drain()

        header = await reader.readexactly(4)
        command = header[1]
        atyp = header[3]
        host, port = await self._read_socks_address(reader, atyp)
        if command != 0x01:  # 只做 CONNECT
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return None

        tunnel = await self._open_tunnel(host, port)
        if tunnel is None:
            writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return None
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        return tunnel

    async def _read_socks_address(
        self, reader: asyncio.StreamReader, atyp: int
    ) -> Tuple[str, int]:
        if atyp == 0x01:
            host = ".".join(str(b) for b in await reader.readexactly(4))
        elif atyp == 0x04:
            import ipaddress

            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        elif atyp == 0x03:
            length = (await reader.readexactly(1))[0]
            raw_host = await reader.readexactly(length)
            try:
                host = raw_host.decode("ascii")
            except UnicodeDecodeError:  # 极少数客户端会塞 UTF-8 域名
                host = raw_host.decode("utf-8", errors="replace")
        else:
            raise ShadowsocksError(f"不支持的 SOCKS5 地址类型: {atyp}")
        port = struct.unpack(">H", await reader.readexactly(2))[0]
        return host, port

    # -- HTTP ---------------------------------------------------------

    async def _serve_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        first_byte: bytes,
    ) -> Optional[ShadowsocksTunnel]:
        raw = first_byte + await reader.readuntil(b"\r\n\r\n")
        header_text = raw.decode("iso-8859-1", errors="replace")
        lines = header_text.split("\r\n")
        if not lines or not lines[0]:
            return None
        parts = lines[0].split(" ")
        if len(parts) < 3:
            return None
        method, target, version = parts[0], parts[1], parts[2]

        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            port = int(port_text or 443)
            tunnel = await self._open_tunnel(host.strip("[]"), port)
            if tunnel is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
                return None
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            return tunnel

        # 绝对 URI（http://host/path）→ 改写成 origin-form 走隧道
        if "://" not in target:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return None
        parsed = urlsplit(target)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        tunnel = await self._open_tunnel(host, port)
        if tunnel is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return None
        kept = [
            line
            for line in lines[1:]
            if line and not line.lower().startswith(("proxy-connection", "connection"))
        ]
        rebuilt = f"{method} {path} {version}\r\n" + "\r\n".join(kept) + "\r\n\r\n"
        await tunnel.write(rebuilt.encode("iso-8859-1", errors="replace"))
        return tunnel

    async def _open_tunnel(self, host: str, port: int) -> Optional[ShadowsocksTunnel]:
        if not host or not port:
            return None
        tunnel = ShadowsocksTunnel(self.node, timeout=self.timeout)
        try:
            await tunnel.open(host, port)
        except Exception as exc:
            logger.debug(f"隧道连接 {host}:{port} 失败: {exc}")
            await tunnel.close()
            return None
        return tunnel

    # -- 双向转发 -----------------------------------------------------

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        tunnel: ShadowsocksTunnel,
    ) -> None:
        async def client_to_tunnel() -> None:
            while True:
                data = await reader.read(RELAY_BUFFER)
                if not data:
                    break
                await tunnel.write(data)

        async def tunnel_to_client() -> None:
            while True:
                data = await tunnel.read(RELAY_BUFFER)
                if not data:
                    break
                writer.write(data)
                await writer.drain()

        tasks: List[asyncio.Task] = [
            asyncio.ensure_future(client_to_tunnel()),
            asyncio.ensure_future(tunnel_to_client()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                try:
                    task.result()
                except Exception:
                    pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()


class ShadowsocksProxy:
    """进程内单例友好的 SS 代理门面：懒启动、按事件循环自动重建。"""

    def __init__(
        self,
        ss_uri: str,
        host: str = "127.0.0.1",
        timeout: float = CONNECT_TIMEOUT,
        select: str = "",
    ) -> None:
        self.ss_uri = (ss_uri or "").strip()
        self.select = (select or "").strip()
        self.host = host
        self.timeout = timeout
        self.node: Optional[SSNode] = None
        self._server: Optional[MixedProxyServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._starting: Optional[asyncio.Future] = None

    @property
    def socks_url(self) -> str:
        return self._server.socks_url if self._server else ""

    @property
    def http_url(self) -> str:
        return self._server.http_url if self._server else ""

    @property
    def running(self) -> bool:
        return self._server is not None

    async def ensure_started(self) -> bool:
        """确保本地代理已监听（同一事件循环内只启动一次）。"""
        if self._server is not None and self._loop is asyncio.get_running_loop():
            return True
        if not self.ss_uri:
            return False
        if self._starting is not None and not self._starting.done():
            await self._starting
            return self._server is not None
        loop = asyncio.get_running_loop()
        self._starting = loop.create_future()
        try:
            nodes = load_proxy_nodes_cached(self.ss_uri)
            self.node = select_proxy_node(nodes, self.select)
            if len(nodes) > 1:
                names = ", ".join(node.tag or node.server for node in nodes[:8])
                logger.info(f"代理配置含 {len(nodes)} 个 ss 节点，已选用「{self.node.tag}」；可选: {names}")
            server = MixedProxyServer(self.node, host=self.host, timeout=self.timeout)
            await server.start()
            self._server = server
            self._loop = loop
            return True
        except Exception as exc:
            logger.warning(f"内置 Shadowsocks 代理启动失败（将退回直连/系统代理）: {exc}")
            self._server = None
            self.node = None
            return False
        finally:
            if self._starting is not None:
                self._starting.set_result(True)
                self._starting = None

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            logger.info("内置 Shadowsocks 代理已停止")
        self._server = None
        self._loop = None


_nodes_cache: Dict[str, List[SSNode]] = {}


def parse_ss_node_cached(ss_uri: str, select: str = "") -> SSNode:
    """兼容旧接口：解析配置并挑出唯一节点。"""
    return select_proxy_node(load_proxy_nodes_cached(ss_uri), select)


def _cli() -> int:  # pragma: no cover - 手测入口
    import argparse

    parser = argparse.ArgumentParser(description="插件内置 Shadowsocks 本地代理")
    parser.add_argument("--uri", required=True, help="ss:// 链接，或 Clash/sing-box 配置文件路径")
    parser.add_argument("--node", default="", help="多节点时按名字子串挑选")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1080)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    source = args.uri
    if os.path.isfile(source):
        with open(source, "r", encoding="utf-8") as handle:
            source = handle.read()
    nodes = load_proxy_nodes(source)
    node = select_proxy_node(nodes, args.node)
    logger.info(f"命中节点: {node.tag} → {node.masked}")

    async def main() -> None:
        server = MixedProxyServer(node, host=args.host, port=args.port)
        await server.start()
        print(f"SOCKS5: {server.socks_url}\nHTTP:   {server.http_url}")
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = [
    "MixedProxyServer",
    "SSNode",
    "ShadowsocksError",
    "ShadowsocksProxy",
    "ShadowsocksTunnel",
    "encode_address",
    "evp_bytes_to_key",
    "load_proxy_nodes",
    "parse_ss_uri",
    "select_proxy_node",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())

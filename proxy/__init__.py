"""内置代理：Shadowsocks / Trojan → 本地 SOCKS5 + HTTP CONNECT。"""

from .shadowsocks import (
    MixedProxyServer,
    ProxyNode,
    ProxyService,
    ShadowsocksError,
    ShadowsocksTunnel,
    load_proxy_nodes,
    select_proxy_node,
)
from .trojan import TrojanTunnel

__all__ = [
    "ProxyNode",
    "ProxyService",
    "MixedProxyServer",
    "ShadowsocksTunnel",
    "TrojanTunnel",
    "ShadowsocksError",
    "load_proxy_nodes",
    "select_proxy_node",
]

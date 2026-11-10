"""内置网络代理组件：Shadowsocks AEAD 隧道 + 本地混合代理口（SOCKS5 / HTTP CONNECT）。

供 Ascii2D 这类被 Cloudflare 按 ASN 风控的站点使用，插件不再依赖外部
clash / v2ray 之类的客户端。
"""

from .shadowsocks import (
    MixedProxyServer,
    ShadowsocksError,
    ShadowsocksProxy,
    ShadowsocksTunnel,
    SSNode,
    encode_address,
    evp_bytes_to_key,
    load_proxy_nodes,
    parse_ss_uri,
    select_proxy_node,
)

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

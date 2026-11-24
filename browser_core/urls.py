"""URL validation for the generic browser tools.

The tools must not be talked into opening the panel, loopback services or
local files by a page they already loaded. `allow_local_urls` is the only way
to opt in.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit


class UrlRejected(ValueError):
    pass


def normalize_url(raw: str, *, allow_local: bool = False) -> str:
    url = (raw or "").strip()
    if not url:
        raise UrlRejected("url is empty")
    if url.startswith("//"):
        url = "https:" + url
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        if not (allow_local and scheme == "file"):
            raise UrlRejected(f"unsupported scheme: {scheme or '(none)'}")
    if scheme in ("http", "https"):
        host = (parts.hostname or "").lower()
        if not host:
            raise UrlRejected("url has no host")
        if not allow_local and _is_local_host(host):
            raise UrlRejected("local addresses are disabled (allow_local_urls)")
    return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _is_local_host(host: str) -> bool:
    if host in ("localhost", "localhost.localdomain", "0.0.0.0", "::1"):
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local

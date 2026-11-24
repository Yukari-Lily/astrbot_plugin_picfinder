"""浏览器反检测：让 Playwright 的 Chromium 更像普通桌面 Chrome。

Google 的「异常流量」页并不只看 IP，它给浏览器打分：UA 里带 ``HeadlessChrome``、
``navigator.webdriver`` 为 true、缺少 ``window.chrome``、语言/时区与出口地区不符，
都会直接把人推到人机验证。这里把这些特征补成正常值，并在上下文里预置 Google
的同意 Cookie（SOCS），避免结果页被同意弹窗挡住。

真正的决定性因素仍然是出口 IP：机房 IP 上叠满这些也只是「不那么可疑」，
住宅/家宽出口 + 已登录 Cookie 才稳。模块只负责把能控的部分做到位。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_UA_VERSION = "141.0.7390.37"

# 让 Chromium 不暴露「自动化」开关，减少指纹差异
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,OptimizationHints,MediaRouter",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--metrics-recording-only",
]

# 在页面脚本之前注入，抹平最常见的自动化特征
STEALTH_INIT_JS = r"""
(() => {
  const define = (obj, prop, getter) => {
    try {
      Object.defineProperty(obj, prop, { get: getter, configurable: true });
    } catch (e) {}
  };

  define(navigator, 'webdriver', () => undefined);
  define(navigator, 'language', () => 'zh-CN');
  define(navigator, 'languages', () => ['zh-CN', 'zh', 'en']);
  define(navigator, 'platform', () => 'Win32');
  define(navigator, 'vendor', () => 'Google Inc.');
  define(navigator, 'hardwareConcurrency', () => 8);
  define(navigator, 'deviceMemory', () => 8);
  define(navigator, 'maxTouchPoints', () => 0);

  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', { value: {}, writable: true, configurable: true });
  }
  window.chrome.runtime = window.chrome.runtime || {};
  window.chrome.app = window.chrome.app || { isInstalled: false };
  window.chrome.csi = window.chrome.csi || function () {
    return { onloadT: Date.now(), startE: Date.now(), pageT: 1000, tran: 15 };
  };
  window.chrome.loadTimes = window.chrome.loadTimes || function () {
    return {
      requestTime: Date.now() / 1000,
      startLoadTime: Date.now() / 1000,
      commitLoadTime: Date.now() / 1000,
      finishDocumentLoadTime: Date.now() / 1000,
      finishLoadTime: Date.now() / 1000,
      firstPaintTime: Date.now() / 1000,
      firstPaintAfterLoadTime: 0,
      navigationType: 'Other',
      wasFetchedViaSpdy: true,
      wasNpnNegotiated: true,
      npnNegotiatedProtocol: 'h2',
      wasAlternateProtocolAvailable: false,
      connectionInfo: 'h2',
    };
  };

  const fakePlugin = (name, filename, description) => ({
    name, filename, description,
    length: 1,
    item: () => null,
    namedItem: () => null,
  });
  const plugins = [
    fakePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    fakePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    fakePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
  ];
  define(navigator, 'plugins', () => plugins);
  define(navigator, 'mimeTypes', () => [{ type: 'application/pdf' }]);

  if (window.navigator.permissions && window.navigator.permissions.query) {
    const original = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (params) => {
      if (params && params.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null });
      }
      return original(params);
    };
  }

  const patchWebGL = (proto) => {
    if (!proto || !proto.getParameter) return;
    const original = proto.getParameter;
    proto.getParameter = function (param) {
      if (param === 37445) return 'Google Inc. (Intel)';
      if (param === 37446) {
        return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      }
      return original.call(this, param);
    };
  };
  try {
    patchWebGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
    patchWebGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);
  } catch (e) {}
})();
"""

# 预置 Google 同意 Cookie：省掉整页的「接受全部」弹窗
GOOGLE_CONSENT_COOKIES: List[Dict[str, Any]] = [
    {
        "name": "SOCS",
        "value": "CAI",
        "domain": ".google.com",
        "path": "/",
        "secure": True,
        "sameSite": "None",
    },
    {
        "name": "CONSENT",
        "value": "YES+",
        "domain": ".google.com",
        "path": "/",
        "secure": True,
        "sameSite": "None",
    },
]


def chrome_version(raw: str = "") -> str:
    """把 ``Chromium 141.0.7390.37`` / ``141.0.7390.37`` 统一成版本号。"""
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", raw or "")
    if match:
        return match.group(1)
    match = re.search(r"(\d+\.\d+\.\d+)", raw or "")
    if match:
        return match.group(1)
    return ""


def chrome_user_agent(version: str = "") -> str:
    full = chrome_version(version) or DEFAULT_UA_VERSION
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{full} Safari/537.36"
    )


def is_headless_user_agent(user_agent: str) -> bool:
    return "headless" in (user_agent or "").lower()


def _norm_same_site(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}.get(text) or None


def _cookie_record(raw: Dict[str, Any], default_domain: str = "") -> Optional[Dict[str, Any]]:
    name = str(raw.get("name") or raw.get("Name") or "").strip()
    value = raw.get("value", raw.get("Value", ""))
    if not name:
        return None
    domain = str(raw.get("domain") or raw.get("Domain") or raw.get("host") or default_domain or "").strip()
    if not domain:
        return None
    record: Dict[str, Any] = {
        "name": name,
        "value": str(value if value is not None else ""),
        "domain": domain,
        "path": str(raw.get("path") or raw.get("Path") or "/") or "/",
    }
    expires = raw.get("expires", raw.get("Expires", raw.get("expirationDate")))
    try:
        if expires is not None and float(expires) > 0:
            record["expires"] = int(float(expires))
    except (TypeError, ValueError):
        pass
    if raw.get("secure") is not None or raw.get("Secure") is not None:
        record["secure"] = bool(raw.get("secure", raw.get("Secure")))
    if raw.get("httpOnly") is not None or raw.get("HttpOnly") is not None:
        record["httpOnly"] = bool(raw.get("httpOnly", raw.get("HttpOnly")))
    same_site = _norm_same_site(raw.get("sameSite") or raw.get("SameSite"))
    if same_site:
        record["sameSite"] = same_site
    return record


def parse_netscape_cookies(text: str) -> List[Dict[str, Any]]:
    """解析浏览器导出的 cookies.txt（Netscape 格式）。"""
    records: List[Dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            # HttpOnly 行以 "#HttpOnly_" 开头，仍需解析
            if line.lower().startswith("#httponly_"):
                line = line[len("#HttpOnly_") :].lstrip()
            else:
                continue
        fields = line.split("\t")
        if len(fields) < 7:
            fields = re.split(r"\s+", line, maxsplit=6)
        if len(fields) < 7:
            continue
        domain, _include, path, secure, expires, name, value = fields[:7]
        record = _cookie_record(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "secure": str(secure).upper() == "TRUE",
                "expires": expires,
            }
        )
        if record:
            records.append(record)
    return records


def parse_cookie_json(text: str) -> List[Dict[str, Any]]:
    """解析 JSON 形式的 Cookie：Playwright 数组、Chrome 导出或 {name: value}。"""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        else:
            # {"SID": "...", "HSID": "..."} 这种最简形式，默认 google.com
            return [
                record
                for key, value in data.items()
                if (record := _cookie_record({"name": key, "value": value}, ".google.com"))
            ]
    if not isinstance(data, list):
        return []
    records: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        record = _cookie_record(item)
        if record:
            records.append(record)
    return records


def load_cookie_file(path: str) -> List[Dict[str, Any]]:
    """读取本地 Cookie 文件（.json 或 Netscape .txt），失败时返回空列表。"""
    if not path:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records = parse_cookie_json(text) if text.lstrip().startswith(("{", "[")) else parse_netscape_cookies(text)
    return records


__all__ = [
    "DEFAULT_UA_VERSION",
    "GOOGLE_CONSENT_COOKIES",
    "STEALTH_ARGS",
    "STEALTH_INIT_JS",
    "chrome_user_agent",
    "chrome_version",
    "is_headless_user_agent",
    "load_cookie_file",
    "parse_cookie_json",
    "parse_netscape_cookies",
]

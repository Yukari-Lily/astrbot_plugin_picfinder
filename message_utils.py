"""消息解析与图片提取工具。
支持从当前消息、引用回复 (get_msg)、CQ 码及各类适配器中稳健提取图片 URL 或本地临时文件。
"""

import base64
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import astrbot.api.message_components as Comp
    from astrbot.api import logger
    from astrbot.api.event import filter as event_filter
except ImportError:
    class Comp:
        class Image:
            pass

        class Reply:
            pass

    import logging
    logger = logging.getLogger("astrbot_plugin_picfinder.message_utils")

    class event_filter:
        class PlatformAdapterType:
            AIOCQHTTP = "aiocqhttp"

IMG_CQ_RE = re.compile(r"\[CQ:image,(?P<kv>[^\]]+)\]")
KV_RE = re.compile(r"([^,=]+)=([^,]+)")

_ACCEPTED_HTTP_PREFIXES = ("http://", "https://")


def _segments(event: Any) -> List[Any]:
    try:
        return list(event.get_messages() or [])
    except Exception:
        return []


def _extract_url_from_values(*values: Any) -> Optional[str]:
    for val in values:
        if isinstance(val, str) and val.startswith(_ACCEPTED_HTTP_PREFIXES):
            return val.replace("&amp;", "&")
    return None


def _first_image_from_cq_string(cq_text: str) -> Optional[str]:
    """从 CQ 码字符串中提取首张图片的可用 HTTP 地址"""
    for match in IMG_CQ_RE.finditer(cq_text):
        values = dict(KV_RE.findall(match.group("kv")))
        found = _extract_url_from_values(values.get("url"), values.get("file"))
        if found:
            return found
    return None


def _own_platform(context: Any, event: Any) -> Any:
    """获取当前事件所属适配器实例的平台 (解决多 bot 共存时 get_msg 错调问题)"""
    getter = getattr(context, "get_platform_inst", None)
    platform_id = getattr(event, "get_platform_id", None)
    if getter is not None and platform_id is not None:
        try:
            platform = getter(platform_id())
            if platform is not None:
                return platform
        except Exception:
            pass
    try:
        return context.get_platform(event_filter.PlatformAdapterType.AIOCQHTTP)
    except Exception:
        return None


async def _image_from_get_msg(context: Any, event: Any, reply_id: Any) -> Optional[str]:
    """通过 OneBot get_msg 查询被引用消息里的首张图片"""
    if getattr(event, "get_platform_name", lambda: "")() != "aiocqhttp":
        return None
    try:
        platform = _own_platform(context, event)
        if not platform:
            return None
        bot = platform.get_client()
    except Exception as exc:
        logger.debug(f"picfinder: 获取 aiocqhttp client 失败: {exc}")
        return None

    candidates = [reply_id]
    try:
        numeric_id = int(reply_id)
        if numeric_id != reply_id:
            candidates.append(numeric_id)
    except (TypeError, ValueError):
        pass

    for candidate in candidates:
        try:
            resp = await bot.api.call_action("get_msg", message_id=candidate)
        except Exception:
            continue
        if not isinstance(resp, dict):
            continue

        message = resp.get("message")
        if isinstance(message, list):
            for part in message:
                if not isinstance(part, dict) or part.get("type") != "image":
                    continue
                data = part.get("data") or {}
                if isinstance(data, dict):
                    found = _extract_url_from_values(data.get("url"), data.get("file"))
                    if found:
                        return found
        elif isinstance(message, str):
            found = _first_image_from_cq_string(message)
            if found:
                return found

        raw = resp.get("raw_message")
        if isinstance(raw, str):
            found = _first_image_from_cq_string(raw)
            if found:
                return found

    return None


async def resolve_event_image(context: Any, event: Any) -> Tuple[Optional[str], Optional[str]]:
    """提取图片。返回 (image_url, local_temp_path)。至少有一个非空，若都没找到则返回 (None, None)。"""
    segments = _segments(event)

    # 1. 检查当前消息中的图片段
    for seg in segments:
        if isinstance(seg, Comp.Image):
            url = getattr(seg, "url", None)
            file_val = getattr(seg, "file", None) or getattr(seg, "path", None)
            direct_url = _extract_url_from_values(url, file_val)
            if direct_url:
                return direct_url, None
            if file_val and isinstance(file_val, str) and os.path.exists(file_val):
                return None, file_val

            # 尝试通过 convert_to_base64 提取
            converter = getattr(seg, "convert_to_base64", None)
            if callable(converter):
                try:
                    b64 = await converter()
                    if b64 and isinstance(b64, str):
                        fd, path = tempfile.mkstemp(suffix=".jpg", prefix="picfinder_b64_")
                        with os.fdopen(fd, "wb") as f:
                            f.write(base64.b64decode(b64))
                        return None, path
                except Exception as exc:
                    logger.debug(f"picfinder: convert_to_base64 失败: {exc}")

        # 字典结构兼容
        if isinstance(seg, dict) and seg.get("type") == "image":
            data = seg.get("data") or {}
            found = _extract_url_from_values(data.get("url"), data.get("file"))
            if found:
                return found, None

    # 2. 检查引用回复 (Reply)
    reply = next((seg for seg in segments if isinstance(seg, Comp.Reply)), None)
    if reply is not None:
        # 检查 reply 自身携带的 chain
        for seg in getattr(reply, "chain", None) or []:
            if isinstance(seg, Comp.Image):
                url = _extract_url_from_values(getattr(seg, "url", None), getattr(seg, "file", None))
                if url:
                    return url, None

        # 检查 reply.id 并通过 get_msg 深度获取
        reply_id = getattr(reply, "id", None)
        if reply_id not in (None, "", 0):
            found_url = await _image_from_get_msg(context, event, reply_id)
            if found_url:
                return found_url, None

    # 兼容 message_obj.reply
    msg_obj = getattr(event, "message_obj", None)
    if msg_obj and getattr(msg_obj, "reply", None):
        reply_obj = msg_obj.reply
        reply_msg = getattr(reply_obj, "message", None)
        if isinstance(reply_msg, list):
            for seg in reply_msg:
                if isinstance(seg, Comp.Image):
                    url = _extract_url_from_values(getattr(seg, "url", None), getattr(seg, "file", None))
                    if url:
                        return url, None
                elif isinstance(seg, dict) and seg.get("type") == "image":
                    data = seg.get("data") or {}
                    url = _extract_url_from_values(data.get("url"), data.get("file"))
                    if url:
                        return url, None

    # 3. 检查文本或 raw_message 中的 CQ:image
    raw_text = getattr(event, "message_str", "") or ""
    cq_url = _first_image_from_cq_string(raw_text)
    if cq_url:
        return cq_url, None

    raw_msg = getattr(getattr(event, "message_obj", None), "raw_message", "") or ""
    if raw_msg and raw_msg != raw_text:
        cq_url = _first_image_from_cq_string(raw_msg)
        if cq_url:
            return cq_url, None

    return None, None

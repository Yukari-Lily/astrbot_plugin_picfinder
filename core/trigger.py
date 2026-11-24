"""搜图触发判定。

只认三种情况（和用户约定的一致）：

1. 同一条消息里既有触发词、又带图片（触发词在消息任何位置都行）；
2. 回复某张图片，并发送触发词；
3. 整条消息只有触发词（没别的字、也没图）→ 进入连续搜图模式，之后再发图。

唤醒词、单纯 @ 机器人、以及别的插件把「llr」之类标成 @ 的 ``is_at_or_wake_command``
都不算触发。
"""

from __future__ import annotations

from typing import Iterable, Sequence

# 「只有触发词」判定时允许忽略的首尾字符
_EDGE_CHARS = " \t\r\n/。．.!！?？~～,，、:：;；\"'“”‘’"


def keyword_hit(text: str, keywords: Sequence[str]) -> bool:
    """消息里出现任意一个触发词（位置不限）。"""
    body = text or ""
    return any(keyword and keyword in body for keyword in keywords)


def is_bare_keyword(text: str, keywords: Sequence[str]) -> bool:
    """整条消息只有触发词本身（允许前后有标点/斜杠）。"""
    body = (text or "").strip(_EDGE_CHARS)
    return bool(body) and body in set(keywords)


def should_search(*, keyword: bool, has_image: bool) -> bool:
    """有图 + 有触发词才搜；没有图交给连续搜图模式。"""
    return bool(keyword and has_image)


__all__ = ["is_bare_keyword", "keyword_hit", "should_search"]

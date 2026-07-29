"""日志与异常里的凭据脱敏。

dataclass 的默认 repr 会把 ``Authorization: Bearer ...`` 原样打进 traceback，
所有携带凭据的字段必须 ``repr=False``，所有对外可见的字符串走这里过一遍。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

__all__ = ["redact", "redact_headers", "redact_url"]

_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"})
_SENSITIVE_QUERY = re.compile(r"([?&](?:token|api_?key|access_token|auth|password|secret)=)([^&#]+)", re.I)
_BEARER = re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]{8,})", re.I)


def _mask(value: str) -> str:
    """保留头尾各 3 个字符，够人肉比对，不够复用。"""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}…{value[-3:]}"


def redact(text: str) -> str:
    """对任意字符串做尽力脱敏（Bearer token + URL query 里的凭据）。

    :param text: 任意文本，通常是即将进日志或异常消息的字符串
    :returns: 敏感片段替换成 ``头3…尾3``（长度 ≤ 8 的整个换成 ``***``）
    """
    text = _BEARER.sub(lambda m: m.group(1) + _mask(m.group(2)), text)
    return _SENSITIVE_QUERY.sub(lambda m: m.group(1) + _mask(m.group(2)), text)


def redact_url(url: str) -> str:
    """URL 专用：去掉 userinfo，遮蔽 query 里的凭据。

    异常消息里不出现完整 WS URL —— 它可能整条就是凭据。

    :param url: 完整 URL
    :returns: userinfo 段换成 ``***``，query 里的 token/api_key 等遮蔽掉
    """
    url = re.sub(r"://([^/@]+)@", "://***@", url)
    return _SENSITIVE_QUERY.sub(lambda m: m.group(1) + _mask(m.group(2)), url)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """遮蔽请求头里的凭据字段，其余原样返回。

    :param headers: 原始请求头
    :returns: 新的 dict；``authorization`` / ``cookie`` / ``x-api-key`` 等已遮蔽
    """
    return {
        k: (_mask(v) if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }

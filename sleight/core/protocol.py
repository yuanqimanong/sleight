"""CDP 消息编解码。sans-io —— 这个模块不碰网络，可以纯单测。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import count
from typing import Any

from .errors import ProtocolError

__all__ = ["CDPError", "Event", "IdAllocator", "Response", "decode", "encode"]


@dataclass(frozen=True, slots=True)
class CDPError:
    code: int | None
    message: str
    data: str | None = None

    def __str__(self) -> str:
        return f"{self.message}{f' ({self.data})' if self.data else ''}"


@dataclass(frozen=True, slots=True)
class Response:
    id: int
    result: dict[str, Any]
    error: CDPError | None = None
    session_id: str | None = None

    def unwrap(self, method: str = "") -> dict[str, Any]:
        """成功则返回 result，失败则抛。

        error 不在 decode 里抛，是因为 flush() 需要知道**是哪条命令**失败了 ——
        无等待发送的场景下调用栈已经离开现场，只剩 id 能定位。
        """
        if self.error is not None:
            raise ProtocolError(str(self.error), code=self.error.code, method=method or None)
        return self.result


@dataclass(frozen=True, slots=True)
class Event:
    method: str
    params: dict[str, Any]
    session_id: str | None = None


class IdAllocator:
    """单调递增的消息 id。CDP 只要求同一连接内唯一。"""

    def __init__(self, start: int = 1) -> None:
        self._counter = count(start)

    def next(self) -> int:
        return next(self._counter)


def encode(
    msg_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> str:
    msg: dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    if session_id:
        msg["sessionId"] = session_id
    return json.dumps(msg, separators=(",", ":"))


def decode(raw: str | bytes) -> Response | Event:
    """把一条 CDP 消息解成 Response 或 Event。带 ``id`` 是响应，带 ``method`` 是事件。"""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"malformed CDP frame: {exc}") from exc

    if not isinstance(msg, dict):
        raise ProtocolError(f"unexpected CDP frame type: {type(msg).__name__}")

    session_id = msg.get("sessionId")

    if "id" in msg:
        err = msg.get("error")
        return Response(
            id=msg["id"],
            result=msg.get("result") or {},
            error=CDPError(err.get("code"), err.get("message", "unknown"), err.get("data"))
            if err
            else None,
            session_id=session_id,
        )

    if "method" in msg:
        return Event(method=msg["method"], params=msg.get("params") or {}, session_id=session_id)

    raise ProtocolError(f"CDP frame has neither id nor method: {sorted(msg)}")

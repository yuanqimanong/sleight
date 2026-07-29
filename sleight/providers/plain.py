"""裸 CDP 端点。

覆盖本地 ``chrome --remote-debugging-port=9222``、browserless 直连、以及任何把
``/json/version`` 摆出来的东西。

它**不继承** :class:`~sleight.providers.base.HTTPProvider` —— 没有管理 API，没有
生命周期，硬套那套骨架只会让基类长出分支。
"""

from __future__ import annotations

from ..core._http import HttpClient
from ..core.errors import ConnectionError, NotFound, NotReady
from ..core.types import Endpoint, InstanceInfo, InstanceStatus
from .base import BaseProvider

__all__ = ["Plain"]

DEFAULT_ID = "default"


class Plain(BaseProvider):
    def __init__(
        self,
        base_url: str,
        *,
        name: str = "plain",
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        """
        :param base_url: 浏览器的 HTTP 调试地址，如 ``http://127.0.0.1:9222``
        :param name: 池内唯一的 provider 名，也是 uid 前缀
        :param headers: 附加请求头。browserless 这类需要鉴权的后端在这里给
        :param timeout: HTTP 超时，秒
        """
        self.base_url = base_url.rstrip("/")
        self.name = name
        self._headers = dict(headers or {})
        self._http = HttpClient(self.base_url, headers=self._headers, timeout=timeout)

    def __repr__(self) -> str:
        return f"<Plain {self.name!r} {self.base_url}>"

    # ------------------------------------------------------------------ #

    def endpoint(self, instance_id: str | None = None) -> Endpoint:
        """从 ``/json/version`` 取浏览器级 WS 地址。

        :param instance_id: 只接受 ``None`` 或 ``"default"``
        :raises NotFound: 传了别的 id
        :raises ConnectionError: 端点不可达，或响应里没有 ``webSocketDebuggerUrl``
        """
        self._check_id(instance_id)
        return Endpoint(
            http_base=self.base_url,
            ws_url=self._browser_ws(),
            headers=self._headers,
        )

    def list_instances(self) -> list[InstanceInfo]:
        """固定一个实例，id 为 ``"default"``。"""
        return [
            InstanceInfo(
                id=DEFAULT_ID,
                provider=self.name,
                ready=self.status(DEFAULT_ID) is InstanceStatus.RUNNING,
                name=self.base_url,
            )
        ]

    def status(self, instance_id: str) -> InstanceStatus:
        """能连上 ``/json/version`` 就算 RUNNING，连不上算 STOPPED。

        :param instance_id: ``"default"`` 之外的一律 NOT_FOUND
        """
        if instance_id not in (DEFAULT_ID, None):
            return InstanceStatus.NOT_FOUND
        try:
            return (
                InstanceStatus.RUNNING
                if self._http.get("/json/version").ok
                else InstanceStatus.STOPPED
            )
        except ConnectionError:
            return InstanceStatus.STOPPED

    def ensure_ready(self, instance_id: str) -> None:
        """没有管理 API —— 只能确认可达，起不了浏览器。

        :param instance_id: 只接受 ``"default"``
        :raises NotFound: 传了别的 id
        :raises NotReady: 端点没有响应。自己去把浏览器拉起来
        """
        self._check_id(instance_id)
        if self.status(instance_id) is not InstanceStatus.RUNNING:
            raise NotReady(
                f"{self.name}: nothing answering at {self.base_url}/json/version; "
                "Plain cannot start a browser, launch it yourself"
            )

    # ------------------------------------------------------------------ #

    def _check_id(self, instance_id: str | None) -> None:
        if instance_id not in (None, DEFAULT_ID):
            raise NotFound(f"{self.name}: Plain has a single instance {DEFAULT_ID!r}")

    def _browser_ws(self) -> str:
        r = self._http.get("/json/version")
        if not r.ok or not isinstance(r.body, dict):
            raise ConnectionError(f"{self.base_url}/json/version returned {r.status}")
        ws = r.body.get("webSocketDebuggerUrl")
        if not ws:
            raise ConnectionError(f"{self.base_url}/json/version has no webSocketDebuggerUrl")
        return ws

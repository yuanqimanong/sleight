"""标准库 HTTP 客户端。

为什么不用 httpx / niquests：Provider 只在 launch/status/list 时发 HTTP，频率极低，
不需要连接池、不需要 HTTP/2。实测依赖代价 —— urllib 0 个包、httpx 6 个、niquests 7 个
（含两个 Rust 编译扩展）。

证书：httpx 并不"管理证书"，它只是捆一份 certifi 的 CA bundle。标准库的
``ssl.create_default_context()`` 在 Windows 走系统证书存储、Linux 走 OpenSSL 默认路径，
对公网 CA 一样能验。只有连自签证书的远程 Manager 才需要 ``ca_bundle``。
"""

from __future__ import annotations

import builtins
import http.client
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ._redact import redact_url
from .errors import AuthError, ConnectionError

log = logging.getLogger("sleight.http")

__all__ = ["HttpClient", "HttpResponse"]

DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: Any
    url: str = field(repr=False, default="")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def detail(self) -> str:
        """FastAPI 风格的 ``{"detail": "..."}``。

        CloakBrowser 的 stop 对「已停止」和「id 不存在」都返回 404，**必须靠 detail
        或 status() 才能区分**。
        """
        if isinstance(self.body, dict):
            d = self.body.get("detail")
            if isinstance(d, str):
                return d
        return ""


class HttpClient:
    """极简 JSON over HTTP 客户端。

    非 2xx **不抛异常**（401/403 除外）—— provider 需要自己解读 409/404，
    生命周期语义无法从状态码通用推导。
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        ca_bundle: str | None = None,
        verify: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._ctx = self._build_ssl_context(ca_bundle, verify)

    @staticmethod
    def _build_ssl_context(ca_bundle: str | None, verify: bool) -> ssl.SSLContext | None:
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle)
        return None  # urllib 用默认上下文

    # ------------------------------------------------------------------ #

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """发一个请求。**所有出站 HTTP 都从这里走** —— 打桩只需要盯这一个入口。

        :param method: HTTP 方法，大小写不敏感
        :param path: 相对 :attr:`base_url` 的路径，要带前导 ``/``
        :param json_body: 给了就序列化成 JSON body 并加 ``Content-Type``
        :param params: query 参数
        :param timeout: 覆盖本次请求的超时，秒。``None`` 用构造时的默认值
        :returns: :class:`HttpResponse`。**非 2xx 不抛异常**（401/403 除外）
        :raises AuthError: 401 / 403
        :raises ConnectionError: 连不上、超时、传输中断
        """
        url = self.base_url + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        data: bytes | None = None
        headers = dict(self.headers)
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        headers.setdefault("Accept", "application/json")

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(
                req, timeout=timeout or self.timeout, context=self._ctx
            ) as resp:
                return HttpResponse(resp.status, _parse(resp.read(), resp.headers), url)
        except urllib.error.HTTPError as exc:
            resp = HttpResponse(exc.code, _parse(exc.read(), exc.headers), url)
            if exc.code in (401, 403):
                suffix = f": {resp.detail}" if resp.detail else ""
                raise AuthError(f"{exc.code} from {redact_url(url)}{suffix}") from exc
            return resp
        except urllib.error.URLError as exc:
            raise ConnectionError(f"cannot reach {redact_url(url)}: {exc.reason}") from exc
        except builtins.TimeoutError as exc:  # socket timeout（注意不是本包的 TimeoutError）
            raise ConnectionError(f"timed out talking to {redact_url(url)}") from exc
        except (http.client.HTTPException, OSError) as exc:
            # urllib 只在它自己的内层 try 里包 URLError；`h.getresponse()` 期间断连会原样
            # 逃逸成 http.client.RemoteDisconnected / ConnectionResetError，绕过整个
            # SleightError 体系，调用方的 except 一个都接不住。
            raise ConnectionError(f"lost connection to {redact_url(url)}: {exc}") from exc

    def get(self, path: str, **kw: Any) -> HttpResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> HttpResponse:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> HttpResponse:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> HttpResponse:
        return self.request("DELETE", path, **kw)


def _parse(raw: bytes, headers: Any) -> Any:
    if not raw:
        return None
    ctype = (headers.get("Content-Type") or "").lower() if headers else ""
    text = raw.decode("utf-8", errors="replace")
    if "json" in ctype:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text

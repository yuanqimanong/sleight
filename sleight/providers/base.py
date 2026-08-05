"""Provider 协议与可继承基类。

Provider 只负责**发现 / 状态 / 启停 / Endpoint**。它不做实例选择，也不持锁 ——
那是 Pool 的唯一职责。
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..core._http import HttpClient, HttpResponse
from ..core.errors import InstanceError, NotFound
from ..core.types import Endpoint, InstanceInfo, InstanceStatus

if TYPE_CHECKING:
    from ..pool import InstanceHandle, Pool

log = logging.getLogger("sleight.provider")

__all__ = ["BaseProvider", "HTTPProvider", "Provider"]

_POOL_LOCK = threading.Lock()


@runtime_checkable
class Provider(Protocol):
    name: str

    def endpoint(self, instance_id: str | None = None) -> Endpoint: ...
    def list_instances(self) -> list[InstanceInfo]: ...
    def status(self, instance_id: str) -> InstanceStatus: ...
    def ensure_ready(self, instance_id: str) -> None: ...
    def recover(self, instance_id: str) -> None: ...
    def release(self, instance_id: str) -> None: ...


class BaseProvider:
    """给 Provider 提供 ``.pool`` / ``.lease()`` 便利方法。"""

    name: str = "provider"

    @property
    def pool(self) -> Pool:
        """**懒创建的单例。**

        这是排他性的命脉：如果每次 ``lease()`` 现建一个 Pool，各线程持有不同的锁表，
        三个线程完全可能同时租到同一个实例。锁表必须归属长期存活的 Pool。
        """
        pool = getattr(self, "_pool", None)
        if pool is None:
            with _POOL_LOCK:
                pool = getattr(self, "_pool", None)
                if pool is None:
                    from ..pool import Pool

                    pool = Pool([self])
                    self._pool = pool
        return pool

    def lease(self, **kw: Any) -> InstanceHandle:
        """便利方法：转发到 :attr:`pool`。**不是** Provider 协议的一部分。

        :param kw: 原样透传给 :meth:`Pool.lease() <sleight.pool.Pool.lease>`
        :returns: :class:`~sleight.pool.InstanceHandle`
        """
        return self.pool.lease(**kw)

    # 大多数后端不需要这两个
    def recover(self, instance_id: str) -> None:
        return None

    def release(self, instance_id: str) -> None:
        """默认 no-op。

        **不要顺手停实例** —— 持久化登录态在里面，停了下次要重新登录。停实例是
        运维动作，不是任务收尾动作。

        :param instance_id: 归还的实例
        """
        return None


class HTTPProvider(BaseProvider, ABC):
    """有 HTTP 管理 API 的后端的公共骨架。

    子类只填 URL 与解析。加一个 browserless provider 的成本是三个方法。

    .. warning::
       **不能用状态码元组表达生命周期语义。** CloakBrowser 实测：``stop`` 对
       「已停止」和「id 根本不存在」返回完全相同的 404 + 相同 detail。所以任何生命
       周期判断都先走 :meth:`status` —— 它是唯一能区分 ``NotFound`` 与 ``NotReady``
       的接口。``launch``/``stop`` 的状态码只作辅助信号。

    :param base_url: 管理 API 的根地址，尾部斜杠会被去掉
    :param name: 池内唯一的 provider 名，也是 uid 前缀。空则取类名小写
    :param timeout: HTTP 超时，秒
    :param ca_bundle: 自签证书的 CA 文件路径。只有连 https 私有 Manager 才用得上
    :param verify: False 关闭证书校验
    """

    launch_path = "/launch"
    stop_path = "/stop"
    ready_timeout = 60.0
    ready_poll = 0.5
    #: ``launch`` / ``stop`` 的 HTTP 超时，秒。
    #:
    #: **不能用通用的那个 15s。** 这两个接口是**同步**的 —— 它们等浏览器进程真的起来
    #: 或停掉才返回。实测冷启动一个 profile 约 69 秒（3.8 GB 内存的机器，含 Xvnc 拉起
    #: 和写默认书签），用 15s 的话会在**浏览器其实已经起来**的情况下抛 ConnectionError，
    #: 而调用方从那个异常里完全看不出实例到底起没起。
    lifecycle_timeout = 300.0
    #: launch 命中即视为"已在运行"，幂等成功
    already_running: tuple[int, ...] = (409,)
    #: stop 命中即视为"已停止" —— 仅作辅助，仍需 status() 复核
    already_stopped: tuple[int, ...] = (404,)

    def __init__(
        self,
        base_url: str,
        *,
        name: str = "",
        timeout: float = 15.0,
        ca_bundle: str | None = None,
        verify: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = name or self.__class__.__name__.lower()
        self._http = HttpClient(
            self.base_url,
            headers=self._auth_headers(),
            timeout=timeout,
            ca_bundle=ca_bundle,
            verify=verify,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} {self.base_url}>"

    # -------------------------- 子类必填 -------------------------------- #

    @abstractmethod
    def _auth_headers(self) -> dict[str, str]: ...

    @abstractmethod
    def _list(self) -> list[InstanceInfo]: ...

    @abstractmethod
    def _ws_url(self, instance_id: str) -> str: ...

    @abstractmethod
    def status(self, instance_id: str) -> InstanceStatus:
        """权威的存在性 + 就绪判定 —— 子类**必须**实现。

        生命周期语义无法从状态码通用推导，所以基类不给默认实现。

        :param instance_id: 要查的实例
        :returns: RUNNING / STOPPED / NOT_FOUND
        """

    #: 管理接口的路径前缀，子类按自己的 URL 形状覆盖
    def _instance_path(self, instance_id: str, suffix: str) -> str:
        return f"/{instance_id}{suffix}"

    # -------------------------- 基类实现 -------------------------------- #

    def list_instances(self) -> list[InstanceInfo]:
        """当前这个后端上的全部实例。"""
        return self._list()

    def endpoint(self, instance_id: str | None = None) -> Endpoint:
        """某个实例的 CDP 连接信息。

        :param instance_id: 必填 —— HTTP 类后端一个端点服务多个实例
        :returns: 含 ws_url 和鉴权头的 :class:`~sleight.core.types.Endpoint`
        :raises ValueError: 没给 instance_id
        """
        if instance_id is None:
            raise ValueError(f"{type(self).__name__} requires an instance_id")
        return Endpoint(
            http_base=self.base_url,
            ws_url=self._ws_url(instance_id),
            headers=self._auth_headers(),
        )

    def ensure_ready(self, instance_id: str) -> None:
        """确保实例在运行。**幂等** —— 已 running 直接返回，不发 launch。

        :param instance_id: 目标实例
        :raises NotFound: status() 说它不存在
        :raises InstanceError: launch 失败，或在 ``ready_timeout`` 内没起来
        """
        st = self.status(instance_id)
        if st is InstanceStatus.NOT_FOUND:
            raise NotFound(f"{self.name}: no such instance {instance_id!r}")
        if st is InstanceStatus.RUNNING:
            return
        self._launch(instance_id)
        self._await_ready(instance_id)

    def recover(self, instance_id: str) -> None:
        """stop → launch → 等就绪。

        **只恢复连接，不重放业务操作** —— sleight 不知道哪个 click 是安全的，
        重放可能意味着付了两次款。调用方从自己的业务检查点决定怎么继续。

        :param instance_id: 目标实例
        :raises NotFound: status() 说它不存在
        :raises InstanceError: 重启失败，或没在 ``ready_timeout`` 内起来
        """
        if self.status(instance_id) is InstanceStatus.NOT_FOUND:
            raise NotFound(f"{self.name}: no such instance {instance_id!r}")
        log.warning("%s: recovering instance %s", self.name, instance_id)
        self._stop(instance_id, tolerate_stopped=True)
        self._launch(instance_id)
        self._await_ready(instance_id)

    # -------------------------- 内部 ------------------------------------ #

    def _launch(self, instance_id: str) -> HttpResponse:
        r = self._http.post(
            self._instance_path(instance_id, self.launch_path), timeout=self.lifecycle_timeout
        )
        if r.ok or r.status in self.already_running:
            return r
        if r.status == 404:
            # launch 的 404 语义明确（"Profile not found"），不同于 stop
            raise NotFound(f"{self.name}: no such instance {instance_id!r}")
        raise InstanceError(f"{self.name}: launch {instance_id} failed ({r.status}) {r.detail}")

    def _stop(self, instance_id: str, *, tolerate_stopped: bool = False) -> HttpResponse:
        r = self._http.post(
            self._instance_path(instance_id, self.stop_path), timeout=self.lifecycle_timeout
        )
        if r.ok:
            return r
        if r.status in self.already_stopped:
            # 404 分不清"已停止"和"不存在" —— 必须 status() 复核，
            # 否则打错的 instance_id 会被当成幂等成功静默吞掉
            if tolerate_stopped or self.status(instance_id) is not InstanceStatus.NOT_FOUND:
                return r
            raise NotFound(f"{self.name}: no such instance {instance_id!r}")
        raise InstanceError(f"{self.name}: stop {instance_id} failed ({r.status}) {r.detail}")

    def _await_ready(self, instance_id: str) -> None:
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if self.status(instance_id) is InstanceStatus.RUNNING:
                return
            time.sleep(self.ready_poll)
        raise InstanceError(
            f"{self.name}: instance {instance_id} not ready after {self.ready_timeout}s"
        )

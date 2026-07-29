"""标准 ``logging``，不引入 loguru。

库不配置根 logger —— 那是应用的事。这里只挂一个 ``NullHandler``，免得用户没配日志时
Python 打印 "No handlers could be found"。

调试时：

    >>> import sleight
    >>> sleight.enable_debug_logging()

Logger 层级：``sleight.transport`` / ``sleight.session`` / ``sleight.input`` /
``sleight.pool`` / ``sleight.lease`` / ``sleight.provider`` / ``sleight.cloakbrowser`` /
``sleight.http``。
"""

from __future__ import annotations

import logging
from typing import TextIO

__all__ = ["enable_debug_logging"]

ROOT = "sleight"
_HANDLER_TAG = "sleight-debug"

logging.getLogger(ROOT).addHandler(logging.NullHandler())


def enable_debug_logging(level: int = logging.DEBUG, stream: TextIO | None = None) -> None:
    """给 ``sleight.*`` 挂一个 stderr handler。仅供调试与开发使用。

    **幂等** —— 重复调用只会调整级别，不会再挂一个 handler。挂两个的话每行日志
    都会打印两遍，而这种事在 notebook 和被重复 import 的模块里太容易发生了。

    凭据由 :mod:`sleight.core._redact` 在生成消息时就已脱敏，但 DEBUG 级别会打印
    完整的 CDP 流量 —— 不要把它开在生产日志里。

    :param level: 日志级别，默认 ``logging.DEBUG``
    :param stream: 输出流。``None`` 表示 ``sys.stderr``
    """
    logger = logging.getLogger(ROOT)
    logger.setLevel(level)
    for existing in logger.handlers:
        if getattr(existing, "name", None) == _HANDLER_TAG:
            existing.setLevel(level)
            return
    handler = logging.StreamHandler(stream)
    handler.name = _HANDLER_TAG
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)

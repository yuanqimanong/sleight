"""标准 ``logging``，不引入 loguru。

库不配置根 logger —— 那是应用的事。这里只挂一个 ``NullHandler``，免得用户没配日志时
Python 打印 "No handlers could be found"。

调试时：

    >>> import sleight
    >>> sleight.enable_debug_logging()

Logger 层级：``sleight.transport`` / ``sleight.session`` / ``sleight.input`` /
``sleight.pool`` / ``sleight.lease`` / ``sleight.provider`` / ``sleight.http``。
"""

from __future__ import annotations

import logging

__all__ = ["enable_debug_logging", "get_logger"]

ROOT = "sleight"

logging.getLogger(ROOT).addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{name}")


def enable_debug_logging(level: int = logging.DEBUG, stream: object | None = None) -> None:
    """给 ``sleight.*`` 挂一个 stderr handler。仅供调试与开发使用。

    凭据由 :mod:`sleight.core._redact` 在生成消息时就已脱敏，但 DEBUG 级别会打印
    完整的 CDP 流量 —— 不要把它开在生产日志里。
    """
    logger = logging.getLogger(ROOT)
    logger.setLevel(level)
    handler = logging.StreamHandler(stream)  # type: ignore[arg-type]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)

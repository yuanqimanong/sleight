"""调试日志开关。

库不配置根 logger —— 那是应用的事。这里只保证两件事：不装 handler 时不会报
"No handlers could be found"，以及重复开调试不会让每行日志打印两遍。
"""

from __future__ import annotations

import io
import logging

import pytest

from sleight import enable_debug_logging
from sleight._logging import ROOT


@pytest.fixture(autouse=True)
def restore_root_logger():
    """每个用例跑完把 sleight logger 复原，免得污染别的测试的输出。"""
    logger = logging.getLogger(ROOT)
    saved_handlers, saved_level = list(logger.handlers), logger.level
    yield
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)


def test_the_package_ships_a_null_handler():
    """没有它，用户没配日志时 Python 会打印 "No handlers could be found"。"""
    handlers = logging.getLogger(ROOT).handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_debug_logging_emits_records():
    buf = io.StringIO()
    enable_debug_logging(stream=buf)
    logging.getLogger(f"{ROOT}.session").debug("hello")
    assert "hello" in buf.getvalue()


def test_enabling_twice_does_not_double_every_line():
    """notebook 里重跑一格、模块被重复 import，都会走到这条路径上。"""
    buf = io.StringIO()
    enable_debug_logging(stream=buf)
    enable_debug_logging(stream=buf)
    enable_debug_logging(stream=buf)

    logging.getLogger(f"{ROOT}.pool").debug("once please")
    assert buf.getvalue().count("once please") == 1


def test_reenabling_adjusts_the_level_in_place():
    buf = io.StringIO()
    enable_debug_logging(stream=buf)
    enable_debug_logging(logging.WARNING, stream=buf)

    logger = logging.getLogger(f"{ROOT}.pool")
    logger.debug("filtered out")
    logger.warning("kept")
    assert "filtered out" not in buf.getvalue()
    assert "kept" in buf.getvalue()

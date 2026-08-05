"""Web 界面。需要 ``pip install "sleight[ui]"``。

单独一个子包，**引擎不依赖它** —— 命令行和 Python API 在没装 fastapi 的机器上照常工作。
"""

from __future__ import annotations

from .app import create_app, serve

__all__ = ["create_app", "serve"]

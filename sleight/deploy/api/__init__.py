"""Web 界面及其依赖包含在默认安装中。

单独一个子包，**引擎不依赖它** —— 命令行和 Python API 不会主动导入 Web 框架。
"""

from __future__ import annotations

from .app import create_app, serve

__all__ = ["create_app", "serve"]

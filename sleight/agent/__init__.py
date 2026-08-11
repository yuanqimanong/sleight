"""Agent 层：给 LLM / MCP 的稳定工具面。

核心是 :class:`~sleight.agent.gateway.Gateway` —— 把 snapshot / ref / click / type /
extract 收进四个类型化、结果结构化的工具（Session / Observe / Act / Extract）外加一个
``find``，默认不暴露 raw CDP/eval。模型读 Observe 的快照文本、挑一个 Ref、走 Act 操作，
失败拿到的是结构化错误而不是异常。
"""

from .gateway import Gateway, ToolResult
from .mcp import MCPServer

__all__ = ["Gateway", "MCPServer", "ToolResult"]

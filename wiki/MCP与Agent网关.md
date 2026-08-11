# MCP 与 Agent 网关

这页解决：怎么让 LLM（Claude Desktop / IDE / 自己的 agent）直接操作浏览器。

两层：**Gateway** 是 Python 里的类型化工具面；**MCP server** 把它包成标准协议。

## MCP server

### 起法

```bash
# 连一个已经在跑的浏览器
SLEIGHT_CDP_URL=http://127.0.0.1:9222 sleight-mcp

# 或让它自己起本机内核
SLEIGHT_BROWSER=fingerprint-chromium SLEIGHT_FINGERPRINT=42 sleight-mcp
```

### 环境变量

| 变量 | 作用 |
|---|---|
| `SLEIGHT_CDP_URL` | 连已在跑的 CDP 端点 |
| `SLEIGHT_BROWSER` | 起本机浏览器（内核名或绝对路径）|
| `SLEIGHT_FINGERPRINT` | 指纹种子（配合 `SLEIGHT_BROWSER`）|
| `SLEIGHT_NO_SANDBOX` | `=1` 时传 `--no-sandbox`（Linux root/容器）|
| `SLEIGHT_ALLOW_RAW` | `=1` 时开放 raw `eval` 逃生舱（**默认关**）|

两个来源变量**至少给一个**，都没给会**启动即退出**并打印提示——不会静默挂着。

### Claude Desktop 配置片段

```json
{
  "mcpServers": {
    "sleight": {
      "command": "sleight-mcp",
      "env": {
        "SLEIGHT_BROWSER": "fingerprint-chromium",
        "SLEIGHT_FINGERPRINT": "42",
        "SLEIGHT_NO_SANDBOX": "1"
      }
    }
  }
}
```

### 四个工具

| 工具 | action | 参数 |
|---|---|---|
| `browser_session` | `open` `reload` `back` `forward` `info` | `url` `wait_text` `wait_selector` `timeout` |
| `browser_observe` | `snapshot` `find` | `query` `role` `max_depth` |
| `browser_act` | `click` `type` `press` `scroll` `hover` | `ref` **或** `coordinate` `text` `key` `dy` `human` |
| `browser_extract` | — | `min_length` |

**典型循环**：`observe(snapshot)` 拿到带 `[ref]` 的树 → 模型挑一个 → `act(click, ref=…)`。
找不到就 `observe(find, query="提交")` 按文字定位。

**坐标逃生舱**：`browser_act` 既收 `ref` 也收 `coordinate: [x, y]`。Canvas、纯图标 UI
这类 AX 树里没有可用节点的场景要靠它——只收 ref 的设计会在那里硬失败。

### 协议细节

- JSON-RPC 2.0 over stdio，**零依赖手写**（不引官方 mcp SDK）。
- 支持 `initialize` / `tools/list` / `tools/call` / `ping`；`notifications/*` 不回。
- **工具级失败走 `isError: true`**（内容里是错误文本），**不是** JSON-RPC error——
  协议错误（未知方法 `-32601`、坏 JSON `-32700`）才用后者。
- **Gateway 懒创建**：`initialize` 和 `tools/list` 不连浏览器，第一个 `tools/call` 才连。
- **stdout 只走协议**，所有日志走 stderr。

### 排查

```bash
SLEIGHT_CDP_URL=http://127.0.0.1:9222 sleight-mcp
# 粘一行回车：
{"jsonrpc":"2.0","id":1,"method":"ping"}
# 应回：{"jsonrpc": "2.0", "id": 1, "result": {}}
```
起不来先看 MCP host 的 **stderr** 日志。

## Gateway（Python 里直接用）

```python
from sleight import launch
from sleight.agent import Gateway

with launch("chromium", no_sandbox=True) as s:
    g = Gateway(s)                       # allow_raw=False（默认），max_batch=5

    g.session("open", url="https://example.com", wait_text="Example")
    snap = g.observe("snapshot")
    print(snap.data["snapshot"], snap.data["refs"])

    hit = g.find("more information")     # = observe("find", query=…)
    ref = hit.data["matches"][0]["ref"]
    g.act("click", ref=ref)

    doc = g.extract()
    print(doc.data["title"], doc.data["quality"])
```

### `ToolResult`

```python
ToolResult(ok: bool, tool: str, action: str, data: dict, error: str | None)
```
- 支持 `if result:` （`__bool__` 就是 `ok`）。
- 失败**返回**而不抛：`ToolResult.fail(...)`，`error` 里是人话。
- Ref 因导航失效时错误直接提示重新拍快照。

### 安全默认

| 项 | 默认 | 说明 |
|---|---|---|
| raw `eval` | **关** | `Gateway(s, allow_raw=True)` 才开；**页面内容永远无法打开它** |
| 硬上限 | 服务端 | `max_batch` 在 Gateway 上，不在模型提交的请求里——请求里的"限额"等于没有限额 |
| 快照作废 | 自动 | `session` 导航后清掉旧快照，旧 ref 不能再用 |

`g.describe()` 返回工具/action 目录，可用来生成系统提示或别的协议的 schema。

---
下一步 → [[Snapshot与Ref]]（Ref 的生命周期）· [[浏览器内核选择]]

# sleight

**像人一样驱动任何 CDP 浏览器。** 带真实手抖的贝塞尔轨迹、按击键动力学研究建模的打字节奏，
以及浏览器实例池的排他租用。

Python ≥ 3.11 · 默认包含 XPath 与 Web 界面 · MIT

**[English README](README.md)** · 📖 **[Wiki — 完整中文文档](https://github.com/yuanqimanong/sleight/wiki)**

```bash
pip install sleight                     # 默认包含 XPath 和 sleight ui

sleight browser install fingerprint-chromium   # 可选：装一个反检测内核
```

## 统一浏览器服务

```bash
sleight ui --bind 127.0.0.1 --port 8700
```

首次启动显示一次管理员 token。Web 创建执行用户，配置默认环境及插件/代理模板，再签发执行 token。
启动时自动读取当前工作目录的 `.env`，系统环境变量优先。参考 [.env.example](.env.example)，填写 `SLEIGHT_UI_TOKEN` 可使用固定管理员登录 token。
默认 SQLite；设置 `SLEIGHT_DATABASE_URL=postgresql://...` 可使用 PostgreSQL，配置与运行账本使用相同模型。
部署只运行 Python，Vue 页面已打包，不需要 Node 或 Redis。

```python
from sleight.client import ServiceClient

with ServiceClient("http://sleight-host:8700", token="<执行用户 token>") as client:
    profile = client.create_profile(name="任务", fingerprint_seed=12345)
    with client.session(profile["id"], human=True) as session:
        session.open("https://example.com")
        print(session.title())
```

HTTP MCP 位于 `/mcp`。stdio 使用 `SLEIGHT_UPSTREAM`、`SLEIGHT_TOKEN` 启动 `sleight-mcp`。
[部署、数据库迁移与回滚](docs/0.6部署与验收.md)。旧本机 CDP API 仍可使用。

## 30 秒

```python
from sleight import connect, Text

with connect("http://127.0.0.1:9222") as s:      # 自己开一个 tab，退出时关掉
    s.open("https://example.com", wait=Text("Example Domain"))
    print(s.title(), len(s.content()))
```

后面挂着真实 profile 的浏览器池：

```python
from sleight.providers import CloakBrowserManager

mgr = CloakBrowserManager("http://127.0.0.1:19000", token="…")

with mgr.lease() as inst:                    # 排他租用，退出即释放
    with inst.session(human=True) as s:      # 每个动作都走拟人轨迹
        s.open("https://example.com")
        s.click("#login")
        s.type("#email", "user@example.com")
        s.click("#submit", human=False)      # …这一下除外，这里要的是快
```

看页面到底加载了什么 —— 库给结构化数据，打什么由你决定：

```python
with s.capture_resources(types={"Script", "Stylesheet"}) as capture:
    s.open(url, wait=Load())
    s.pump_events(10)          # load 之后才异步到的那一批

for r in capture.snapshot():
    print(r.resource_type, r.status, r.url)
```

指定某一个 profile —— 按 id、按名字、按标签。名字对不上会**立刻**失败并列出可见的名字，
而不是一路阻塞到超时：

```python
with mgr.lease(instance_id="5edcc28a-…") as inst:              ...
with mgr.lease(name="Win-US-02") as inst:                      ...
with mgr.lease(where=lambda i: "us" in i.tags) as inst:        ...

handles = pool.lease_many(4, names=NAMES, timeout=60)   # 部分失败自动回滚
```

拖一个滑块 —— 按钮 mask 全程按住，轨迹先冲过头再折回，松手前有停顿，因为
**一到就松手**是最可靠的机器特征：

```python
s.drag("#captcha-knob", by=(212, 0), human=CAREFUL)
s.drag_and_drop("#card", "#done-column")     # HTML5 原生拖放，或 JS 的 —— 都支持
```

换出口 IP。隧道按 TCP 连接分配地址，而 Chrome 复用 keep-alive socket，所以整轮跑下来
会钉在同一个 IP 上。**只有新建浏览器上下文**能可靠打破这一点 —— 清缓存、加唯一查询串、
`emulateNetworkConditions` 全都没用（[为什么](https://github.com/yuanqimanong/sleight/wiki/常见问题)）：

```python
with inst.context() as ctx, ctx.session() as s:   # 自己的 socket pool → 新出口
    print(ctx.exit_ip())
    s.open(url)
```

别为丢掉的字节付钱，事后再甩掉追踪 cookie：

```python
with s.block(types=["Image", "Media", "Font"]) as blocked:
    s.open(url)
print(blocked.by_type)                       # {'Image': 34, 'Font': 6}

report = s.clear_site_data("https://example.com")
print(report.cookies)                        # ('datadome',) —— 真正被清掉的
```

三个 provider 的实例，一个逻辑池：

```python
from sleight import Pool
from sleight.providers import CloakBrowserManager, Plain

pool = Pool([
    CloakBrowserManager("http://10.0.0.1:9000", token=T1, name="hk"),
    CloakBrowserManager("http://10.0.0.2:9000", token=T2, name="sg"),
    Plain("http://127.0.0.1:9222", name="local"),
])

with pool.lease(where=lambda i: "us" in i.tags) as inst:
    ...
```

## 让 LLM 来驱动

把页面压成模型能读的东西，每个可交互元素拿回一个稳定的 ref。点 ref 走的是**同一条**
拟人输入链 —— 真实的 `isTrusted` 事件、两次命中校验，一样不少：

```python
snap = s.snapshot()
print(snap.text())
# RootWebArea "Checkout"
#   heading "Your order"
#   textbox "Card number" [e1]
#   button "Pay now" [e2]

s.type(snap.ref("e1"), "4242 4242 4242 4242")
s.click(snap.ref("e2"))
```

Ref 由 `(loaderId, backendNodeId)` 做键：同一个节点跨多次快照拿到同一个 ref；页面一导航，
每个 ref 立刻失效（`StaleRef`），而不是悄悄指向此刻占着那个位置的东西。子 frame 会被合并
进来 —— 同进程 iframe 和跨源 OOPIF 都是 —— 所以 iframe 里的元素和别的元素一样有 ref。

直接在浏览器里，从渲染后的 DOM 提取正文和常用字段：

```python
doc = s.extract_document()
doc.title, doc.byline, doc.text[:80], doc.json_ld, doc.low_quality
```

要读很多元素？取一次静态快照在内存里查，别为每个元素付一次 CDP 往返
（50 行表格实测约快 18 倍）：

```python
dom = s.parse()                                   # 一次抓取，之后纯 Python
names = [e.text for e in dom.query_all("tr.row td.name")]
deep  = s.parse(pierce_shadow=True)               # open Shadow DOM 也内联进来
rows  = s.parse(xpath=True).xpath("//tr[3]/td")   # XPath 默认安装可用
```

页面登录后，还可以直接读取接口，复用当前浏览器的 Cookie（包括 HttpOnly）、代理和连接：

```python
s.open("https://your-site.example/account", referrer="https://your-site.example/")
response = s.fetch("/api/items")
if response.ok:
    items = response.json()
```

`fetch()` 不导航、不新建实例，远程 `ServiceClient` 会话使用同一 API。SDK 支持
GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS；默认超时 30 秒、响应最多 1 MiB，超时或超限即取消读取。
返回 UTF-8 文本；HTTP 4xx/5xx 保留真实状态，网络错误抛异常，不自动重试。
仍受 CORS/CSP、SameSite 和浏览器请求头规则约束，不能替代所有页面渲染。
`open(referrer=...)` 仅在显式提供来源时设置，跨站只发送来源域，HTTPS → HTTP 不发送来源。

自己起一个本机浏览器 —— 任何 Chromium 构建，包括反检测内核。同一个种子，每次同一套指纹：

```python
from sleight import launch

with launch("fingerprint-chromium", fingerprint=42) as s:
    s.open("https://example.com")
```

整套东西还是一个 **MCP server**，模型可以直接驱动：

```bash
SLEIGHT_CDP_URL=http://127.0.0.1:9222 sleight-mcp
```

零额外依赖的 JSON-RPC over stdio，暴露五个工具 —— `browser_session` /
`browser_observe`（snapshot + find）/ `browser_act`（按 ref **或**按坐标，后者给 canvas
和纯图标 UI 用）/ `browser_extract` / `browser_fetch`（GET/HEAD，最多 256 KiB）。
Raw CDP 和 `eval` 默认隐藏，要显式打开。

## 有一队浏览器要驱动

跑 CloakBrowser 难的不是驱动，是部署、插件下发，以及"这个 profile 为什么行为不一样"的考古。
所以同一个包里带了运维 CLI。本机 docker 和远程 SSH 走同一条代码路径，只有 runner 不同：

```bash
sleight hosts add hk-01 --ssh deploy@10.0.0.12 --dir /srv/cloakbrowser-manager --sudo
sleight deploy --host hk-01
sleight deployments add hk-01 second --dir /srv/cbm-2 --port 9001   # 同一台机，第二个 manager
sleight ext push ./plugins/bypass-paywalls --host hk-01   # MV3 校验 + 权限检查
sleight ext apply --host hk-01                            # 下发到每个 profile，然后重启
sleight ext verify --host hk-01                           # 浏览器真的加载了吗
sleight ui                                                # 同样的事，在浏览器里做
```

主机、每台机上的 manager，以及部署/备份/升级的审计流水，都存在本地 SQLite
（启动目录下的 `data/control.db`），CLI 和 Web 界面共用。`SLEIGHT_HOME` 可覆盖数据目录。

`sleight ui` 会一步步带你走：连接主机（保存前先做真实连接测试）、选规模模板、体检、部署。
每个选项旁边都有一行小字说明"填错了会坏什么"和推荐值 —— 后端定义一次，CLI
（`sleight templates`）和界面共同渲染。

部署是幂等的，`--dry-run` 会打印它将写入的确切字节，而那些**不该做的事是被拒绝而不是被
记录在文档里**：不许用 `latest` tag、不许 `down -v`、不许悄悄轮换正在用的 `AUTH_TOKEN`、
不许在同一个 `/data` 上起第二个 manager。引擎只用标准库（SSH 就是系统的 `ssh`）；
`sleight ui` 及其依赖已包含在默认安装中。

## 为什么会有这个库

指纹级别的反检测是**已经解决**的问题 —— CloakBrowser 在源码层改 Chromium，Camoufox 改
Firefox。它们解决的是**浏览器长什么样**。没有东西解决**它怎么动**。

- Playwright 和 Puppeteer 让鼠标瞬移。`mouse.move(steps=N)` 插的是**匀速直线** ——
  零抖动、零加速度。这本身就是一个签名。
- 浏览器不会替你补轨迹。**即使浏览器侧开了 humanize**，外部 CDP 客户端在按下与抬起之间
  产生的 `mousemove` 事件是 **零** 个。实测，不是猜的。
- 好的轨迹实现都在 JavaScript 里（`ghost-cursor`）。Python 移植版维护都很薄。
- Crawlee for Python 的 `BrowserPool` [不支持远程浏览器](https://github.com/apify/crawlee-python/issues/1743)。

sleight 补的正是这个缺口：**Python + 远程 CDP + 人类行为 + 实例租用。**

## 和 Playwright 的关系

**不是替代，是补充。** sleight 是驱动层，不是框架。它刻意不做下载、录像、tracing 和完整的
locator DSL。需要那些就用 Playwright。

有意思的是两者可以一起用：sleight 的 `human` 模块是
[sans-io](https://sans-io.readthedocs.io/) 的 —— 它只产出 `(method, params, sleep_after)`
三元组，从不碰 socket —— 所以它驱动 Playwright 的 `CDPSession` 和驱动 sleight 自己的
transport 一样顺。

## 动作凭什么可信

| | sleight | 典型自动化 |
|---|---|---|
| 路径形状 | 三次贝塞尔，控制点偏向一侧 | 直线 |
| 微动作 | WindMouse 风力项（相关抖动） | 没有，或白噪声 |
| 点数 | Fitts 定律 —— 远而小的目标花更久 | 固定 `steps=N` |
| 落点 | 目标框内的截断高斯 | 正中心 |
| 坐标 | 整数 | 用浮点当"抖动" |
| 过冲 | 冲过目标再折回，按距离缩放 | 精确到达 |
| 打字 | 逐字符事件，间隔按 digraph 分类 | 一次 `insertText` |
| 滚动 | 反复的小 `mouseWheel` 增量 | 一次 `scrollTo` |
| 拖拽 | 按钮 mask 全程按住、滑块级过冲、松手前停顿 | 瞬移，或一到就松手 |

参数不是编的。它们来自
[WindMouse](https://ben.land/post/2021/04/25/windmouse-human-mouse-movement/) 物理模型、
[ghost-cursor](https://github.com/Xetera/ghost-cursor) 的 Fitts 定律点数预算，
以及已发表的击键动力学测量（左右手交替 digraph 平均 114 ms，同手不同指 131 ms，
同一根手指最慢且方差最大）。

## 范围

**做：** 导航/刷新/历史 · 类型化等待条件 · 渲染后 DOM 读取 · CSS 查询 ·
拟人鼠标/键盘/滚轮/**拖拽** · 元素截图 · 表单（`select_option`、`upload_file`）·
用于换出口 IP 的独立**浏览器上下文** · 按 origin 的**站点数据清理** ·
基于 Fetch domain 的**请求拦截** · `exit_ip()` · 结构化网络资源捕获 ·
跨 provider 的实例发现 · 带 TTL 续租的协作式排他租用（内存，或数据库跨进程）·
幂等恢复 · 通过本机 docker 或 SSH 部署运维 CloakBrowser Manager（含插件）。

**也做**（面向 LLM 的那一层）：**iframe / OOPIF / Shadow DOM 穿透** ·
带稳定 ref 的**无障碍快照** · 正文与元数据**抽取** · 五工具 **agent 网关**与 **MCP server** ·
启动本机浏览器 binary。

**不做：** 调度与队列 · 指纹伪装（那是浏览器的职责 —— sleight 负责驱动它，见 `LocalLauncher`）·
强制 fencing · WebDriver BiDi · Firefox。

部署层在自己的子包里，`import sleight` 永远不会导入它，所以驱动层始终是个一依赖的库。

## 已知限制

实测出来的，不是推测。每一条都有人花过一天才弄明白：

| | |
|---|---|
| **上下文里用不了扩展** | `Target.createBrowserContext` 建的是无痕上下文，Chrome 默认不在无痕里启用扩展。同一个 profile、同一个 URL：`chrome-extension://<id>/…` 在默认 context 打得开，在新 context 里返回 `ERR_BLOCKED_BY_CLIENT`。所以**换出口 IP 和用插件互斥** —— 依赖插件就靠多租几个 profile、各配各的上游代理来换出口。 |
| **重定向时 `reload()` 可能提前返回** | 一次重定向是两次文档提交，中间那个也可能发自己的 `DOMContentLoaded`。实测 `http://` → `https://` 复现 3/8，不重定向 0/8。这不是 sleight 特有的。在意就等页面自己的东西（`Selector`、`Text`）。 |
| **`block()` 只在 sleight 跟浏览器说话时生效** | 被暂停的请求要靠事件泵拿裁决，而事件泵跑在 `open` / `wait` / `pump_events` / 每次 `call` 里。纯 `time.sleep()` 期间请求会挂着。 |
| **`Transport` 归属创建它的线程** | 是**强制**的不是"建议"：跨线程用当场抛错。每线程租自己的实例。`Pool` 和租约表则是刻意共享的。 |
| **`set_viewport()` 改不了 `screen.*`，`clear_viewport()` 也未必能改回尺寸** | 那是渲染层覆盖；屏幕尺寸是启动时定死的 profile 指纹字段。清除覆盖只保证"没有覆盖" —— 在 Chromium 146 + Xvnc 上实测，一半的尝试窗口弹回，另一半停在被覆盖的尺寸。设你要的尺寸，别指望恢复。 |

## 状态

`0.x` —— alpha，API 还会变。每个版本的破坏性变更写在它的 git tag 里。发布由
[`.github/workflows/publish.yml`](.github/workflows/publish.yml) 从 tag 触发，
走 PyPI Trusted Publishing —— 仓库里不存任何 token。

## 许可

MIT


## 0.6 deployment

[三项目部署、兼容边界与完整回滚](docs/0.6部署与验收.md)。Web 管理原生浏览器和 Docker Manager，统一插件/代理模板，复用官方 VNC。服务统一管理 SQLite/PostgreSQL 配额、租约与回收，客户端通过用户 token 接入，Sleight 服务必须常驻。

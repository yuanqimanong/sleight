# 浏览器操作 API

这页是 `Session` 的完整方法参考（55 个公开成员）。拿一个 Session 的方式见 [[快速开始]]。

## 顶层入口

| 函数 | 说明 |
|---|---|
| `connect(url, *, headers=None, **kw)` | 连已在跑的 CDP 端点（`http://host:port` 或 `ws://…`），新建自有 tab |
| `launch(binary, *, fingerprint=None, headless=True, no_sandbox=False, profile_dir=None, args=(), **kw)` | 起本机浏览器再连上；退出时停进程、清临时 profile |

`**kw` 透传给 `Session`：`human`（拟人档位）、`rng`（固定 seed 可复现）、`track_network`、`track_runtime`。

## 事件量开关（窄链路/高 RTT 时用）

CDP 的入站流量里，**命令响应**由你自己的动作决定，**事件**由浏览器主动推。两个开关关的都是后者，
默认都是 `True`，现有代码不用改。

| 开关 | 关掉之后失去 | 关掉之后**不受影响** |
|---|---|---|
| `track_network=False` | `NetworkIdle` 等待条件、`cookies()`、`capture_resources()` | 其余全部 |
| `track_runtime=False` | `frames()` / `frame()` / `frame_element()`（直接抛 `SleightError`）；`snapshot()` 不再下钻**跨源** OOPIF | `eval()` / `content()` / `query()` / `click()` / `type()` / `wait(DomReady/Load/Text/Selector)`、导航纪元，以及 `snapshot()` 对**同源** iframe 的合并 |

`eval()` 为什么不受影响：`Runtime.evaluate` 是**命令**，不需要先 `Runtime.enable`——enable 打开的只是
事件流（`consoleAPICalled` / `exceptionThrown` / `executionContextCreated`）。库里唯一传 `contextId`
的是子 frame 求值那条路，`eval()` 不传。

```python
# 只抓正文、不进 iframe 的爬虫：两个都可以关
with connect(url, track_network=False, track_runtime=False) as s:
    s.open(article_url)              # wait 靠 Page 域，照常
    html = s.content()
```

> 想省流量**不要**用 `block()` / `Fetch.enable`：它对每个子资源产生一条
> `Fetch.requestPaused`（含完整请求头）加一条回应，两帧过链路；而被拦掉的资源本来就下载在
> 浏览器宿主上，根本不占你到浏览器这条链路。方向是反的。

## 导航

| 方法 | 说明 |
|---|---|
| `open(url, *, wait=DomReady(), timeout=60)` | 导航并等待。同文档(hash)跳转时 `DomReady`/`Load` 直接返回 |
| `reload(*, ignore_cache=False, wait=DomReady(), timeout=60)` | 刷新。**与 `open(当前URL)` 不同**（后者会命中缓存）|
| `back(*, steps=1, …)` / `forward(*, steps=1, …)` | 历史前进后退；步数不够抛 `SleightError` |
| `history()` | `(当前下标, NavigationEntry 列表)` |

> **重定向陷阱**：一次重定向是两次文档提交，中间那个也可能发自己的 `DOMContentLoaded`。
> 实测 `http://`→`https://` 复现 3/8。在意就等页面自己的东西（`Selector`/`Text`）。

## 等待条件

`wait(cond, *, timeout=30)`，轮询间隔 0.10s→0.25s 递增。条件类型：

| 条件 | 含义 |
|---|---|
| `DomReady()` | `DOMContentLoaded`（默认）|
| `Load()` | `load` |
| `Text("字样")` | body 文本包含 |
| `Selector("#x")` | 选择器命中 ≥1 |
| `Gone("#x")` | 选择器命中 0 |
| `NetworkIdle(quiet=…)` | 网络安静（要 `track_network=True`）|

超时抛 `TimeoutError`，异常带 `last_value`（最后一次求值结果）。

## 读取

| 方法 | 说明 |
|---|---|
| `content()` | 渲染后 `documentElement.outerHTML`（**不含** shadow 内容）|
| `text()` | `body.innerText`（隐藏元素不计）|
| `title()` / `url()` | 标题 / 当前地址（重定向后）|
| `viewport()` | `(innerWidth, innerHeight)`，求值失败回落 `(1280,720)` |
| `eval(expr)` | `Runtime.evaluate`，by-value。**只可读**——写交互会产生 `isTrusted=false` 假事件 |
| `call(method, params, **kw)` | 原始 CDP 逃生舱（带本会话 sessionId）|

## 查找

| 方法 | 说明 |
|---|---|
| `query(selector)` | 第一个匹配的实时 `Element`；没有返回 `None` |
| `query_all(selector)` | 全部匹配（文档顺序）|
| `query_shadow(selector, index=0)` | **穿透 open Shadow DOM** 定位 |
| `require(target)` | 把选择器/元素统一成"确认存在"的元素，没有则抛 `ElementError` |

`query`/`query_all` **只看主 frame 普通 DOM**，不穿 iframe。

## Frame

| 方法 | 说明 |
|---|---|
| `frames()` | 全部 frame（主 + 同源子 + 跨源 OOPIF），每个带 `reachable` |
| `frame(match)` | 按 frameId / name / URL 子串定位，返回只读 `FrameView` |
| `frame_element(iframe选择器, 内部选择器, index=0)` | iframe 内的可交互元素（同源与 OOPIF 都支持）|

整层依赖 Runtime 域，会话建成 `track_runtime=False` 时这三个都抛 `SleightError`。详见
[[Frame与ShadowDOM]]。

## LLM 层

| 方法 | 说明 |
|---|---|
| `snapshot(*, max_depth=None, cross_frame=True)` | 无障碍快照 + 稳定 Ref，默认合并子 frame。见 [[Snapshot与Ref]] |
| `parse(*, pierce_shadow=False, xpath=False)` | 离线静态树，批量只读。见 [[数据提取]] |
| `extract_document(*, min_length=200)` | 正文 + 常用字段。见 [[数据提取]] |

## 交互（全部走拟人输入链）

| 方法 | 说明 |
|---|---|
| `click(target, *, human=None, button="left", click_count=1)` | 移动→命中校验→按下→抬起；**点击前后各校验一次** |
| `double_click(target, **kw)` | 两次完整的按下抬起（`detail` 依次 1、2）|
| `type(target, text, *, human=None, clear=False)` | 逐字符按键；`clear=True` 会先校验焦点 |
| `press(chord, *, human=None)` | 组合键，如 `"Ctrl+A"` |
| `hover(target, *, human=None)` | 只移动不按下 |
| `scroll(dy, *, human=None)` / `scroll_into_view(target, …)` | 滚轮 / 滚到可见 |
| `drag(target, *, by=(dx,dy), to=…, human=None)` | 拖拽，按住全程、过冲折返、松手前停顿 |
| `drag_and_drop(source, target, *, native=None)` | HTML5 原生拖放或 JS 拖动 |
| `select_option(target, *, value=…, label=…)` | 下拉选择 |
| `upload_file(target, *paths)` | 文件上传（路径是**浏览器那台机器**上的）|

`target` 可以是：CSS 选择器 / `Element` / `BackendElement`(Ref) / `FrameElement` / `Point` / `Box`。
**给裸坐标（`Point`/`Box`）不做命中校验**——误点了也不报错。

`human` 三态：`None` 继承 Session 默认 · `False` 直通 · `True`/`HumanProfile` 指定档位。见 [[拟人化详解]]。

## 视口与截图

| 方法 | 说明 |
|---|---|
| `set_viewport(w, h, …)` / `clear_viewport()` | 渲染层覆盖。**改不了 `screen.*`**（那是启动时定死的指纹字段）|
| `screenshot(path=None, *, target=None)` | 整页或单个元素，PNG |

## 网络与状态

| 方法 | 说明 |
|---|---|
| `block(types=[...], urls=[...])` | 基于 Fetch domain 拦截。**只在 sleight 与浏览器通信时生效** |
| `capture_resources(types=…, predicate=…, dedupe_by="url", …)` | 结构化收集加载的资源 |
| `observe_events(callback)` | 挂原始 CDP 事件观察者（低层逃生舱）|
| `pump_events(duration, tick=0.25)` | 原地收事件 N 秒（没有明确条件时用）|
| `cookies(urls=None)` | 读 cookie（**只读，没有写接口**）|
| `exit_ip()` | 当前出口 IP |
| `clear_site_data(url)` | 按 origin 清 cookie/localStorage/IndexedDB/CacheStorage/SW |
| `clear_browser_data()` | 浏览器级清理（高破坏性）|

## 生命周期

`close()` 幂等；自有 target 走 `Target.closeTarget`，接管的只 detach。
`with` 语法自动收尾。属性：`target_id` / `owned_target` / `transport` / `cdp_session_id` / `closed` / `cursor`。

## Element（实时元素）

`exists()` `box()` `require_box()` `text()` `attr(name)` `value()` `screenshot()`
`select_option()` `upload_file()` `object_id()` `in_viewport()` `scroll_metrics()`
`hit_test(x,y)` `require_hit(...)` `has_focus()` `require_focus(...)` `js_ref`

> `attr("value")` 读的是 HTML **属性**（初值，不随输入变），要当前值用 `value()`。

## Pool / InstanceHandle / BrowserContext

见 [[分布式与实例池]]。

---
下一步 → [[数据提取]] · [[Frame与ShadowDOM]] · [[拟人化详解]]

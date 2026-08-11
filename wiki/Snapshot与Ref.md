# Snapshot 与 Ref

这页解决：怎么把页面变成 LLM 能读的东西，以及怎么让模型稳定地"指到"某个元素。

## 一句话

`snapshot()` 把页面压成一棵无障碍树，每个**可交互**元素带一个 `[ref]`；
`snap.ref("e1")` 把它解析回一个能点能输入的元素。

```python
snap = s.snapshot()
print(snap.text())
# RootWebArea "Checkout"
#   heading "Your order"
#   textbox "Card number" [e1]
#   button "Pay now" [e2]

s.type(snap.ref("e1"), "4242 4242 4242 4242")
s.click(snap.ref("e2"))          # 真实 isTrusted 事件，走拟人轨迹 + 双重命中校验
```

**关键**：`snap.ref(...)` 返回的东西满足 `ElementLike` 协议，直接喂给现有的
`click`/`type`——拟人轨迹、两次命中校验、真实 CDP 输入一律照旧，输入链一行代码都没改。

## 签名

```python
snapshot(*, max_depth: int | None = None, cross_frame: bool = True) -> Snapshot
```

| 参数 | 作用 |
|---|---|
| `max_depth` | 树的最大深度（含下钻进 frame 的深度）。深页用它压上下文 |
| `cross_frame` | 是否合并子 frame，**默认合并** |

`Snapshot` 上：`text()` 渲染成文本 · `refs` 全部 ref 列表 · `ref(r)` 解析 · `root` 根节点 ·
`generation` 取快照时的 loaderId。

## 哪些元素会拿到 Ref

只有**可交互** role 才发 ref——省得模型上下文里全是噪声：

```
button link textbox searchbox combobox listbox checkbox radio
menuitem menuitemcheckbox menuitemradio tab switch slider
spinbutton option treeitem textfield
```

标题、段落、表格这类结构性节点会**出现在树里**（给模型上下文），但**不发 ref**。

## Ref 的生命周期

内部用 **`(loaderId, backendNodeId)` 复合键**（对齐 Google chrome-devtools-mcp 的设计）：

- **同一节点跨多次快照 → 同一个 ref**。多轮 agent 循环不会每次都换编号。
- **页面一导航 → 全部失效**。`snap.ref("e1")` 抛 `StaleRef`，提示重新拍快照。

```python
snap = s.snapshot()
s.open("https://other.example")
snap.ref("e1")        # StaleRef: from a stale snapshot (page navigated since)
```

这是**有意的**：绝不静默降级去点"此刻恰好占着那个位置"的另一个元素。

不同 frame 里恰好相同的 `backendNodeId`（OOPIF 子 session 各自编号）会拿到**不同**的 ref。

## 蒸馏规则

原始 AX 树对模型来说噪声太多，输出前做三件事：

1. **`InlineTextBox` 永远去掉** —— 那是最底层文本几何，对模型零价值。
2. **与父节点同名的 `StaticText` 折叠** —— 按钮/标题/链接的文字标签，不重复占一行。
3. **无语义容器上提** —— `generic`/`none`/无 role 且无 ref 无名的节点，把子节点提上来，自己不占行。

效果对比：

```
蒸馏前                          蒸馏后
RootWebArea "T"                 RootWebArea "T"
  group                           group
    heading "Head"                  heading "Head"
      StaticText "Head"             button "Go" [e1]
        InlineTextBox "Head"        textbox "search" [e2]
    button "Go" [e1]
      StaticText "Go"
        InlineTextBox "Go"
    textbox "search" [e2]
```

**不做 token 计数**：三个成熟实现（playwright-mcp / chrome-devtools-mcp / browser-use）
都不在服务端算 token，而是用"结构蒸馏 + depth 上限 + 只给可交互节点 ref"。这里照做——
省一个 tokenizer 依赖，也不和某家分词器耦合。要压上下文就用 `max_depth`。

## 跨 frame 合并

`cross_frame=True`（默认）会把子 frame 的内容并进同一棵树，**同进程 iframe 和跨源 OOPIF 都算**：

```
RootWebArea "P"
  group
    button "ParentBtn" [e1]
    Iframe
      group
        button "ChildBtn" [e2]      ← iframe 里的元素，照样有 ref
        textbox "childinput" [e3]
```

`s.click(snap.ref("e2"))` 直接点进 iframe 里的按钮，**不用手动切 frame**。

两种 frame 的底层机制不同（同进程复用主 session 的 backendNodeId；OOPIF 走它自己 attach
出来的子 session + 父页偏移），但在树里没有区别。细节见 [[Frame与ShadowDOM]]。

**取不到的子 frame 当叶子处理**，不崩——比如 OOPIF 还没就绪或 attach 失败时，
那个 `Iframe` 节点就是空的，不会伪装成"里面没东西"。

## 与静态树的分工

| | `snapshot()` | `parse()` |
|---|---|---|
| 用途 | 给**模型**看、按 ref **交互** | **批量只读**抓数据 |
| 内容 | 无障碍语义（role + name）| 完整 DOM |
| 能交互 | ✅ | ❌ |
| 查询方式 | 遍历树 / `find` | CSS / XPath |
| 速度 | 一次 AX 抓取 | 一次 HTML 抓取（批量读快 ~18x）|

见 [[数据提取]]。

---
下一步 → [[MCP与Agent网关]]（把这套暴露给 LLM）· [[Frame与ShadowDOM]]

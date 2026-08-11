# Frame 与 Shadow DOM

这页解决：怎么读/点 iframe 里的东西、跨源 iframe 有什么不同、Shadow DOM 怎么穿透。

> 背景：`query()` / `query_all()` / `Element` **只看主 frame 的普通 DOM**。
> 验证码（DataDome 那种滑块）常常就在 iframe 里——这页讲怎么够到它。

## 三种入口

| 需求 | 用什么 |
|---|---|
| 看有哪些 frame | `s.frames()` |
| **读** iframe 内容 | `s.frame(match)` → `FrameView` |
| **点/输入** iframe 里的元素 | `s.frame_element(iframe选择器, 内部选择器)` |
| 让 LLM 一次看到所有 frame | `s.snapshot()`（默认合并，见 [[Snapshot与Ref]]）|

## 枚举

```python
for f in s.frames():
    print(f.frame_id, f.url, f.is_main, f.reachable)
```

`FrameInfo` 字段：`frame_id` `url` `name` `parent_id` `is_main` `reachable`。

**`reachable` 是关键**：
- `True` = 同源/同进程子 frame，主 session 直接有它的执行上下文。
- `False` = **跨源 OOPIF**，在别的进程/别的 CDP target。它仍会出现在列表里（从
  `Target.getTargets` 并入），只是要另开子 session 才能读。

## 读 iframe

```python
view = s.frame("child.html")     # 按 frameId / name / URL 子串匹配
print(view.text())
print(view.count("#btn"))
view.eval("document.title")
```

同源和跨源**都支持**——跨源会自动 attach 出子 session 再读，对你透明。

## 点 iframe 里的元素

```python
target = s.frame_element("#captcha-frame", "#slider-knob")
s.drag(target, by=(212, 0), human=CAREFUL)     # 走完整拟人链
```

返回的对象满足 `ElementLike`，可直接喂给 `click` / `type` / `drag`。

### 底层怎么做的

| | 同进程 iframe | 跨源 OOPIF |
|---|---|---|
| 求值 | 主 session 的 `executionContextId` | 它自己 attach 的**子 CDP session** |
| 几何 | `DOM.getBoxModel` **直接给顶层坐标**（Chromium 已把偏移/滚动/transform 拍平）| 子 session 给 **frame-local** 坐标，需**加上 `<iframe>` 在父页的偏移** |
| 输入 | 从顶层 session 发 | 同样从顶层 session 发 |
| 命中校验 | `DOM.getNodeForLocation`（原生穿透 iframe 和 shadow）| **两级**：父页命中 iframe + frame 内命中目标 |

**实测确认**：同进程 frame 的 `backendNodeId` 在主 session 上就能 `getBoxModel`，
返回的就是顶层坐标——所以这条路径零额外换算。

## 显式失败，绝不静默误点

这几种情况会**抛错**而不是硬点：

| 情况 | 行为 |
|---|---|
| iframe（或其祖先）带 CSS `transform` | 抛错。缩放/旋转下线性坐标相加会算错落点 |
| 元素在 frame 内但不在视口 | 命中校验失败抛错 |
| 父页有东西盖住 iframe | 抛错，并说明是父页遮挡 |
| OOPIF 内元素需要滚动 | 抛错（子 session 的 objectId 顶层用不了），要求先滚到可见 |

## Shadow DOM

原生 `querySelector` **不穿透** shadow root。用：

```python
el = s.query_shadow("#deep-button")     # 递归进每个 open shadow root
s.click(el)                             # isTrusted=true
```

**为什么命中校验要走 composed 树**：`document.elementFromPoint` 在 shadow 内容上返回的是
**shadow host**，用普通的 `el.contains(hit)` 判定会**假阴性**——明明没被遮挡却判成被遮挡，
可点的元素被拒。所以命中判断沿 composed 树（`assignedSlot` / `ShadowRoot→host`）向上找，
`<input>` 的 UA-shadow 内部、slot 投影内容都能正确判命中。

**closed shadow root** 浏览器本身就不给访问，无解。

静态树也能穿透（用于批量只读）：

```python
dom = s.parse(pierce_shadow=True)
dom.query("#deep-button").text
```

## 已知边界

- **只支持单层 iframe**（父页里一个 `<iframe>`）。多层嵌套没做。
- OOPIF 内**不支持滚动**（见上）。
- `snapshot()` 里取不到的子 frame 当叶子处理，不崩、也不假装里面是空的。

---
下一步 → [[Snapshot与Ref]] · [[浏览器操作API]] · [[反检测实战与限制]]

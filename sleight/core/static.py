"""静态元素：把当前页面 HTML 取一次、解析成内存里的树，之后**零 CDP 往返**地批量查。

要读一堆元素的文本/属性（表格、列表、卡片流）时，实时 :class:`~sleight.core.element.Element`
每次查找/读属性都是一次或多次 CDP 往返，N 个元素就是 O(N) 次 WebSocket 往返。静态树把这
换成"**一次 ``content()`` + 之后纯内存查询**"，批量只读能快一个数量级。

和实时元素分工清楚：

- :class:`Element` / :class:`~sleight.core.snapshot.BackendElement`：实时、能点能输入、跟着
  页面变。
- :class:`StaticElement`：一次快照、只读、不随后续 JS 变更、**不能交互**。查到目标后要操作，
  再用它的 CSS 路径回到实时 :meth:`Session.query`。

零依赖：只用标准库 ``html.parser``，不引 lxml。支持常见 CSS 子集：类型 / ``#id`` / ``.class`` /
``*`` / ``[attr]`` ``[attr=v]`` ``[attr^=v]`` ``[attr$=v]`` ``[attr*=v]`` ``[attr~=v]`` / 复合
（``div.a#b[x=y]``）/ 后代（空格）/ 子（``>``）/ 并集（``,``）。比 DrissionPage 的静态树多一点：
:meth:`Session.parse` 的 ``pierce_shadow=True`` 会把 open Shadow DOM 内容也内联进来一起查。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["StaticElement", "parse_html"]

_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

#: HTML 的隐式闭合：开一个 key 标签时，若栈顶是 value 里的标签，先自动弹掉它。浏览器的
#: outerHTML 本来就是良构的，用不到这条；但直接喂原始/不规范 HTML 时它能救回常见结构。
_AUTO_CLOSE = {
    "li": {"li"},
    "p": {"p"},
    "option": {"option"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tbody": {"thead", "tbody"},
    "thead": {"tbody"},
    "dd": {"dd", "dt"},
    "dt": {"dd", "dt"},
}


class StaticElement:
    """内存树里的一个元素节点。只读。

    ``nodes`` 按**文档顺序**混放直属文本（``str``）与子元素（``StaticElement``），这样
    ``<p>some <b>bold</b> text</p>`` 的文本顺序才对（``some bold text``），不会把直属文本
    和子元素文本前后错开。
    """

    __slots__ = ("attrs", "nodes", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.nodes: list[str | StaticElement] = []
        self.parent: StaticElement | None = None

    def __repr__(self) -> str:
        ident = f"#{self.attrs['id']}" if self.attrs.get("id") else ""
        cls = f".{'.'.join(self.attrs['class'].split())}" if self.attrs.get("class") else ""
        return f"<StaticElement {self.tag}{ident}{cls}>"

    @property
    def children(self) -> list[StaticElement]:
        return [n for n in self.nodes if isinstance(n, StaticElement)]

    # —— 读 —— #

    def attr(self, name: str) -> str | None:
        """一个 HTML 属性值；没有返回 ``None``。"""
        return self.attrs.get(name)

    @property
    def text(self) -> str:
        """本元素及所有后代的可见文本（按文档顺序拼接，空白规整）。"""
        out: list[str] = []
        self._collect_text(out)
        return re.sub(r"\s+", " ", " ".join(out)).strip()

    def _collect_text(self, out: list[str]) -> None:
        for node in self.nodes:
            if isinstance(node, str):
                out.append(node)
            else:
                node._collect_text(out)

    @property
    def inner_text(self) -> str:
        """仅本元素**直属**的文本（不含后代元素里的）。"""
        parts = [n for n in self.nodes if isinstance(n, str)]
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def iter(self) -> list[StaticElement]:
        """本元素 + 所有后代（深度优先）。"""
        out = [self]
        for child in self.children:
            out.extend(child.iter())
        return out

    # —— 查 —— #

    def query(self, selector: str) -> StaticElement | None:
        """第一个匹配 ``selector`` 的后代；没有返回 ``None``。"""
        for el in _select(self, selector):
            return el
        return None

    def query_all(self, selector: str) -> list[StaticElement]:
        """所有匹配 ``selector`` 的后代（文档顺序）。"""
        return _select(self, selector)


# --------------------------------------------------------------------------- #
# 解析：标准库 html.parser -> StaticElement 树
# --------------------------------------------------------------------------- #


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = StaticElement("#document", {})
        self._stack: list[StaticElement] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        auto = _AUTO_CLOSE.get(tag)
        if auto:
            while len(self._stack) > 1 and self._stack[-1].tag in auto:
                self._stack.pop()
        el = StaticElement(tag, {k: (v or "") for k, v in attrs})
        el.parent = self._stack[-1]
        self._stack[-1].nodes.append(el)
        if tag not in _VOID:
            self._stack.append(el)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> 这类自闭合：只加节点，不入栈
        el = StaticElement(tag, {k: (v or "") for k, v in attrs})
        el.parent = self._stack[-1]
        self._stack[-1].nodes.append(el)

    def handle_endtag(self, tag: str) -> None:
        # 容忍不规范嵌套：从栈顶往下找同名标签，找到就弹到它
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip() and self._stack[-1].tag not in ("script", "style"):
            self._stack[-1].nodes.append(data)


def parse_html(html: str) -> StaticElement:
    """把一段 HTML 解析成 :class:`StaticElement` 根（``#document``）。"""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


# --------------------------------------------------------------------------- #
# CSS 子集选择器
# --------------------------------------------------------------------------- #

# 一个简单选择器：tag / #id / .class / * / [attr] / [attr op val]
_SIMPLE = re.compile(
    r"""
      \* |
      \#(?P<id>[\w\-]+) |
      \.(?P<cls>[\w\-]+) |
      \[(?P<attr>[\w\-]+)\s*(?:(?P<op>[~^$*]?=)\s*(?P<val>"[^"]*"|'[^']*'|[^\]]*))?\] |
      (?P<tag>[\w\-]+)
    """,
    re.VERBOSE,
)


def _parse_compound(text: str) -> list[tuple[str, str, str]]:
    """把 ``div.a#b[x=y]`` 拆成简单选择器条件列表 ``[(kind, a, b), ...]``。"""
    conds: list[tuple[str, str, str]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _SIMPLE.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f"bad selector near {text[pos:]!r}")
        pos = m.end()
        if m.group("id"):
            conds.append(("id", m.group("id"), ""))
        elif m.group("cls"):
            conds.append(("class", m.group("cls"), ""))
        elif m.group("attr"):
            val = (m.group("val") or "").strip("\"'")
            conds.append(("attr", m.group("attr"), (m.group("op") or "") + "\x00" + val))
        elif m.group("tag"):
            conds.append(("tag", m.group("tag").lower(), ""))
        # '*' -> 无条件
    return conds


def _match_compound(el: StaticElement, conds: list[tuple[str, str, str]]) -> bool:
    for kind, a, b in conds:
        if kind == "tag":
            if el.tag != a:
                return False
        elif kind == "id":
            if el.attrs.get("id") != a:
                return False
        elif kind == "class":
            if a not in (el.attrs.get("class") or "").split():
                return False
        elif kind == "attr":
            op, _, want = b.partition("\x00")
            if a not in el.attrs:
                return False
            if op:
                got = el.attrs.get(a) or ""
                if op == "=" and got != want:
                    return False
                if op == "^=" and not got.startswith(want):
                    return False
                if op == "$=" and not got.endswith(want):
                    return False
                if op == "*=" and want not in got:
                    return False
                if op == "~=" and want not in got.split():
                    return False
    return True


def _parse_sequence(part: str) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """把 ``a b > c`` 拆成 ``[(comb, compound_conds), ...]``，comb 是它**左侧**的组合符。"""
    tokens = re.split(r"\s*(>)\s*|\s+", part.strip())
    tokens = [t for t in tokens if t]                 # 去掉 split 产生的 None/空
    seq: list[tuple[str, list[tuple[str, str, str]]]] = []
    comb = " "                                         # 第一个相对根是后代
    for tok in tokens:
        if tok == ">":
            comb = ">"
            continue
        seq.append((comb, _parse_compound(tok)))
        comb = " "
    return seq


def _matches_sequence(el: StaticElement, seq: list[tuple[str, list[tuple[str, str, str]]]]) -> bool:
    comb, conds = seq[-1]
    if not _match_compound(el, conds):
        return False
    if len(seq) == 1:
        return True                                    # 最左段：可在任意位置
    rest = seq[:-1]
    if comb == ">":
        return el.parent is not None and _matches_sequence(el.parent, rest)
    anc = el.parent                                    # 后代：任一祖先满足即可
    while anc is not None and anc.tag != "#document":
        if _matches_sequence(anc, rest):
            return True
        anc = anc.parent
    return False


def _select(root: StaticElement, selector: str) -> list[StaticElement]:
    seqs = [_parse_sequence(part) for part in selector.split(",") if part.strip()]
    if not seqs:
        return []
    candidates = [e for e in root.iter() if e is not root and e.tag != "#document"]
    seen: set[int] = set()
    out: list[StaticElement] = []
    for el in candidates:
        if id(el) in seen:
            continue
        if any(_matches_sequence(el, seq) for seq in seqs):
            seen.add(id(el))
            out.append(el)
    return out

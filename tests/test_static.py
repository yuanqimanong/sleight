"""静态元素与 CSS 子集引擎：纯内存，不开浏览器。

覆盖解析（含不规范嵌套/void 元素/文本顺序）与 CSS 子集（类型/#id/.class/*/属性各算符/
复合/后代/子/并集）。都是纯 Python，跨平台、CI 无浏览器可跑。
"""

from __future__ import annotations

import pytest

from sleight.core.static import parse_html

_HTML = """<html><body>
<div id=main class="wrap box">
  <ul class=list>
    <li class=row data-k=1><a href=/a>Alpha</a></li>
    <li class=row data-k=2><a href=/b>Beta</a></li>
    <li class="row hot" data-k=3><a href=/c>Gamma</a></li>
  </ul>
  <input name=q value=hi>
  <p>some <b>bold</b> text</p>
</div>
<footer><a href=/x>foot</a></footer>
</body></html>"""


@pytest.fixture
def dom():
    return parse_html(_HTML)


def test_type_id_class_selectors(dom):
    assert len(dom.query_all("li")) == 3
    assert dom.query("#main").tag == "div"
    assert len(dom.query_all(".row")) == 3
    assert [e.text for e in dom.query_all("li.row.hot")] == ["Gamma"]


def test_attribute_operators(dom):
    assert dom.query("input[name=q]").attr("value") == "hi"
    assert len(dom.query_all("[data-k]")) == 3
    assert len(dom.query_all("[href^=/]")) == 4              # /a /b /c /x
    assert len(dom.query_all("[href$=b]")) == 1              # /b
    assert len(dom.query_all("[href*=x]")) == 1              # /x
    assert dom.query("li[data-k=2] a").text == "Beta"


def test_descendant_and_child_combinators(dom):
    assert [e.attr("href") for e in dom.query_all("#main a")] == ["/a", "/b", "/c"]  # 不含 footer
    assert len(dom.query_all("ul.list > li")) == 3          # 直接子
    assert len(dom.query_all("ul.list > a")) == 0           # a 不是 ul 的直接子
    assert dom.query("#main a[href=/x]") is None            # footer 的 a 不在 #main 里


def test_union_and_star(dom):
    assert len(dom.query_all("a, input")) == 5              # 4 a + 1 input
    assert len(dom.query_all("*[data-k]")) == 3


def test_text_is_in_document_order(dom):
    assert dom.query("p").text == "some bold text"          # 直属文本与子元素文本按序交织
    assert dom.query("p").inner_text == "some text"         # 仅直属文本
    assert dom.query("li.hot").text == "Gamma"


def test_void_elements_do_not_swallow_siblings():
    d = parse_html("<div><input id=a><span id=b>x</span></div>")
    assert d.query("#a").tag == "input"
    assert d.query("#b") is not None and d.query("#b").text == "x"


def test_tolerates_unclosed_tags():
    # <p> 没闭合就来下一个 <p>：不该把后面的都吞成子节点
    d = parse_html("<div><p>one<p>two</div>")
    assert [p.text for p in d.query_all("p")] == ["one", "two"]


def test_script_and_style_text_is_ignored():
    d = parse_html("<div>keep<script>var x=1;</script><style>.a{}</style></div>")
    assert d.query("div").text == "keep"


def test_bad_selector_raises():
    d = parse_html("<div></div>")
    with pytest.raises(ValueError, match="bad selector"):
        d.query_all("!!!")

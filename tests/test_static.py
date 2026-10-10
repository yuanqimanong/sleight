"""静态元素与 CSS 子集引擎：纯内存，不开浏览器。

覆盖解析（含不规范嵌套/void 元素/文本顺序）与 CSS 子集（类型/#id/.class/*/属性各算符/
复合/后代/子/并集）。都是纯 Python，跨平台、CI 无浏览器可跑。
"""

from __future__ import annotations

import pytest

from sleight.core.static import StaticElement as StaticElementType
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


# --------------------------------------------------------------------------- #
# XPath（默认安装包含 lxml 后端）
# --------------------------------------------------------------------------- #

def test_xpath_without_the_lxml_backend_says_how_to_enable_it():
    from sleight.core.errors import SleightError

    dom = parse_html(_HTML)                       # 零依赖那条
    with pytest.raises(SleightError, match="parse\\(xpath=True\\)"):
        dom.xpath("//li")


def test_xpath_returns_elements_and_string_values():
    dom = parse_html(_HTML, xpath=True)

    rows = dom.xpath("//li[@class='row']")
    assert len(rows) == 2                          # class="row hot" 不等于 'row'
    assert all(isinstance(r, StaticElementType) for r in rows)
    assert rows[0].text == "Alpha"

    texts = dom.xpath("//li//a/text()")            # text() -> str
    assert texts == ["Alpha", "Beta", "Gamma"]

    hrefs = dom.xpath("//a/@href")                 # @attr -> str
    assert hrefs == ["/a", "/b", "/c", "/x"]


def test_xpath_supports_what_the_css_subset_cannot():
    dom = parse_html(_HTML, xpath=True)
    # contains() + 位置谓词 + 轴 —— CSS 子集都表达不了
    assert len(dom.xpath("//li[contains(@class,'row')]")) == 3
    assert dom.xpath("//ul/li[2]")[0].text == "Beta"
    assert dom.xpath("//li[last()]")[0].text == "Gamma"
    assert dom.xpath("//input/preceding-sibling::ul")[0].tag == "ul"


def test_xpath_is_relative_to_the_element_it_is_called_on():
    dom = parse_html(_HTML, xpath=True)
    main = dom.query("#main")
    assert len(main.xpath(".//a")) == 3, "相对本元素：footer 的 a 不算"
    assert len(dom.xpath("//a")) == 4, "文档级：footer 的 a 也算"


def test_css_semantics_are_identical_on_both_backends():
    plain, lx = parse_html(_HTML), parse_html(_HTML, xpath=True)
    for selector in ("li.row", "#main a", "ul.list > li", "[href^=/]", "a, input", "li.row.hot"):
        assert len(plain.query_all(selector)) == len(lx.query_all(selector)), selector
        assert [e.text for e in plain.query_all(selector)] == \
               [e.text for e in lx.query_all(selector)], selector
    assert plain.query("p").text == lx.query("p").text == "some bold text"


def test_lxml_backed_tree_still_walks_parents_and_children():
    dom = parse_html(_HTML, xpath=True)
    row = dom.xpath("//li[@data-k='2']")[0]
    assert row.parent.tag == "ul"
    assert row.query("a").attr("href") == "/b"
    assert row.attr("data-k") == "2"

"""真浏览器下的正文/字段提取：文章页抽到正文+字段，链接页判低质量。

提取在页面内做，吃的是渲染后的 DOM —— 得在真 Chromium 上验证。没有浏览器时整个模块 skip。
"""

from __future__ import annotations

import pytest

from sleight.core.types import Text

from .conftest import serve_pages

pytestmark = pytest.mark.integration

_ARTICLE = """<!doctype html><html lang=en><head><title>Doc Title</title>
<meta name=description content="a short summary">
<meta property="og:title" content="OG Title">
<meta property="og:site_name" content="Example News">
<meta name=author content="Jane Doe">
<meta property="article:published_time" content="2026-08-10T00:00:00Z">
<script type="application/ld+json">{"@type":"Article","headline":"LD Headline"}</script>
</head><body>
<nav><a href=/a>Home</a> <a href=/b>Section</a> <a href=/c>More</a></nav>
<article><h1>The Heading</h1>
<p>First paragraph with several sentences. It has commas, and periods. Enough text to
score well above the threshold so the density heuristic picks this article body rather
than the navigation bar that is full of links.</p>
<p>Second paragraph continues the story with more sentences, more commas, and a good
amount of readable prose so the extractor clearly prefers it over everything else.</p>
</article>
<footer><a href=/x>x</a><a href=/y>y</a></footer></body></html>"""

_LINKY = (
    "<!doctype html><html lang=en><head><title>Links</title></head><body><div>"
    + " ".join(f'<a href=/{i}>link number {i}</a>' for i in range(40))
    + "</div></body></html>"
)


def test_extracts_metadata_jsonld_and_main_content(live_session):
    with serve_pages({"a.html": _ARTICLE}) as d:
        live_session.open(f"file://{d}/a.html", wait=Text("The Heading"))
        doc = live_session.extract_document()

        assert doc.title == "OG Title"                 # og:title 优先于 <title>
        assert doc.site_name == "Example News"
        assert doc.byline == "Jane Doe"
        assert doc.lang == "en"
        assert doc.excerpt == "a short summary"
        assert any(j.get("headline") == "LD Headline" for j in doc.json_ld)

        assert "First paragraph" in doc.text
        assert "Home" not in doc.text, "导航链接不该进正文"
        assert doc.quality > 0.3 and not doc.low_quality


def test_link_heavy_page_is_flagged_low_quality(live_session):
    with serve_pages({"l.html": _LINKY}) as d:
        live_session.open(f"file://{d}/l.html", wait=Text("link number 1"))
        doc = live_session.extract_document()
        assert doc.link_density > 0.5
        assert doc.low_quality, "全是链接的页面应判低质量，而不是假装抽到干净正文"


def test_outer_html_returns_only_that_subtree(live_session):
    """``outer_html()`` 的卖点是回传量，所以断言的是"明显比整页小"，
    不是"内容对" —— 后者 fake transport 已经锁住了。"""
    with serve_pages({"a.html": _ARTICLE}) as d:
        live_session.open(f"file://{d}/a.html", wait=Text("The Heading"))

        body = live_session.outer_html("article")
        assert body is not None
        assert body.startswith("<article>") and body.endswith("</article>")
        assert "First paragraph" in body
        assert "<nav>" not in body and "<footer>" not in body, "只该有这一棵子树"
        assert len(body) < len(live_session.content()) / 2

        assert live_session.outer_html("#nothing-here") is None

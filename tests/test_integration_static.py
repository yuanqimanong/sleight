"""真浏览器下的静态快查：一次取 HTML、内存里批量查；pierce_shadow 穿透 open shadow。

静态树的内容来自渲染后的真 DOM，且 pierce_shadow 依赖页面内序列化，得在真 Chromium 上验。
无浏览器时 skip。
"""

from __future__ import annotations

import pytest

from sleight.core.types import Text

from .conftest import serve_pages

pytestmark = pytest.mark.integration

_ROWS = "".join(
    f"<tr class=row><td class=name>Item {i}</td><td class=price>${i}0</td></tr>"
    for i in range(1, 51)
)
_TABLE_PAGE = f"<!doctype html><h1>Head</h1><table id=t>{_ROWS}</table>"

_SHADOW_PAGE = (
    "<!doctype html><h1>Head</h1><my-widget></my-widget>"
    "<script>customElements.define('my-widget', class extends HTMLElement{"
    "constructor(){super();this.attachShadow({mode:'open'}).innerHTML="
    "'<button id=shadowbtn>ShadowLabel</button>';}});</script>"
)


def test_static_bulk_read_matches_live(live_session):
    with serve_pages({"p.html": _TABLE_PAGE}) as d:
        live_session.open(f"file://{d}/p.html", wait=Text("Head"))

        dom = live_session.parse()
        names = [e.text for e in dom.query_all("tr.row td.name")]
        prices = [e.text for e in dom.query_all("tr.row td.price")]
        assert names[0] == "Item 1" and names[-1] == "Item 50"
        assert prices[-1] == "$500"
        # 和实时查找的数量一致
        assert len(names) == len(live_session.query_all("tr.row td.name"))


def test_parse_default_misses_shadow_but_pierce_finds_it(live_session):
    with serve_pages({"p.html": _SHADOW_PAGE}) as d:
        live_session.open(f"file://{d}/p.html", wait=Text("Head"))

        assert live_session.parse().query("#shadowbtn") is None, "默认静态树不含 shadow 内容"
        deep = live_session.parse(pierce_shadow=True)
        btn = deep.query("#shadowbtn")
        assert btn is not None and btn.text == "ShadowLabel", "pierce_shadow 应内联 shadow 内容"

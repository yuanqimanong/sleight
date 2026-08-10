"""真浏览器下的 Shadow DOM：穿透 open shadow root 定位并点中。

普通 ``querySelector`` 到不了 shadow 内的元素，而 ``document.elementFromPoint`` 在 shadow
内容上返回的是 host —— 老式 ``el.contains(hit)`` 命中校验会把可点的元素误判成"被遮挡"。
这两件事 fake transport 编不出来，必须上真 Chromium。没有浏览器时整个模块 skip。
"""

from __future__ import annotations

import pytest

from sleight.core.types import Text

from .conftest import serve_pages

pytestmark = pytest.mark.integration


_SHADOW_PAGE = """<!doctype html><title>S</title><h1>Shadow</h1>
<my-widget></my-widget>
<script>
customElements.define('my-widget', class extends HTMLElement {
  constructor() {
    super();
    const r = this.attachShadow({mode: 'open'});
    r.innerHTML = '<div style="padding:30px">'
      + '<button id=real style="padding:20px" '
      + 'onclick="window.__hit = event.isTrusted">Deep button</button></div>';
  }
});
</script>"""


def test_query_shadow_reaches_into_an_open_shadow_root(live_session):
    with serve_pages({"s.html": _SHADOW_PAGE}) as d:
        live_session.open(f"file://{d}/s.html", wait=Text("Shadow"))

        assert live_session.query("#real") is None, "普通 querySelector 不该穿透 shadow"
        el = live_session.query_shadow("#real")
        assert el is not None, "query_shadow 应能定位到 shadow 内的按钮"
        assert el.text() == "Deep button"


def test_click_lands_a_trusted_event_on_a_shadow_internal_button(live_session):
    """整条链路：穿透定位 → composed 命中校验 → 真实点击落在 shadow 内按钮上。"""
    with serve_pages({"s.html": _SHADOW_PAGE}) as d:
        live_session.open(f"file://{d}/s.html", wait=Text("Shadow"))
        el = live_session.query_shadow("#real")

        box = el.require_box()
        cx, cy = int(box.x + box.w / 2), int(box.y + box.h / 2)
        # 顶层 elementFromPoint 在按钮上返回的是 host，不是按钮 —— 正是老式命中校验的假阴性点
        top_hit = live_session.eval(
            f"(() => {{ const h = document.elementFromPoint({cx}, {cy});"
            f" return h ? h.tagName : null; }})()"
        )
        assert top_hit == "MY-WIDGET"
        assert el.hit_test(cx, cy) is True, "composed 命中校验应穿透 shadow 判定为命中"

        live_session.click(el)
        assert live_session.eval("window.__hit") is True, "shadow 内按钮没有收到可信点击"

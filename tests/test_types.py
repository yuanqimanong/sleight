"""核心数据类型与凭据脱敏。

这里的每条断言都对应一个「dataclass 默认行为会咬人」的地方：``frozen=True`` 不等于
不可变，默认 repr 会把 token 原样打进 traceback。
"""

from __future__ import annotations

import pytest

from sleight.core._redact import redact, redact_headers, redact_url
from sleight.core.types import (
    Box,
    DomReady,
    Endpoint,
    Gone,
    InstanceInfo,
    InstanceStatus,
    NetworkIdle,
    Point,
    Selector,
    Text,
)

TOKEN = "SECRET-abcdef0123456789"


# --------------------------------------------------------------------------- #
# 凭据不外泄
# --------------------------------------------------------------------------- #


def test_no_token_in_repr():
    """凭据绝不能出现在 repr 里。

    dataclass 的默认 repr 会把 ``Authorization: Bearer …`` 原样打进日志和 traceback，
    所以 headers 必须 ``repr=False``。
    """
    ep = Endpoint("http://x", "ws://x", {"Authorization": f"Bearer {TOKEN}"})
    assert TOKEN not in repr(ep)


def test_ws_url_stays_out_of_repr_too():
    """WS URL 可能整条就是凭据（query 里带 token 的 browserless 就是这样）。"""
    ep = Endpoint("http://x", f"ws://host/cdp?token={TOKEN}", {})
    assert TOKEN not in repr(ep)
    assert "http://x" in repr(ep)          # 非敏感字段还得看得见，否则没法排查


def test_redact_url_masks_query_credentials_and_userinfo():
    assert TOKEN not in redact_url(f"ws://host/cdp?token={TOKEN}")
    assert redact_url("http://user:pw@host/api") == "http://***@host/api"
    # 保留头尾各 3 个字符 —— 够人肉比对，不够复用
    assert "SEC" in redact_url(f"http://h/?api_key={TOKEN}")


def test_redact_masks_bearer_tokens_anywhere_in_a_message():
    assert TOKEN not in redact(f"403 from manager (Authorization: Bearer {TOKEN})")


def test_redact_headers_leaves_harmless_ones_alone():
    masked = redact_headers({"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
    assert TOKEN not in masked["Authorization"]
    assert masked["Accept"] == "application/json"


def test_short_secrets_are_fully_masked():
    """短 token 露头尾 3 个字符就等于露了一半。"""
    assert redact_url("http://h/?token=abcd") == "http://h/?token=***"


# --------------------------------------------------------------------------- #
# frozen ≠ 不可变
# --------------------------------------------------------------------------- #


def test_endpoint_headers_cannot_be_mutated_through_the_dataclass():
    ep = Endpoint("http://x", "ws://x", {"Authorization": "Bearer t"})
    with pytest.raises(TypeError):
        ep.headers["Authorization"] = "Bearer other"     # type: ignore[index]


def test_endpoint_copies_the_source_mapping():
    """冻结只阻止字段重新赋值 —— 不复制的话，调用方改自己那个 dict 就改到了我们。"""
    source = {"Authorization": "Bearer t"}
    ep = Endpoint("http://x", "ws://x", source)
    source["Authorization"] = "Bearer tampered"
    assert ep.headers["Authorization"] == "Bearer t"


def test_instance_labels_are_copied_and_frozen():
    source = {"region": "hk"}
    info = InstanceInfo(id="i1", provider="p", labels=source)
    source["region"] = "sg"
    assert info.labels["region"] == "hk"
    with pytest.raises(TypeError):
        info.labels["region"] = "sg"      # type: ignore[index]


def test_tags_are_normalised_to_a_frozenset():
    info = InstanceInfo(id="i1", provider="p", tags=["us", "us", "prod"])  # type: ignore[arg-type]
    assert info.tags == frozenset({"us", "prod"})


def test_header_list_is_the_shape_websocket_client_wants():
    ep = Endpoint("http://x", "ws://x", {"Authorization": "Bearer t"})
    assert ep.header_list() == ["Authorization: Bearer t"]


# --------------------------------------------------------------------------- #
# uid / 几何
# --------------------------------------------------------------------------- #


def test_uid_is_prefixed_by_provider():
    """多个 plain provider 的 ``"default"`` 会撞，租约 key 靠前缀区分。"""
    a = InstanceInfo(id="default", provider="local")
    b = InstanceInfo(id="default", provider="remote")
    assert a.uid == "local:default"
    assert a.uid != b.uid


def test_point_coordinates_are_forced_to_integers():
    """真实鼠标不产生小数坐标 —— 拿小数位当"抖动"是一眼可辨的特征。"""
    p = Point(10.7, -3.2)          # type: ignore[arg-type]
    assert (p.x, p.y) == (10, -3)
    assert isinstance(p.x, int) and isinstance(p.y, int)


def test_box_center_is_rounded_to_a_pixel():
    assert Box(10.0, 20.0, 6.0, 6.0).center == Point(13, 23)


def test_box_knows_when_it_is_unclickable():
    assert Box(0.0, 0.0, 0.0, 10.0).empty
    assert not Box(0.0, 0.0, 1.0, 1.0).empty


# --------------------------------------------------------------------------- #
# 等待条件
# --------------------------------------------------------------------------- #


def test_conditions_carry_their_kind_without_becoming_a_field():
    """``kind`` 是 ClassVar：做成字段的话带默认值的基类字段会挡住子类的必填字段。"""
    assert DomReady().kind == "domready"
    assert Text("Sign in").kind == "text"
    assert Selector("#app").value == "#app"
    assert Gone(".spinner").kind == "gone"
    assert NetworkIdle().quiet == 0.5


def test_condition_str_names_the_value_for_timeout_messages():
    assert str(Text("Sign in")) == "Text('Sign in')"
    assert str(DomReady()) == "DomReady"


def test_instance_status_is_a_str_enum():
    assert InstanceStatus.RUNNING == "running"
    assert InstanceStatus.NOT_FOUND == "not_found"

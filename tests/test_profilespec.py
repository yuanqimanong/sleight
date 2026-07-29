"""ProfileSpec 组装与指纹自洽校验。"""

from __future__ import annotations

import pytest

from sleight.providers.cloakbrowser import ProfileSpec


def test_preset_is_self_consistent():
    s = ProfileSpec.windows_us("Win-US-01")
    assert s.platform == "windows"
    assert s.timezone == "America/New_York" and s.locale == "en-US"
    assert "NVIDIA" in (s.gpu_renderer or "")
    assert "Apple" not in (s.gpu_renderer or "")


def test_macos_preset_never_gets_a_direct3d_string():
    s = ProfileSpec.macos_us("Mac-US-01")
    assert "Metal" in (s.gpu_renderer or "")
    assert "Direct3D" not in (s.gpu_renderer or "")


def test_platform_and_gpu_must_agree():
    with pytest.raises(ValueError, match="Apple/Metal"):
        ProfileSpec(
            name="bad",
            platform="windows",
            gpu_renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M2)",
        ).validate()


def test_geoip_conflicts_with_manual_timezone():
    with pytest.raises(ValueError, match="geoip"):
        ProfileSpec(
            name="bad", geoip=True, proxy="socks5://h:1", timezone="Asia/Tokyo"
        ).validate()


def test_geoip_without_proxy_has_nothing_to_derive_from():
    with pytest.raises(ValueError, match="nothing to derive"):
        ProfileSpec(name="bad", geoip=True).validate()


def test_geoip_preset_skips_timezone_and_locale():
    s = ProfileSpec.windows_us("g", geoip=True, proxy="socks5://u:p@h:1")
    assert s.timezone is None and s.locale is None


def test_user_agent_must_match_platform():
    with pytest.raises(ValueError, match="user_agent declares"):
        ProfileSpec(
            name="bad",
            platform="windows",
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        ).validate()


def test_uncommon_resolution_warns_but_does_not_fail():
    with pytest.warns(UserWarning, match="uncommon resolution"):
        ProfileSpec(name="odd", screen_width=1234, screen_height=567).validate()


def test_payload_shape_matches_the_manager_api():
    """字段名对齐 Manager 的 ProfileCreate；None 丢掉，tags 转 [{tag}]。"""
    s = ProfileSpec.windows_us("Win-US-02", proxy="socks5://u:p@hk:3000", tags=("us", "prod"))
    p = s.to_payload()

    assert p["name"] == "Win-US-02"
    assert p["proxy"] == "socks5://u:p@hk:3000"
    assert p["tags"] == [{"tag": "us"}, {"tag": "prod"}]
    assert p["auto_launch"] is False          # 默认不自动起，交给 ensure_ready
    assert "user_agent" not in p              # None 不进 body，让 Manager 用默认
    assert isinstance(p["launch_args"], list)


def test_viewport_height_matches_measured_offset():
    """viewport = screen_height − 133（Manager 实测：1080 → 947）。"""
    assert ProfileSpec("x").viewport_height == 947


def test_replace_returns_a_new_spec():
    a = ProfileSpec.windows_us("a")
    b = a.replace(name="b", headless=True)
    assert a.name == "a" and not a.headless
    assert b.name == "b" and b.headless

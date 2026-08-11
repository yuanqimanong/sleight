"""ProfileSpec 组装与指纹自洽校验。"""

from __future__ import annotations

import pytest

from sleight.providers.cloakbrowser import ProfileSpec


def test_preset_is_self_consistent():
    s = ProfileSpec.windows("Win-US-01")
    assert s.platform == "windows"
    assert s.timezone == "America/New_York" and s.locale == "en-US"
    assert "NVIDIA" in (s.gpu_renderer or "")
    assert "Apple" not in (s.gpu_renderer or "")


def test_macos_preset_never_gets_a_direct3d_string():
    s = ProfileSpec.macos("Mac-US-01")
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
    s = ProfileSpec.windows("g", geoip=True, proxy="socks5://u:p@h:1")
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
    s = ProfileSpec.windows("Win-US-02", proxy="socks5://u:p@hk:3000", tags=("us", "prod"))
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
    a = ProfileSpec.windows("a")
    b = a.replace(name="b", headless=True)
    assert a.name == "a" and not a.headless
    assert b.name == "b" and b.headless


# --------------------------------------------------------------------------- #
# 平台 × 地区（3 个平台工厂 + Region 枚举）
# --------------------------------------------------------------------------- #


def test_region_locks_timezone_and_locale_together():
    from sleight.providers.cloakbrowser import Region

    hk = ProfileSpec.windows("hk", Region.HK)
    assert (hk.timezone, hk.locale) == ("Asia/Hong_Kong", "zh-HK")
    jp = ProfileSpec.linux("jp", Region.JP)
    assert (jp.timezone, jp.locale) == ("Asia/Tokyo", "ja-JP")
    # 同一地区换平台，时区/语言不变，变的是平台与 GPU
    assert ProfileSpec.macos("m", Region.HK).timezone == "Asia/Hong_Kong"


def test_platform_decides_the_gpu_not_the_region():
    from sleight.providers.cloakbrowser import Region

    assert "NVIDIA" in ProfileSpec.windows("a", Region.HK).gpu_renderer
    assert "Apple" in ProfileSpec.macos("b", Region.HK).gpu_renderer
    assert "Mesa" in ProfileSpec.linux("c", Region.HK).gpu_renderer


def test_gpu_can_be_overridden_within_the_platform():
    assert "Intel" in ProfileSpec.windows("a", gpu="windows-intel").gpu_renderer


def test_cross_platform_gpu_is_rejected():
    with pytest.raises(ValueError, match="not a windows GPU"):
        ProfileSpec.windows("a", gpu="macos-apple")


def test_unknown_region_is_rejected():
    with pytest.raises(ValueError, match="unknown region"):
        ProfileSpec.windows("a", "atlantis")


def test_every_region_produces_a_self_consistent_spec():
    from sleight.providers.cloakbrowser import Region

    for region in Region:
        for factory in (ProfileSpec.windows, ProfileSpec.macos, ProfileSpec.linux):
            spec = factory("x", region)          # 工厂内部会 validate()，矛盾组合会抛
            assert spec.timezone == region.timezone and spec.locale == region.locale
            assert region.label                   # 每个地区都有中文标签给 UI 用


def test_geoip_still_leaves_timezone_and_locale_to_the_proxy():
    from sleight.providers.cloakbrowser import Region

    s = ProfileSpec.windows("g", Region.HK, geoip=True, proxy="socks5://u:p@h:1")
    assert s.timezone is None and s.locale is None

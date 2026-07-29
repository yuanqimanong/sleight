"""端到端验证 —— 需要真 CloakBrowser Manager。

    set SLEIGHT_CLOAK_BASE=http://127.0.0.1:19000
    set SLEIGHT_CLOAK_TOKEN=...
    pytest -m manager

验收站点用 TransparencyUSA：直连 403、走真实浏览器正常，是天然的用例。
"""

from __future__ import annotations

import os

import pytest

from sleight import NetworkIdle, Text
from sleight.core.types import InstanceStatus
from sleight.providers import CloakBrowserManager, ProfileSpec

pytestmark = pytest.mark.manager

BASE = os.environ.get("SLEIGHT_CLOAK_BASE", "http://127.0.0.1:19000")
TARGET = "https://www.transparencyusa.org/oh/race/attorney-general-of-ohio-76804"


@pytest.fixture(scope="module")
def mgr() -> CloakBrowserManager:
    if not os.environ.get("SLEIGHT_CLOAK_TOKEN"):
        pytest.skip("SLEIGHT_CLOAK_TOKEN not set")
    return CloakBrowserManager(BASE)


def test_discovery(mgr: CloakBrowserManager):
    instances = mgr.list_instances()
    assert instances, "manager reports no profiles"
    assert all(i.provider == "cloakbrowser" for i in instances)
    assert all(i.uid.startswith("cloakbrowser:") for i in instances)

    system = mgr.system_status()
    assert system["profiles_total"] >= len(instances)


def test_status_is_the_only_source_of_not_found(mgr: CloakBrowserManager):
    """stop 的 404 分不清"已停止"和"不存在"，只有 status 能。"""
    assert mgr.status("00000000-0000-0000-0000-000000000000") is InstanceStatus.NOT_FOUND
    running = [i for i in mgr.list_instances() if i.ready]
    if running:
        assert mgr.status(running[0].id) is InstanceStatus.RUNNING


def test_ws_url_is_built_from_our_base_not_the_managers(mgr: CloakBrowserManager):
    """Manager 把 webSocketDebuggerUrl 的 host 写死成 127.0.0.1:19000。"""
    ep = mgr.endpoint("abc")
    assert ep.ws_url == f"{BASE.replace('http://', 'ws://')}/api/profiles/abc/cdp"
    assert ep.headers["Authorization"].startswith("Bearer ")


#: Cloudflare / DataDome 之类的人机验证插页特征
CHALLENGE_MARKERS = ("Just a moment", "Performing security verification", "Checking your browser")


def test_end_to_end_navigation(mgr: CloakBrowserManager):
    """真实导航 + 自有 target 的生命周期。

    用一个无聊到不会变的站点。断言外部网站的**内容**是脆的 —— 见
    :func:`test_acceptance_site_still_reachable`。
    """
    running = [i for i in mgr.list_instances() if i.ready]
    if not running:
        pytest.skip("no running profile")

    with mgr.lease(instance_id=running[0].id) as inst:
        before = {t["targetId"] for t in inst.targets()}

        with inst.session() as s:
            assert s.owned_target                       # 自建 tab，不接管别人的
            assert s.target_id not in before

            s.open("https://example.com", wait=Text("Example Domain"), timeout=60)
            s.wait(NetworkIdle(), timeout=30)

            assert "example.com" in s.url()
            assert "Example Domain" in s.title()
            assert len(s.content()) > 500
            closed_target = s.target_id

        after = {t["targetId"] for t in inst.targets()}
        assert closed_target not in after, "owned target must be closed on exit"
        assert before <= after, "we must not have closed anyone else's tab"


def test_acceptance_site_still_reachable(mgr: CloakBrowserManager):
    """外部验收站点：当初入选是因为直连 403、走真实浏览器正常。

    2026-07 复测：它已经加了 Cloudflare 人机验证插页，走浏览器也会被拦。挡在这里
    的是**指纹和信誉**，不是行为 —— 而指纹是浏览器的事，sleight 明确不碰。
    所以这条遇到验证页就 skip，不当成回归。
    """
    running = [i for i in mgr.list_instances() if i.ready]
    if not running:
        pytest.skip("no running profile")

    with mgr.lease(instance_id=running[0].id) as inst, inst.session() as s:
        s.open(TARGET, timeout=90)
        text = s.text()
        if any(marker in text for marker in CHALLENGE_MARKERS):
            pytest.skip(f"{TARGET} now serves a bot challenge: {text.splitlines()[1:2]}")
        assert "Attorney General" in text
        assert len(s.content()) > 10_000


def test_profile_spec_round_trip(mgr: CloakBrowserManager):
    """建 → 查 → 幂等 ensure → 删。用 auto_launch=False，不打扰在跑的实例。"""
    spec = ProfileSpec.windows_us("sleight-spec-test", proxy=None, tags=("sleight-test",))
    created = mgr.create_profile(spec)
    try:
        assert created.name == "sleight-spec-test"
        assert "sleight-test" in created.tags
        assert mgr.status(created.id) is InstanceStatus.STOPPED

        again = mgr.ensure_profile(spec)                # 幂等
        assert again.id == created.id

        raw = mgr.get_profile(created.id)
        assert raw["timezone"] == "America/New_York"
        assert raw["platform"] == "windows"
        assert "NVIDIA" in (raw["gpu_renderer"] or "")
    finally:
        mgr.delete_profile(created.id, force=True)
    assert mgr.status(created.id) is InstanceStatus.NOT_FOUND

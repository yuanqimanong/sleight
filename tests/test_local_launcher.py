"""LocalLauncher：起本机浏览器进程再当 CDP 端点驱动。

分两层：
- 不开浏览器的单测——参数拼装、binary 解析、未启动时的状态、close 幂等。用 sys.executable
  当"占位 binary"（只解析路径、拼参数，**从不真的 spawn**），所以跨平台、CI 无浏览器也能跑。
- 真浏览器集成——launch() / LocalLauncher+lease / recover，标 integration，无 Chromium 时 skip。
"""

from __future__ import annotations

import sys

import pytest

from sleight.core.errors import InstanceError, NotFound
from sleight.core.types import InstanceStatus, Text
from sleight.providers.local import LocalLauncher

# --------------------------------------------------------------------------- #
# 单测：不 spawn，只看参数与状态机
# --------------------------------------------------------------------------- #


def test_build_args_includes_the_fingerprint_and_debugging_port():
    prov = LocalLauncher(sys.executable, fingerprint=42, no_sandbox=True)
    prov._port = 9333          # 跳过自动挑端口，直接看拼装
    prov._profile_dir = "/tmp/x"
    args = prov._build_args()
    assert args[0] == sys.executable
    assert "--remote-debugging-port=9333" in args
    assert "--user-data-dir=/tmp/x" in args
    assert "--fingerprint=42" in args
    assert "--headless=new" in args
    assert "--no-sandbox" in args
    assert args[-1] == "about:blank"


def test_build_args_omits_fingerprint_and_headless_when_not_requested():
    prov = LocalLauncher(sys.executable, headless=False)
    prov._port = 9333
    prov._profile_dir = "/tmp/x"
    args = prov._build_args()
    assert not any(a.startswith("--fingerprint") for a in args), "没给 seed 就不该传"
    assert "--headless=new" not in args
    assert "--no-sandbox" not in args


def test_extra_args_are_appended_before_the_url():
    prov = LocalLauncher(sys.executable, args=["--proxy-server=socks5://127.0.0.1:9050"])
    prov._port = 9333
    prov._profile_dir = "/tmp/x"
    args = prov._build_args()
    assert "--proxy-server=socks5://127.0.0.1:9050" in args
    assert args.index("--proxy-server=socks5://127.0.0.1:9050") < args.index("about:blank")


def test_unknown_binary_fails_clearly():
    with pytest.raises(InstanceError, match="not found"):
        LocalLauncher("definitely-not-a-real-browser-xyz")


def test_status_is_stopped_before_launch_and_id_is_validated():
    prov = LocalLauncher(sys.executable)
    assert prov.status("default") is InstanceStatus.STOPPED
    assert prov.status("other") is InstanceStatus.NOT_FOUND
    with pytest.raises(NotFound):
        prov.endpoint("other")


def test_endpoint_before_start_is_an_error():
    prov = LocalLauncher(sys.executable)
    with pytest.raises(InstanceError, match="not started"):
        prov.endpoint()


def test_close_is_idempotent_without_a_process():
    prov = LocalLauncher(sys.executable)
    prov.close()
    prov.close()          # 不抛
    with pytest.raises(InstanceError, match="closed"):
        prov.ensure_ready("default")


# --------------------------------------------------------------------------- #
# 集成：真 Chromium
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_launch_starts_drives_and_cleans_up(chromium_path):
    from sleight import launch

    with launch(chromium_path, no_sandbox=True) as s:
        s.open("data:text/html,<title>Hi</title><h1>launched</h1>", wait=Text("launched"))
        assert s.title() == "Hi"


@pytest.mark.integration
def test_local_launcher_with_pool_lease(chromium_path):
    prov = LocalLauncher(chromium_path, no_sandbox=True)
    with prov:
        with prov.lease() as inst, inst.session() as s:
            s.open("data:text/html,<title>P</title><h1>pool</h1>", wait=Text("pool"))
            assert s.title() == "P"
        assert prov.status("default") is InstanceStatus.RUNNING
    # 上下文退出后进程被停掉
    assert prov.status("default") is InstanceStatus.STOPPED


@pytest.mark.integration
def test_recover_restarts_the_browser(chromium_path):
    prov = LocalLauncher(chromium_path, no_sandbox=True)
    with prov:
        prov.ensure_ready("default")
        assert prov.status("default") is InstanceStatus.RUNNING
        prov.recover("default")
        assert prov.status("default") is InstanceStatus.RUNNING

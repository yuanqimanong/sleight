"""插件与 profile 运维。

``merge_launch_args`` 是这一层的心脏，所以它单独被按每一种情况测了一遍：整字段替换的
API 上，"保留其它参数"和"两个扩展参数必须成对"都是一错就静默失效的东西。
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from sleight.core.errors import NotFound
from sleight.core.types import InstanceStatus
from sleight.deploy.engine import Deployer
from sleight.deploy.errors import DeployError
from sleight.deploy.ops import (
    ExtensionOps,
    ProfileOps,
    extension_ids,
    extension_paths,
    merge_launch_args,
)
from sleight.deploy.spec import DeploySpec

from .conftest import FakeRunner, docker_ok

SPEC = DeploySpec(dir="/srv/cbm")
BPC = "/data/extensions/bypass-paywalls-chrome-clean-master"
ADG = "/data/extensions/adguard-adblocker"
MANIFEST = '{"name": "Bypass Paywalls Clean", "version": "4.4.1.5", "manifest_version": 3}'


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def test_two_flags_always_come_as_a_pair():
    """只给 --load-extension，Chromium 会把它指定的插件也一并禁掉。"""
    args = merge_launch_args([], [BPC])
    assert args == [f"--disable-extensions-except={BPC}", f"--load-extension={BPC}"]


def test_multiple_paths_are_comma_joined_identically_in_both_flags():
    args = merge_launch_args([], [BPC, ADG])
    values = [a.split("=", 1)[1] for a in args]
    assert values[0] == values[1] == f"{BPC},{ADG}"


def test_unrelated_launch_args_are_preserved():
    """在 Manager UI 里手调过的参数不能因为改插件就被抹掉。"""
    current = ["--lang=en-US", f"--load-extension={ADG}", "--proxy-bypass-list=<local>"]
    args = merge_launch_args(current, [BPC])
    assert args[:2] == ["--lang=en-US", "--proxy-bypass-list=<local>"]
    assert extension_paths(args) == [BPC]


def test_adding_one_extension_keeps_the_old_one():
    """PUT 是整字段替换：手工加一个插件最容易把旧的摘掉。"""
    current = merge_launch_args([], [BPC])
    args = merge_launch_args(current, [BPC, ADG])
    assert extension_paths(args) == [BPC, ADG]


def test_empty_list_removes_the_extension_flags_only():
    current = ["--lang=en-US", *merge_launch_args([], [BPC])]
    assert merge_launch_args(current, []) == ["--lang=en-US"]


def test_merge_is_idempotent():
    once = merge_launch_args(["--lang=x"], [BPC, ADG])
    assert merge_launch_args(once, [BPC, ADG]) == once


def test_bare_flags_without_values_are_dropped_too():
    assert merge_launch_args(["--load-extension", "--lang=x"], []) == ["--lang=x"]


def test_extension_paths_reads_load_extension_not_the_whitelist():
    """``--disable-extensions-except`` 只是白名单，真正加载的是 ``--load-extension``。"""
    args = [f"--disable-extensions-except={BPC},{ADG}", f"--load-extension={BPC}"]
    assert extension_paths(args) == [BPC]


def test_extension_paths_tolerates_spaces_and_empties():
    assert extension_paths([f"--load-extension= {BPC} ,,{ADG}"]) == [BPC, ADG]


def test_extension_paths_of_nothing():
    assert extension_paths([]) == []
    assert extension_paths(["--lang=en-US"]) == []


def test_extension_ids_come_from_the_target_list():
    targets = [
        {"type": "page", "url": "https://example.com"},
        {"type": "service_worker", "url": f"chrome-extension://{'a' * 32}/background.js"},
        {"webSocketDebuggerUrl": "ws://x/devtools/page/1", "url": f"chrome-extension://{'b' * 32}/"},
    ]
    assert extension_ids(targets) == {"a" * 32, "b" * 32}


def test_extension_ids_ignores_things_that_are_not_ids():
    assert extension_ids([{"url": "chrome-extension://short/x"}]) == set()
    assert extension_ids([]) == set()


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #


class FakeManager:
    """够 ops 用的最小 Manager 替身。记下每一次 PUT 和 stop。"""

    name = "fake"

    def __init__(self, profiles: list[dict], targets: dict[str, list[dict]] | None = None) -> None:
        self.profiles = profiles
        self.targets = targets or {}
        self.updates: list[tuple[str, dict]] = []
        self.stops: list[str] = []
        self.launched: list[str] = []

    def list_profiles(self) -> list[dict]:
        return [dict(p) for p in self.profiles]

    def _get(self, pid: str) -> dict:
        for p in self.profiles:
            if p["id"] == pid:
                return p
        raise NotFound(pid)

    def update_profile(self, pid: str, **changes):
        self.updates.append((pid, changes))
        self._get(pid).update(changes)

    def stop(self, pid: str) -> None:
        self.stops.append(pid)
        self._get(pid)["status"] = "stopped"

    def ensure_ready(self, pid: str) -> None:
        self.launched.append(pid)
        self._get(pid)["status"] = "running"

    def status(self, pid: str) -> InstanceStatus:
        try:
            raw = self._get(pid)
        except NotFound:
            return InstanceStatus.NOT_FOUND
        return InstanceStatus.RUNNING if raw["status"] == "running" else InstanceStatus.STOPPED

    def find_profile(self, name: str):
        return next((p for p in self.profiles if p.get("name") == name), None)

    def cdp_targets(self, pid: str) -> list[dict]:
        return self.targets.get(pid, [])


def profiles(*specs: tuple[str, str, list[str]]) -> list[dict]:
    return [
        {"id": pid, "name": name, "status": status, "launch_args": list(args), "tags": []}
        for pid, name, status, args in [
            (s[0], s[0].upper(), s[1], s[2]) for s in specs
        ]
    ]


def setup(
    *,
    installed: dict[str, str] | None = None,
    mgr_profiles: list[dict] | None = None,
    targets: dict[str, list[dict]] | None = None,
    replies: dict | None = None,
    **kw,
) -> tuple[ExtensionOps, FakeRunner, FakeManager]:
    """一台装好了插件、Manager 上有若干 profile 的机器。"""
    files = {
        f"{SPEC.extensions_dir}/{name}/manifest.json": body
        for name, body in (installed or {}).items()
    }
    runner = FakeRunner(
        files=files,
        dirs=("/", "/srv", SPEC.dir, SPEC.data_dir, SPEC.extensions_dir,
              *[f"{SPEC.extensions_dir}/{n}" for n in (installed or {})]),
        replies={**docker_ok(), "docker exec": (0, MANIFEST), **(replies or {})},
    )
    dep = Deployer(SPEC, runner, **kw)
    mgr = FakeManager(mgr_profiles if mgr_profiles is not None else [], targets)

    @contextmanager
    def connect(**_kw):
        yield mgr

    dep.connect = connect
    return ExtensionOps(dep), runner, mgr


# --------------------------------------------------------------------------- #
# 看
# --------------------------------------------------------------------------- #


def test_list_installed_reads_the_manifest_and_counts_files():
    ops, _, _ = setup(installed={"bpc": MANIFEST})
    (ext,) = ops.list_installed()
    assert ext.dirname == "bpc"
    assert ext.name == "Bypass Paywalls Clean"
    assert ext.version == "4.4.1.5"
    assert ext.manifest_version == 3
    assert ext.files == 1
    assert ext.container_path == "/data/extensions/bpc"
    assert ext.problems == []


def test_container_path_is_not_the_host_path():
    """填错这个 Chromium 会静默不加载。"""
    ops, _, _ = setup(installed={"bpc": MANIFEST})
    (ext,) = ops.list_installed()
    assert ext.container_path == "/data/extensions/bpc"
    assert ext.container_path != f"{SPEC.extensions_dir}/bpc"


def test_mv2_is_reported_as_a_problem():
    ops, _, _ = setup(installed={"old": '{"name": "Old", "manifest_version": 2}'})
    (ext,) = ops.list_installed()
    assert not ext.mv3
    assert "MV2" in ext.problems[0]


def test_unreadable_manifest_is_reported():
    ops, _, _ = setup(installed={"half": "not json"})
    (ext,) = ops.list_installed()
    assert "manifest.json" in ext.problems[0]


def test_root_only_permissions_are_reported():
    """容器里的浏览器进程不是 root（实测 uid 1001），root 独占的目录它读不到。"""
    ops, runner, _ = setup(installed={"bpc": MANIFEST})
    runner.unreadable_entries = 7
    (ext,) = ops.list_installed()
    assert ext.unreadable == 7
    assert "chmod -R a+rX" in ext.problems[0]


def test_no_extensions_dir_is_an_empty_list_not_a_crash():
    ops, _, _ = setup()
    assert ops.list_installed() == []


# --------------------------------------------------------------------------- #
# 推
# --------------------------------------------------------------------------- #


def local_ext(tmp_path, *, mv=3, name="bpc"):
    root = tmp_path / name
    (root / "js").mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"name": "X", "version": "1", "manifest_version": mv}))
    (root / "js" / "bg.js").write_text("//")
    return root


def test_push_refuses_mv2_before_transferring_anything(tmp_path):
    """传一个 Chromium 146 不会加载的插件毫无意义，所以在本地就拦掉。"""
    ops, runner, _ = setup()
    with pytest.raises(DeployError, match="MV3"):
        ops.push(local_ext(tmp_path, mv=2))
    assert runner.pushed == []


def test_push_refuses_a_directory_without_a_manifest(tmp_path):
    ops, _, _ = setup()
    (tmp_path / "empty").mkdir()
    with pytest.raises(DeployError, match=r"manifest\.json"):
        ops.push(tmp_path / "empty")


def test_push_transfers_then_fixes_permissions(tmp_path):
    ops, runner, _ = setup()
    src = local_ext(tmp_path)
    ext = ops.push(src)
    assert runner.pushed == [(str(src), f"{SPEC.extensions_dir}/bpc")]
    assert runner.ran("chmod", "-R", "a+rX", SPEC.extensions_dir)
    assert ext.manifest_version == 3
    assert ext.files == 2


def test_push_confirms_the_container_can_actually_read_it(tmp_path):
    """挂载写错、权限没修，症状都是"文件在但浏览器看不到"。"""
    ops, _runner, _ = setup(replies={"docker exec": (1, "No such file or directory")})
    with pytest.raises(DeployError, match="cannot read"):
        ops.push(local_ext(tmp_path))


def test_push_can_rename(tmp_path):
    ops, _runner, _ = setup()
    ext = ops.push(local_ext(tmp_path), name="paywalls")
    assert ext.dirname == "paywalls"
    assert ext.container_path == "/data/extensions/paywalls"


def test_push_under_dry_run_transfers_nothing(tmp_path):
    ops, runner, _ = setup(dry_run=True)
    ops.push(local_ext(tmp_path))
    assert runner.pushed == []


def test_remove_needs_the_directory_to_exist():
    ops, _, _ = setup()
    with pytest.raises(NotFound):
        ops.remove("nope")


def test_remove_deletes_the_directory():
    ops, runner, _ = setup(installed={"bpc": MANIFEST})
    ops.remove("bpc")
    assert runner.ran("rm", "-rf", f"{SPEC.extensions_dir}/bpc")


# --------------------------------------------------------------------------- #
# 下发
# --------------------------------------------------------------------------- #


def test_apply_hits_every_profile():
    """漏掉一个就意味着有一定概率租到没插件的那台。"""
    ops, _, mgr = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "running", []), ("p2", "stopped", []), ("p3", "stopped", [])),
    )
    changes = ops.apply()
    assert len(changes) == 3
    assert {pid for pid, _ in mgr.updates} == {"p1", "p2", "p3"}
    for _, payload in mgr.updates:
        assert extension_paths(payload["launch_args"]) == ["/data/extensions/bpc"]


def test_apply_stops_only_the_running_ones():
    """参数只在浏览器进程启动时读，所以在跑的必须停一次。"""
    ops, _, mgr = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "running", []), ("p2", "stopped", [])),
    )
    changes = ops.apply()
    assert mgr.stops == ["p1"]
    assert [c.stopped for c in changes] == [True, False]


def test_apply_does_not_touch_a_profile_that_is_already_right():
    ops, _, mgr = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "running", merge_launch_args([], ["/data/extensions/bpc"]))),
    )
    changes = ops.apply()
    assert mgr.updates == []
    assert mgr.stops == []
    assert not changes[0].updated


def test_apply_no_restart_leaves_running_instances_alone():
    ops, _, mgr = setup(
        installed={"bpc": MANIFEST}, mgr_profiles=profiles(("p1", "running", []))
    )
    ops.apply(restart=False)
    assert mgr.updates and mgr.stops == []


def test_apply_only_selects_a_subset_in_order():
    ops, _, mgr = setup(
        installed={"adguard": MANIFEST, "bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "stopped", [])),
    )
    ops.apply(names=["bpc", "adguard"])
    (_, payload) = mgr.updates[0]
    assert extension_paths(payload["launch_args"]) == [
        "/data/extensions/bpc", "/data/extensions/adguard"
    ]


def test_apply_uses_every_installed_extension_by_default():
    ops, _, mgr = setup(
        installed={"adguard": MANIFEST, "bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "stopped", [])),
    )
    ops.apply()
    (_, payload) = mgr.updates[0]
    assert len(extension_paths(payload["launch_args"])) == 2


def test_apply_refuses_a_name_that_is_not_installed():
    ops, _, mgr = setup(installed={"bpc": MANIFEST}, mgr_profiles=profiles(("p1", "stopped", [])))
    with pytest.raises(DeployError, match="push them first"):
        ops.apply(names=["typo"])
    assert mgr.updates == []


def test_apply_with_no_profiles_is_not_an_error():
    ops, _, _mgr = setup(installed={"bpc": MANIFEST})
    assert ops.apply() == []


def test_apply_under_dry_run_sends_no_put():
    ops, _, mgr = setup(
        installed={"bpc": MANIFEST}, mgr_profiles=profiles(("p1", "running", [])), dry_run=True
    )
    changes = ops.apply()
    assert changes[0].updated                    # 会说要改
    assert mgr.updates == [] and mgr.stops == []  # 但什么也没发


def test_change_summary_names_what_moved():
    ops, _, _ = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "running", merge_launch_args([], [ADG]))),
    )
    (change,) = ops.apply()
    assert "+bpc" in change.summary
    assert "-adguard-adblocker" in change.summary


# --------------------------------------------------------------------------- #
# 验
# --------------------------------------------------------------------------- #


def loaded_targets(n: int) -> list[dict]:
    return [
        {"url": f"chrome-extension://{chr(ord('a') + i) * 32}/background.js"} for i in range(n)
    ]


def test_verify_confirms_the_browser_really_loaded_them():
    ops, _, _mgr = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "running", merge_launch_args([], [BPC]))),
        targets={"p1": loaded_targets(1)},
    )
    (report,) = ops.verify()
    assert report.ok
    assert len(report.loaded) == 1
    assert "1/1" in report.summary


def test_verify_fails_loudly_when_nothing_loaded():
    """这就是"文件在、参数对、但浏览器里没有"那种情况。"""
    ops, _, _ = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "running", merge_launch_args([], [BPC]))),
        targets={},
    )
    (report,) = ops.verify(settle=0.0)
    assert not report.ok
    assert "0/1" in report.summary


def test_verify_launches_stopped_instances():
    ops, _, mgr = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "stopped", merge_launch_args([], [BPC]))),
        targets={"p1": loaded_targets(1)},
    )
    ops.verify()
    assert mgr.launched == ["p1"]


def test_verify_can_be_told_not_to_launch():
    ops, _, mgr = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "stopped", merge_launch_args([], [BPC]))),
    )
    (report,) = ops.verify(launch=False)
    assert mgr.launched == []
    assert not report.running
    assert "无法验证" in report.summary


def test_a_profile_with_no_extensions_configured_is_not_a_failure():
    ops, _, _ = setup(installed={"bpc": MANIFEST}, mgr_profiles=profiles(("p1", "running", [])))
    (report,) = ops.verify()
    assert report.expected == 0
    assert "没配置扩展" in report.summary


# --------------------------------------------------------------------------- #
# 漂移
# --------------------------------------------------------------------------- #


def test_drift_finds_the_profile_that_was_missed():
    """症状是"有时候能过付费墙，有时候不能" —— 只改了一部分 profile。"""
    ops, _, _ = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(
            ("p1", "running", merge_launch_args([], ["/data/extensions/bpc"])),
            ("p2", "running", []),
        ),
    )
    report = ops.drift()
    assert not report["consistent"]
    assert report["profiles"][0]["missing"] == []
    assert report["profiles"][1]["missing"] == ["/data/extensions/bpc"]


def test_drift_is_consistent_when_everything_matches():
    args = merge_launch_args([], ["/data/extensions/bpc"])
    ops, _, _ = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(("p1", "running", args), ("p2", "stopped", args)),
    )
    assert ops.drift()["consistent"]


def test_drift_flags_paths_that_point_at_nothing():
    """插件目录被删了，但 profile 还配着它 —— Chromium 会因此启动失败或忽略。"""
    ops, _, _ = setup(
        installed={"bpc": MANIFEST},
        mgr_profiles=profiles(
            ("p1", "running", merge_launch_args([], ["/data/extensions/bpc", "/data/extensions/gone"]))
        ),
    )
    report = ops.drift()
    assert report["profiles"][0]["unknown"] == ["/data/extensions/gone"]
    assert not report["consistent"]


def test_drift_is_json_serialisable():
    """Web 界面直接把它丢给前端。"""
    ops, _, _ = setup(installed={"bpc": MANIFEST}, mgr_profiles=profiles(("p1", "running", [])))
    json.dumps(ops.drift())


# --------------------------------------------------------------------------- #
# profile 运维
# --------------------------------------------------------------------------- #


def _profile_ops(**kw) -> tuple[ProfileOps, FakeManager]:
    ops, _, mgr = setup(**kw)
    return ProfileOps(ops.dep), mgr


def test_stop_all_only_touches_running_instances():
    ops, mgr = _profile_ops(
        mgr_profiles=profiles(("p1", "running", []), ("p2", "stopped", []), ("p3", "running", []))
    )
    assert ops.stop_all() == ["p1", "p3"]
    assert mgr.stops == ["p1", "p3"]


def test_launch_and_stop_accept_a_name_as_well_as_an_id():
    ops, mgr = _profile_ops(mgr_profiles=profiles(("p1", "stopped", [])))
    assert ops.launch("P1") == "p1"                  # profiles() 把名字设成大写 id
    assert mgr.launched == ["p1"]
    assert ops.stop("p1") == "p1"


def test_an_unknown_profile_is_a_not_found():
    ops, _ = _profile_ops(mgr_profiles=profiles(("p1", "running", [])))
    with pytest.raises(NotFound, match="id or name"):
        ops.stop("typo")


def test_list_returns_raw_fields_including_launch_args():
    """InstanceInfo 只留驱动层要的四个字段，运维要看 launch_args。"""
    ops, _ = _profile_ops(mgr_profiles=profiles(("p1", "running", ["--lang=x"])))
    assert ops.list()[0]["launch_args"] == ["--lang=x"]

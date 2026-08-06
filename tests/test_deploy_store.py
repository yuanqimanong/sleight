"""本地 SQLite：主机、每台机上的若干 Manager、部署流水。

这一层存的是"下一条命令往哪台机、哪个目录发"。存错了就是往错的 Manager 上执行，
所以歧义必须报错而不是猜 —— 那是这组测试的重点。
"""

from __future__ import annotations

import json
import threading

import pytest

from sleight.deploy.errors import DeployError
from sleight.deploy.spec import DeploySpec
from sleight.deploy.store import Deployment, Host, Store, store_path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEIGHT_HOME", str(tmp_path / "home"))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


# --------------------------------------------------------------------------- #
# 建库
# --------------------------------------------------------------------------- #


def test_the_db_lands_under_sleight_home(tmp_path):
    assert store_path() == tmp_path / "home" / "sleight.db"
    Store()
    assert store_path().is_file()


def test_the_home_dir_is_private(tmp_path):
    """里面是运维目标机的清单，别人不该能列。"""
    Store()
    assert oct(store_path().parent.stat().st_mode)[-3:] == "700"


def test_opening_an_existing_db_is_not_destructive(tmp_path):
    first = Store(tmp_path / "x.db")
    first.put_host(Host(name="a", ssh="u@h"))
    assert [h.name for h in Store(tmp_path / "x.db").hosts()] == ["a"]


# --------------------------------------------------------------------------- #
# 主机
# --------------------------------------------------------------------------- #


def test_host_round_trip(store):
    store.put_host(Host(
        name="hk-01", ssh="deploy@1.2.3.4", port=2222, identity="~/.ssh/id_ed25519",
        sudo=True, strict_host_key="accept-new", notes="香港那台",
    ))
    host = store.require_host("hk-01")
    assert host.ssh == "deploy@1.2.3.4"
    assert host.port == 2222
    assert host.sudo is True
    assert host.strict_host_key == "accept-new"
    assert host.notes == "香港那台"
    assert host.created_at and host.updated_at
    assert not host.local


def test_an_empty_ssh_means_local(store):
    store.put_host(Host(name="local"))
    assert store.require_host("local").local


def test_put_host_is_an_upsert_and_keeps_created_at(store):
    store.put_host(Host(name="a", ssh="old@h"))
    created = store.require_host("a").created_at
    store.put_host(Host(name="a", ssh="new@h"))
    assert store.require_host("a").ssh == "new@h"
    assert store.require_host("a").created_at == created
    assert len(store.hosts()) == 1


def test_an_unknown_host_lists_the_known_ones(store):
    store.put_host(Host(name="hk-01"))
    with pytest.raises(DeployError, match="hk-01"):
        store.require_host("sg-02")


def test_hosts_come_back_sorted(store):
    for name in ("c", "a", "b"):
        store.put_host(Host(name=name))
    assert [h.name for h in store.hosts()] == ["a", "b", "c"]


def test_deleting_a_host_takes_its_deployments_with_it(store):
    store.put_host(Host(name="a"))
    store.put_deployment("a", "one", DeploySpec(dir="/srv/one"))
    store.delete_host("a")
    assert store.hosts() == []
    assert store.deployments() == []


def test_deleting_an_unknown_host_is_an_error(store):
    with pytest.raises(DeployError):
        store.delete_host("nope")


def test_host_builds_the_right_runner(store):
    store.put_host(Host(name="local"))
    store.put_host(Host(name="remote", ssh="deploy@h", port=2222, identity="/k"))
    assert type(store.require_host("local").runner()).__name__ == "LocalRunner"
    remote = store.require_host("remote").runner()
    assert remote.target == "deploy@h"
    assert remote.port == 2222


# --------------------------------------------------------------------------- #
# 部署 —— 一台机器可以有多个 Manager
# --------------------------------------------------------------------------- #


def test_one_host_can_hold_several_managers(store):
    """引擎本来就支持（不同目录/端口/容器名），存储层不该是那个瓶颈。"""
    store.put_host(Host(name="big", ssh="u@h"))
    store.put_deployment("big", "a", DeploySpec(dir="/srv/a", port=9000, container_name="cbm-a"))
    store.put_deployment("big", "b", DeploySpec(dir="/srv/b", port=9001, container_name="cbm-b"))
    assert [d.name for d in store.deployments(host="big")] == ["a", "b"]
    assert store.require_deployment("big", "b").spec.port == 9001


def test_two_deployments_cannot_share_a_data_dir(store):
    """手册 A.3 的禁令。两个 Manager 挂同一个 /data 会互相踩 profile 数据库。"""
    store.put_host(Host(name="h"))
    store.put_deployment("h", "a", DeploySpec(dir="/srv/same"))
    with pytest.raises(DeployError, match="互相踩"):
        store.put_deployment("h", "b", DeploySpec(dir="/srv/same"))


def test_updating_a_deployment_in_place_is_fine(store):
    store.put_host(Host(name="h"))
    store.put_deployment("h", "a", DeploySpec(dir="/srv/same", port=9000))
    store.put_deployment("h", "a", DeploySpec(dir="/srv/same", port=9100))
    assert store.require_deployment("h", "a").spec.port == 9100
    assert len(store.deployments()) == 1


def test_the_same_dir_on_a_different_host_is_fine(store):
    """两台机器各自的 /srv/cbm 毫无关系。"""
    for name in ("h1", "h2"):
        store.put_host(Host(name=name, ssh=f"u@{name}"))
        store.put_deployment(name, "default", DeploySpec(dir="/srv/cbm"))
    assert len(store.deployments()) == 2


def test_an_undeployable_spec_is_rejected_before_it_is_stored(store):
    """存一个部署不了的配置，等于把错误推迟到最不方便的时刻。"""
    store.put_host(Host(name="h"))
    with pytest.raises(ValueError, match="not reproducible"):
        store.put_deployment("h", "a", DeploySpec(image="repo/img:latest"))
    assert store.deployments() == []


def test_a_deployment_needs_its_host_to_exist(store):
    with pytest.raises(DeployError, match="no host named"):
        store.put_deployment("ghost", "a", DeploySpec())


def test_the_spec_survives_a_round_trip(store):
    spec = DeploySpec(
        dir="/srv/x", image="repo/img:v9", port=9100, bind_ip="10.0.0.5", expose=True,
        shm_size="8gb", container_name="cbm-x", nofile=99999,
    )
    store.put_host(Host(name="h"))
    store.put_deployment("h", "default", spec)
    assert store.require_deployment("h", "default").spec == spec


# --------------------------------------------------------------------------- #
# resolve：歧义必须报错，不许猜
# --------------------------------------------------------------------------- #


def test_a_host_with_one_deployment_resolves_by_host_name(store):
    store.put_host(Host(name="h"))
    store.put_deployment("h", "default", DeploySpec(dir="/srv/cbm"))
    assert store.resolve("h").name == "default"


def test_explicit_host_slash_name(store):
    store.put_host(Host(name="h"))
    store.put_deployment("h", "a", DeploySpec(dir="/srv/a", port=9000))
    store.put_deployment("h", "b", DeploySpec(dir="/srv/b", port=9001))
    assert store.resolve("h/b").spec.dir == "/srv/b"
    assert store.resolve("h/b").ref == "h/b"


def test_an_ambiguous_host_refuses_to_guess(store):
    """猜一个然后往错的 Manager 上发命令是不能接受的。"""
    store.put_host(Host(name="h"))
    store.put_deployment("h", "a", DeploySpec(dir="/srv/a", port=9000))
    store.put_deployment("h", "b", DeploySpec(dir="/srv/b", port=9001))
    with pytest.raises(DeployError, match="say which one") as exc:
        store.resolve("h")
    assert "h/a" in str(exc.value) and "h/b" in str(exc.value)


def test_a_host_with_no_deployment_says_how_to_add_one(store):
    store.put_host(Host(name="h"))
    with pytest.raises(DeployError, match="deployments add"):
        store.resolve("h")


def test_resolving_an_unknown_host(store):
    with pytest.raises(DeployError, match="no host named"):
        store.resolve("ghost")


def test_deleting_a_deployment_leaves_the_host(store):
    store.put_host(Host(name="h"))
    store.put_deployment("h", "a", DeploySpec(dir="/srv/a"))
    store.delete_deployment("h", "a")
    assert store.deployments() == []
    assert [x.name for x in store.hosts()] == ["h"]


# --------------------------------------------------------------------------- #
# 部署记录与流水
# --------------------------------------------------------------------------- #


def test_record_deploy_stamps_image_time_and_status(store):
    store.put_host(Host(name="h"))
    store.put_deployment("h", "a", DeploySpec(dir="/srv/a"))
    assert store.require_deployment("h", "a").deployed_at == ""

    store.record_deploy("h", "a", image="repo/img:v9", status={"profiles_total": 3})
    row = store.require_deployment("h", "a")
    assert row.image == "repo/img:v9"
    assert row.deployed_at
    assert row.status["profiles_total"] == 3


def test_events_are_newest_first_and_capped(store):
    for i in range(5):
        store.log_event("h", "deploy", ok=True, deployment="a", detail=f"第 {i} 次")
    events = store.events(limit=3)
    assert len(events) == 3
    assert events[0].detail == "第 4 次"
    assert events[0].kind == "deploy" and events[0].ok


def test_events_can_be_filtered_by_host(store):
    store.log_event("h1", "deploy", ok=True)
    store.log_event("h2", "backup", ok=False, detail="没空间了")
    assert [e.host for e in store.events(host="h2")] == ["h2"]
    assert store.events(host="h2")[0].ok is False


def test_logging_an_event_never_raises(store, monkeypatch):
    """记不上账不该让运维动作本身失败。"""
    store.close()
    monkeypatch.setattr(store, "path", "/nonexistent-dir/x.db")
    store.log_event("h", "deploy", ok=True)          # 不该抛


def test_a_huge_detail_is_truncated_not_rejected(store):
    store.log_event("h", "deploy", ok=False, detail="x" * 10_000)
    assert len(store.events()[0].detail) <= 2000


# --------------------------------------------------------------------------- #
# 从 hosts.toml 迁移
# --------------------------------------------------------------------------- #


def test_an_existing_hosts_toml_is_imported_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "hosts.toml").write_text(
        '[hosts.hk-01]\nssh = "deploy@1.2.3.4"\nport = 2222\nsudo = true\n\n'
        '[hosts.hk-01.deploy]\ndir = "/srv/cbm"\nport = 9100\n',
        encoding="utf-8",
    )
    store = Store()
    host = store.require_host("hk-01")
    assert host.ssh == "deploy@1.2.3.4" and host.port == 2222 and host.sudo
    assert store.resolve("hk-01").spec.port == 9100
    assert store.resolve("hk-01").spec.dir == "/srv/cbm"
    # 原文件留着但改了名，不会被导入第二次
    assert not (home / "hosts.toml").exists()
    assert (home / "hosts.toml.imported").is_file()


def test_a_broken_hosts_toml_is_left_alone(tmp_path):
    """语法坏了就别动它 —— 悄悄改名会让人以为文件丢了。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "hosts.toml").write_text("[hosts.a\nssh =", encoding="utf-8")
    Store()
    assert (home / "hosts.toml").is_file()


def test_no_hosts_toml_is_not_an_error(tmp_path):
    assert Store().hosts() == []


# --------------------------------------------------------------------------- #
# 并发
# --------------------------------------------------------------------------- #


def test_two_writers_do_not_corrupt_the_db(tmp_path):
    """Web 界面和 CLI 常常同时开着。"""
    path = tmp_path / "concurrent.db"
    Store(path).put_host(Host(name="h"))
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            Store(path).put_deployment(
                "h", f"d{index}", DeploySpec(dir=f"/srv/{index}", port=9000 + index)
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(Store(path).deployments(host="h")) == 8


def test_rows_are_json_serialisable(store):
    """Web 界面直接把它们丢给前端。"""
    store.put_host(Host(name="h", ssh="u@x"))
    store.put_deployment("h", "a", DeploySpec(dir="/srv/a"))
    store.record_deploy("h", "a", image="i:1", status={"ok": True})
    store.log_event("h", "deploy", ok=True, deployment="a")
    json.dumps([h.to_dict() for h in store.hosts()])
    json.dumps([d.to_dict() for d in store.deployments()])
    json.dumps([e.to_dict() for e in store.events()])


def test_deployment_ref_is_host_slash_name():
    assert Deployment(host="h", name="a", spec=DeploySpec()).ref == "h/a"


# --------------------------------------------------------------------------- #
# 同一台机上的第二个 Manager —— 命名空间与冲突
# --------------------------------------------------------------------------- #


def test_a_second_deployment_gets_its_own_compose_project_and_container():
    """不加后缀的话第二个 Manager 会把第一个删掉。

    真机上踩过：两份 compose 都渲染出 `name: cloakbrowser`，于是第二次
    `docker compose up -d` 认为对方的容器是本项目的旧实例，顺手清掉了 —— 第一个
    Manager 就这么静默没了。
    """
    from sleight.deploy.store import namespaced

    stock = DeploySpec()
    second = namespaced(stock, "second")
    assert second.name == "cloakbrowser-second"
    assert second.container_name == "cloakbrowser-manager-second"
    # default 保持经典名字，单部署的机器升级上来什么都不用改
    assert namespaced(stock, "default") == stock


def test_explicit_names_are_left_alone():
    from sleight.deploy.store import namespaced

    given = DeploySpec(name="mine", container_name="my-cbm")
    assert namespaced(given, "second") == given


def test_the_store_applies_the_namespacing(store):
    store.put_host(Host(name="h"))
    store.put_deployment("h", "default", DeploySpec(dir="/srv/a", port=9000))
    store.put_deployment("h", "second", DeploySpec(dir="/srv/b", port=9001))
    first, second = store.deployments(host="h")
    assert first.spec.name != second.spec.name, "两个部署的 compose 项目名必须不同"
    assert first.spec.container_name != second.spec.container_name


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dir", "/srv/same", "目录"),
        ("name", "same-project", "compose 项目名"),
        ("container_name", "same-container", "容器名"),
        ("port", 9000, "端口"),
    ],
)
def test_colliding_with_another_deployment_on_the_same_host_is_refused(store, field, value, message):
    """这四样每一样撞了都是事故，而且都是静默的那种。"""
    store.put_host(Host(name="h"))
    base = {"dir": "/srv/same", "name": "same-project", "container_name": "same-container",
            "port": 9000}
    store.put_deployment("h", "first", DeploySpec(**base))
    # 其余字段全部错开，只留 field 这一个撞上
    other = {"dir": "/srv/other", "name": "other-project", "container_name": "other-container",
             "port": 9999}
    other[field] = value
    with pytest.raises(DeployError, match=message):
        store.put_deployment("h", "second", DeploySpec(**other))


def test_collisions_across_hosts_are_fine(store):
    """两台机器各跑一个同名容器毫无关系。"""
    for name in ("h1", "h2"):
        store.put_host(Host(name=name, ssh=f"u@{name}"))
        store.put_deployment(name, "default", DeploySpec(dir="/srv/cbm", port=9000))
    assert len(store.deployments()) == 2


@pytest.mark.parametrize("bad", ["-nope", "Has Upper", "with space", "", "a/b"])
def test_a_deployment_name_that_cannot_be_a_project_name_is_refused(store, bad):
    """部署名会被拼进 compose 项目名和容器名。"""
    store.put_host(Host(name="h"))
    with pytest.raises(DeployError, match="不合法"):
        store.put_deployment("h", bad, DeploySpec())

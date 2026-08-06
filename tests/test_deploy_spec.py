"""DeploySpec 的校验与派生路径，以及 compose/.env 的渲染。

这一层是纯数据 + 纯函数，所以能把"生产环境禁止"的每一条都钉成一个断言。
"""

from __future__ import annotations

import pytest

from sleight.deploy.render import format_env, parse_env, render_compose, render_env
from sleight.deploy.spec import DeploySpec, generate_token, split_image

# --------------------------------------------------------------------------- #
# 镜像引用
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("cloakhq/cloakbrowser-manager:v0.0.10", ("cloakhq/cloakbrowser-manager", "v0.0.10", "")),
        ("nginx", ("nginx", "", "")),
        ("nginx:latest", ("nginx", "latest", "")),
        # 冒号既可能是 registry 端口也可能是标签分隔符，只能在最后一个 / 之后找
        ("reg.example.com:5000/ns/img", ("reg.example.com:5000/ns/img", "", "")),
        ("reg.example.com:5000/ns/img:v2", ("reg.example.com:5000/ns/img", "v2", "")),
        ("img:v1@sha256:abc", ("img", "v1", "sha256:abc")),
        ("img@sha256:abc", ("img", "", "sha256:abc")),
    ],
)
def test_split_image(image, expected):
    assert split_image(image) == expected


def test_token_is_32_bytes_of_hex():
    token = generate_token()
    assert len(token) == 64
    assert int(token, 16) >= 0
    assert generate_token() != token


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def test_defaults_validate():
    DeploySpec().validate()


def test_latest_is_refused_unless_explicit():
    with pytest.raises(ValueError, match="not reproducible"):
        DeploySpec(image="cloakhq/cloakbrowser-manager:latest").validate()
    DeploySpec(image="cloakhq/cloakbrowser-manager:latest", allow_latest=True).validate()


def test_missing_tag_is_refused():
    """裸镜像名等于隐式 latest —— 手册 A.3 明确禁止的那条。"""
    with pytest.raises(ValueError, match="no tag or digest"):
        DeploySpec(image="cloakhq/cloakbrowser-manager").validate()


def test_digest_pin_beats_latest_tag():
    """钉了摘要就算可复现，标签叫什么都不重要。"""
    DeploySpec(image="repo/img:latest@sha256:" + "a" * 64).validate()


def test_public_bind_needs_expose():
    with pytest.raises(ValueError, match="cleartext"):
        DeploySpec(bind_ip="0.0.0.0").validate()
    DeploySpec(bind_ip="0.0.0.0", expose=True).validate()


def test_private_network_bind_also_needs_expose():
    """10.0.0.12 也不是回环 —— 手册 A.6 那种私网直连是有意为之，必须显式承认。"""
    with pytest.raises(ValueError, match="cleartext"):
        DeploySpec(bind_ip="10.0.0.12").validate()


def test_loopback_v6_is_fine():
    DeploySpec(bind_ip="::1").validate()
    assert DeploySpec(bind_ip="::1").bound_url == "http://[::1]:9000"


@pytest.mark.parametrize(
    "kw",
    [
        {"name": "Cloak Browser"},          # compose 项目名不能有空格和大写
        {"name": "-bad"},
        {"container_name": "!nope"},
        {"dir": "relative/path"},
        {"dir": "/"},
        {"port": 0},
        {"port": 70000},
        {"shm_size": "5 gigs"},
        {"stop_grace_period": "45"},        # 缺单位
        {"restart": "sometimes"},
        {"nofile": 64},
        {"bind_ip": "not-an-ip"},
        {"log_max_file": 0},
    ],
)
def test_bad_specs_are_refused(kw):
    with pytest.raises(ValueError):
        DeploySpec(**kw).validate()


def test_error_lists_every_problem_at_once():
    """一次说清所有问题 —— 修一个报一个是最消耗耐心的交互。"""
    with pytest.raises(ValueError) as exc:
        DeploySpec(name="Bad Name", port=0, shm_size="huge").validate()
    message = str(exc.value)
    assert message.count("- ") >= 3


def test_warnings_are_not_errors():
    spec = DeploySpec(bind_ip="10.0.0.12", expose=True)
    spec.validate()
    joined = " ".join(spec.warnings())
    assert "cleartext" in joined
    assert "digest" in joined


# --------------------------------------------------------------------------- #
# 派生路径
# --------------------------------------------------------------------------- #


def test_derived_paths_are_posix_even_from_windows():
    """目标机一定是 Linux。用 pathlib 拼会在 Windows 控制机上生成反斜杠。"""
    spec = DeploySpec(dir="/srv/cbm")
    assert spec.compose_path == "/srv/cbm/docker-compose.yaml"
    assert spec.env_path == "/srv/cbm/.env"
    assert spec.data_dir == "/srv/cbm/data"
    assert spec.backups_dir == "/srv/cbm/backups"
    assert spec.extensions_dir == "/srv/cbm/data/extensions"
    assert "\\" not in spec.state_path


def test_container_and_host_extension_paths_differ_only_by_prefix():
    """填错这个 Chromium 会静默不加载，是文档里最常见的坑。"""
    spec = DeploySpec(dir="/data/apps/cloakbrowser-manager")
    assert spec.container_extensions_dir == "/data/extensions"
    assert spec.extensions_dir == "/data/apps/cloakbrowser-manager/data/extensions"


def test_reachable_from_control():
    assert not DeploySpec().reachable_from_control
    assert DeploySpec(bind_ip="10.0.0.12", expose=True).reachable_from_control


def test_round_trip_ignores_unknown_fields():
    """老状态文件要能被新版本读 —— 多出来的键不该让 status 直接崩。"""
    data = {**DeploySpec(port=9100).to_dict(), "some_future_field": 1}
    assert DeploySpec.from_dict(data).port == 9100


# --------------------------------------------------------------------------- #
# compose 渲染
# --------------------------------------------------------------------------- #


def test_compose_has_the_things_that_matter():
    text = render_compose(DeploySpec(port=9100, shm_size="8gb", container_name="cbm-01"))
    assert "name: cloakbrowser" in text
    assert "container_name: cbm-01" in text
    assert '"${MANAGER_BIND_IP:-127.0.0.1}:${MANAGER_PORT:-9100}:8080"' in text
    assert 'shm_size: "8gb"' in text
    assert "./data:/data" in text
    assert "soft: 65535" in text and "hard: 65535" in text


def test_compose_refuses_to_start_without_a_token():
    """``:?`` 不是装饰。.env 丢了要当场报错，而不是拉起一个免鉴权的浏览器集群。"""
    text = render_compose(DeploySpec())
    assert "${AUTH_TOKEN:?" in text
    assert "${MANAGER_IMAGE:?" in text


def test_healthcheck_targets_the_container_port():
    """健康检查在容器**内部**跑，所以是 8080 而不是宿主机端口。"""
    text = render_compose(DeploySpec(port=9100))
    assert "http://127.0.0.1:8080/api/status" in text
    assert "http://127.0.0.1:9100/api/status" not in text


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #


def test_env_round_trip():
    spec = DeploySpec(port=9100, image="repo/img:v1", bind_ip="10.0.0.12", expose=True)
    values = parse_env(render_env(spec, "deadbeef"))
    assert values == {
        "AUTH_TOKEN": "deadbeef",
        "MANAGER_IMAGE": "repo/img:v1",
        "MANAGER_BIND_IP": "10.0.0.12",
        "MANAGER_PORT": "9100",
    }


def test_env_keeps_hand_added_keys():
    """人在目标机上加过的键不能因为一次 apply 就消失。"""
    text = render_env(DeploySpec(), "tok", extra={"AUTH_TOKEN": "old", "MY_FLAG": "1"})
    values = parse_env(text)
    assert values["AUTH_TOKEN"] == "tok"          # 受管键以 spec 为准
    assert values["MY_FLAG"] == "1"


def test_env_mentions_the_600_requirement():
    assert "600" in render_env(DeploySpec(), "tok")


def test_parse_env_reads_a_hand_written_file():
    """目标机上那份可能是照着手册手敲的，各种写法都要吃得下。"""
    values = parse_env(
        "# comment\n"
        "\n"
        "AUTH_TOKEN=abc123\n"
        "export MANAGER_PORT=9000\n"
        'MANAGER_IMAGE="repo/img:v1"\n'
        "MANAGER_BIND_IP=127.0.0.1 # 只听本机\n"
        "not a valid line\n"
        "=missing_key\n"
    )
    assert values == {
        "AUTH_TOKEN": "abc123",
        "MANAGER_PORT": "9000",
        "MANAGER_IMAGE": "repo/img:v1",
        "MANAGER_BIND_IP": "127.0.0.1",
    }


def test_values_with_hash_get_quoted():
    """裸值里的 # 会被 compose 当行内注释，静默截断。"""
    text = format_env({"K": "a#b"})
    assert text == 'K="a#b"\n'
    assert parse_env(text)["K"] == "a#b"


def test_empty_value_is_quoted_not_dropped():
    assert parse_env(format_env({"K": ""})) == {"K": ""}


def test_a_tilde_path_says_why_it_cannot_be_expanded():
    """命令行里 shell 会先展开 ~，界面上手打就不会 —— 那时得说清怎么办。

    展开 ~ 需要知道目标机上那个用户的 home，而 spec 是纯数据，手上没有 runner。
    """
    with pytest.raises(ValueError, match="不会展开") as exc:
        DeploySpec(dir="~/cloakbrowser-manager").validate()
    assert "/home/" in str(exc.value), "光说不行还不够，得给个能照抄的例子"


def test_the_recommended_dir_is_something_the_validator_accepts():
    """推荐值必须自己能过校验 —— 照着推荐填却被拒绝是最伤人的。"""
    import re

    from sleight.deploy.presets import FIELD_HELP

    for path in re.findall(r"(/[\w./-]+)", FIELD_HELP["dir"].recommend):
        DeploySpec(dir=path).validate()
    assert "~" not in FIELD_HELP["dir"].recommend

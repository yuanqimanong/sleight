"""``DeploySpec`` → ``docker-compose.yaml`` / ``.env`` 文本。

纯函数、零 IO。``sleight deploy --dry-run`` 打出来的就是这里的返回值，和真正会落到
目标机上的字节完全一致 —— 不存在"预览一套、执行另一套"。

**为什么手写 YAML 而不是用 pyyaml**：只有一个固定形状要输出，字段全部经过
:meth:`DeploySpec.validate` 的正则校验。为此多背一个依赖不划算，理由同
:mod:`sleight.core._http` 那段"为什么不用 httpx"。
"""

from __future__ import annotations

from copy import deepcopy

import yaml

from .spec import CONTAINER_DATA, CONTAINER_PORT, DeploySpec

__all__ = ["ENV_KEYS", "format_env", "parse_env", "render_compose", "render_env"]

#: ``.env`` 里由 sleight 管理的键。其余键（人手加的）在重写时原样保留。
ENV_KEYS = ("AUTH_TOKEN", "MANAGER_IMAGE", "MANAGER_BIND_IP", "MANAGER_PORT")

HEADER = (
    "# 由 sleight deploy 生成 —— 手改会在下次 apply 时被覆盖。\n"
    "# 要改配置就改 sleight 的部署参数或 Web 配置，然后重新 apply。\n"
)


def render_compose(spec: DeploySpec) -> str:
    """渲染 ``docker-compose.yaml``。

    只有会随主机变化的四个值走 ``.env``（镜像、监听地址、端口、token），其余直接写死
    在 compose 里 —— 一份 compose 自己就能读懂，不用两个文件对照。

    ``${AUTH_TOKEN:?…}`` 里的 ``:?`` 不是装饰：``.env`` 丢了的时候它让 compose 当场
    报错，而不是拉起一个 ``AUTH_TOKEN=""`` 的 Manager。空 token 是**免鉴权的浏览器
    集群**，比起不起来严重得多。

    :param spec: 部署描述
    :returns: 完整的 compose 文件文本
    """
    healthcheck_url = f"http://127.0.0.1:{CONTAINER_PORT}/api/status"
    rendered = f"""{HEADER}
name: {spec.name}

services:
  {spec.service_name}:
    image: "${{MANAGER_IMAGE:?MANAGER_IMAGE is missing from .env}}"
    container_name: {spec.container_name}
    restart: {spec.restart}
    init: true

    ports:
      - "${{MANAGER_BIND_IP:-127.0.0.1}}:${{MANAGER_PORT:-{spec.port}}}:{CONTAINER_PORT}"

    environment:
      AUTH_TOKEN: "${{AUTH_TOKEN:?AUTH_TOKEN is missing from .env}}"
      CLOAKBROWSER_AUTO_UPDATE: "{str(spec.auto_update).lower()}"

    volumes:
      - "{spec.data_dir}:{CONTAINER_DATA}"

    shm_size: "{spec.shm_size}"
    mem_limit: "{spec.mem_limit}"
    cpus: {spec.cpus}
    pids_limit: {spec.pids_limit}
    stop_grace_period: {spec.stop_grace_period}

    ulimits:
      nofile:
        soft: {spec.nofile}
        hard: {spec.nofile}

    # 在容器**内部**执行，所以是 127.0.0.1:{CONTAINER_PORT} 而不是宿主机端口。
    # 新旧 Manager 都支持带 token 的 /api/status；旧版 /api/health 也要求鉴权。
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import os,urllib.request; urllib.request.urlopen(urllib.request.Request('{healthcheck_url}', headers={{'Authorization':'Bearer '+os.environ['AUTH_TOKEN']}}), timeout=5)"
        ]
      interval: 30s
      timeout: 8s
      retries: 3
      start_period: 30s

    logging:
      driver: json-file
      options:
        max-size: "{spec.log_max_size}"
        max-file: "{spec.log_max_file}"
"""
    if spec.data_volume:
        body = yaml.safe_load(rendered)
        body["services"][spec.service_name]["volumes"] = [{
            "type": "volume", "source": "sleight_data", "target": CONTAINER_DATA,
            "volume": {"subpath": "data"},
        }]
        body["volumes"] = {"sleight_data": {"name": spec.data_volume, "external": True}}
        return HEADER + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)
    return rendered


def merge_compose(current: str | None, spec: DeploySpec) -> str:
    """Update owned fields while retaining networks, extra mounts, env and services."""
    fresh = yaml.safe_load(render_compose(spec))
    if not current:
        return render_compose(spec)
    old = yaml.safe_load(current)
    if not isinstance(old, dict) or not isinstance(old.get("services"), dict):
        raise ValueError("Existing Compose has no services; import it before applying")
    if spec.service_name not in old["services"]:
        raise ValueError("Configured Manager service is missing; import existing Compose first")
    service = old["services"][spec.service_name]
    before = deepcopy(old)
    desired = fresh["services"][spec.service_name]
    # Imported mounts define the real /data and plugin paths. Never replace them.
    desired.pop("volumes")
    environment = service.get("environment", {})
    if isinstance(environment, list):
        environment = dict(e.split("=", 1) if "=" in e else (e, None) for e in environment)
    desired["environment"] = {**environment, **desired["environment"]}
    # Preserve explicitly set runtime variables, including vendor configuration.
    for key in ("CLOAKBROWSER_AUTO_UPDATE",):
        desired["environment"][key] = str(spec.auto_update).lower()
    service.update(desired)
    # A YAML dump is stable on subsequent reconciliation and never changes env secrets.
    return current if old == before else yaml.safe_dump(old, sort_keys=False, allow_unicode=True)


def render_env(spec: DeploySpec, token: str, *, extra: dict[str, str] | None = None) -> str:
    """渲染 ``.env``。

    :param spec: 部署描述
    :param token: ``AUTH_TOKEN``。engine 会先读目标机上已有的值，**存量 token 不会被
        无声换掉** —— 换了等于所有在用的客户端一起 401
    :param extra: 目标机 ``.env`` 里人手加的其它键，原样保留
    :returns: 完整的 ``.env`` 文本
    """
    managed = {
        "AUTH_TOKEN": token,
        "MANAGER_IMAGE": spec.image,
        "MANAGER_BIND_IP": spec.bind_ip,
        "MANAGER_PORT": str(spec.port),
    }
    kept = {k: v for k, v in (extra or {}).items() if k not in managed}
    body = format_env(managed)
    if kept:
        body += "\n# —— 以下不是 sleight 写的，原样保留 ——\n" + format_env(kept)
    return f"{HEADER}# 权限应为 600：这个文件里有 AUTH_TOKEN。\n\n{body}"


def format_env(values: dict[str, str]) -> str:
    """把键值对写成 ``.env`` 行。

    值里出现 ``#`` 或空白就加引号 —— compose 会把裸值里的 ``#`` 当行内注释起点，
    静默截断。
    """
    out = []
    for key, value in values.items():
        text = str(value)
        if text == "" or any(c in text for c in ' \t"\'#$'):
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'{key}="{escaped}"')
        else:
            out.append(f"{key}={text}")
    return "\n".join(out) + "\n"


def parse_env(text: str) -> dict[str, str]:
    """读 ``.env``。

    刻意宽松：目标机上那份可能是照着手册手写的。**不做变量插值** —— 我们只需要原样
    取回 ``AUTH_TOKEN`` 和保留人手加的键，插值是 compose 自己的事。

    :param text: 文件内容
    :returns: 键 → 值。解析不出的行直接跳过
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
            if raw.count('\\"'):
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        else:
            # 裸值里的 # 是行内注释 —— compose 就是这么解析的
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values

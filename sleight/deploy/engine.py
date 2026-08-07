"""Manager 部署的全生命周期：plan / apply / status / logs / upgrade / backup / destroy。

引擎只跟 :class:`~sleight.deploy.runner.Runner` 说话，所以本机和远程走的是同一条代码
路径。手册附录 A 里那串手工命令，这里一条一条落成了可重跑的步骤。

**幂等**：重复 ``apply()`` 不会重建容器、不会换 token、不会动 ``/data``。什么都没变时
它连 ``up -d`` 都不发。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .errors import DeployError, PreflightFailed
from .preflight import Check, CheckLevel, preflight, worst
from .render import parse_env, render_compose, render_env
from .runner import PULL_TIMEOUT, CommandResult, Runner, describe
from .spec import CONTAINER_PORT, DeploySpec, generate_token

if TYPE_CHECKING:
    from ..providers.cloakbrowser import CloakBrowserManager

log = logging.getLogger("sleight.deploy")

__all__ = ["DeployError", "DeployResult", "Deployer", "Plan"]

#: 容器从 created 到 healthy 的等待上限。冷启动要解压浏览器，start_period 就是 30s
HEALTH_TIMEOUT = 240.0


@dataclass(frozen=True, slots=True)
class Plan:
    """``apply()`` 会做什么。``--dry-run`` 打的就是它。"""

    spec: DeploySpec
    checks: list[Check]
    files: dict[str, str]
    changes: list[str]
    commands: list[tuple[str, ...]]
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return worst(self.checks) is CheckLevel.FAIL

    @property
    def up_to_date(self) -> bool:
        return not self.changes

    def render(self, *, show_files: bool = True) -> str:
        """渲染成人能读的报告。"""
        out = [f"目标  {self.spec.dir}  (镜像 {self.spec.image})", ""]
        out += ["体检"] + [f"  {line}" for c in self.checks for line in str(c).splitlines()]
        if self.warnings:
            out += ["", "提醒"] + [f"  ! {w}" for w in self.warnings]
        out += ["", "变更"]
        out += [f"  · {c}" for c in self.changes] or ["  （无 —— 目标机已经是这个状态）"]
        if self.commands:
            out += ["", "将执行"] + [f"  $ {describe(c)}" for c in self.commands]
        if show_files and self.files:
            for path, text in self.files.items():
                out += ["", f"── {path} " + "─" * max(0, 66 - len(path))]
                out += [f"  {line}" for line in text.splitlines()]
        return "\n".join(out)


@dataclass(frozen=True, slots=True)
class DeployResult:
    """``apply()`` / ``upgrade()`` 的结果。"""

    spec: DeploySpec
    token: str = field(repr=False)
    changed: bool
    steps: list[str]
    status: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if not self.status:
            return "已部署（未取到 /api/status）"
        s = self.status
        return (
            f"Manager 就绪 —— 浏览器内核 {s.get('binary_version', '?')}，"
            f"profile {s.get('profiles_total', '?')} 个，运行中 {s.get('running_count', '?')} 个"
        )


class Deployer:
    """一台目标机上的一个 Manager 部署。

    :param spec: 部署描述
    :param runner: 在哪台机器上执行（:class:`~sleight.deploy.runner.LocalRunner` 或
        :class:`~sleight.deploy.runner.SSHRunner`）
    :param sudo: 建目录、改权限时用 ``sudo -n``。**docker 命令不加 sudo** ——
        当前用户应该在 docker 组里
    :param dry_run: 只读探测照常发，任何会改变目标机的命令都只记录不执行
    :param on_progress: 每完成一步回调一次，Web 界面的 SSE 和 CLI 的 ``-v`` 都用它
    """

    def __init__(
        self,
        spec: DeploySpec,
        runner: Runner,
        *,
        sudo: bool = False,
        dry_run: bool = False,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.spec = spec
        self.runner = runner
        self.sudo = sudo
        self.dry_run = dry_run
        self._on_progress = on_progress
        #: dry-run 下累积的命令；真跑时也记，用于结果里的 steps
        self.recorded: list[tuple[str, ...]] = []
        self.steps: list[str] = []

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def say(self, message: str) -> None:
        self.steps.append(message)
        log.info("%s", message)
        if self._on_progress is not None:
            self._on_progress(message)

    def probe(self, argv: Sequence[str], **kw: Any) -> CommandResult:
        """只读探测。**dry-run 下照样执行** —— 不然预览出来的差异全是猜的。"""
        return self.runner.run(argv, **kw)

    def mutate(self, argv: Sequence[str], *, check: bool = True, **kw: Any) -> CommandResult:
        """会改变目标机的命令。dry-run 下只记录。"""
        self.recorded.append(tuple(argv))
        if self.dry_run:
            return CommandResult(tuple(argv), 0, "", "")
        return self.runner.run(argv, check=check, **kw)

    def write_file(self, text: str, path: str, *, mode: int | None = None) -> None:
        self.recorded.append(("write", path))
        if self.dry_run:
            return
        self.runner.put_text(text, path, mode=mode, sudo=self.sudo)

    def compose_argv(self, *args: str) -> list[str]:
        """``docker compose`` 命令。工作目录是部署目录，``.env`` 因此会被自动读取。"""
        return ["docker", "compose", *args]

    def _compose(
        self, *args: str, timeout: float | None = None, mutate: bool = True, check: bool = True
    ) -> CommandResult:
        argv = self.compose_argv(*args)
        if not mutate:
            return self.probe(argv, cwd=self.spec.dir, timeout=timeout)
        return self.mutate(argv, cwd=self.spec.dir, timeout=timeout, check=check)

    # ------------------------------------------------------------------ #
    # 目标机现状
    # ------------------------------------------------------------------ #

    def existing_env(self) -> dict[str, str]:
        """目标机上现有的 ``.env``。没有就是空 dict。"""
        text = self.runner.read_text(self.spec.env_path, sudo=self.sudo)
        return parse_env(text) if text else {}

    def existing_token(self) -> str | None:
        """现有的 ``AUTH_TOKEN``。

        **存量 token 绝不无声更换** —— 换掉等于所有在用的客户端同时 401，而且旧 token
        在别的机器的环境变量里，找回来很麻烦。
        """
        return self.existing_env().get("AUTH_TOKEN") or None

    def read_state(self) -> dict[str, Any]:
        """上次部署留下的状态文件（记着旧镜像，回滚要用）。"""
        text = self.runner.read_text(self.spec.state_path, sudo=self.sudo)
        if not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def container_state(self) -> dict[str, str]:
        """容器的存在性、运行状态和健康状态。

        :returns: ``{"exists", "status", "health", "image"}``，全是字符串
        """
        fmt = "{{.State.Status}}\t{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}\t{{.Config.Image}}"
        r = self.probe(["docker", "inspect", "--format", fmt, self.spec.container_name])
        if not r.ok:
            return {"exists": "no", "status": "absent", "health": "none", "image": ""}
        parts = [*r.text.split("\t"), "", "", ""][:3]
        return {"exists": "yes", "status": parts[0], "health": parts[1], "image": parts[2]}

    def api_status(self) -> dict[str, Any]:
        """``GET /api/status``，在**容器内**发。

        为什么不在宿主机上 curl：宿主机不一定有 curl 或 python，而镜像自己的
        healthcheck 就是用容器内的 python 打这个接口的 —— 那是唯一能假定存在的工具。
        ``/api/status`` 免鉴权，所以不需要把 token 传进命令行。
        """
        script = (
            "import urllib.request,sys;"
            f"sys.stdout.write(urllib.request.urlopen("
            f"'http://127.0.0.1:{CONTAINER_PORT}/api/status', timeout=5).read().decode())"
        )
        r = self.probe(
            ["docker", "exec", self.spec.container_name, "python", "-c", script], timeout=30
        )
        if not r.ok:
            return {}
        try:
            body = json.loads(r.text)
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def published_ports(self) -> list[str]:
        """容器实际发布出来的端口，用来确认端口映射真的生效了。"""
        r = self.probe(["docker", "port", self.spec.container_name])
        return [ln.strip() for ln in r.out.splitlines() if ln.strip()] if r.ok else []

    # ------------------------------------------------------------------ #
    # plan
    # ------------------------------------------------------------------ #

    def plan(self) -> Plan:
        """算出要做什么，**不动目标机**。

        :raises ValueError: spec 自相矛盾
        """
        self.spec.validate()
        checks = preflight(self.spec, self.runner, sudo=self.sudo)

        env = self.existing_env()
        token = env.get("AUTH_TOKEN") or generate_token()
        compose_text = render_compose(self.spec)
        env_text = render_env(self.spec, token, extra=env)
        files = {self.spec.compose_path: compose_text, self.spec.env_path: env_text}

        changes: list[str] = []
        if not self.probe(["test", "-d", self.spec.dir]).ok:
            changes.append(f"创建目录 {self.spec.dir}（含 data/ 和 backups/）")
        current_compose = self.runner.read_text(self.spec.compose_path, sudo=self.sudo)
        if current_compose is None:
            changes.append("写入 docker-compose.yaml")
        elif current_compose != compose_text:
            changes.append("更新 docker-compose.yaml")
        if not env:
            changes.append("写入 .env（生成新的 AUTH_TOKEN）")
        else:
            drift = [
                k for k in ("MANAGER_IMAGE", "MANAGER_BIND_IP", "MANAGER_PORT")
                if env.get(k) != parse_env(env_text).get(k)
            ]
            if drift:
                changes.append(f"更新 .env：{', '.join(drift)}（AUTH_TOKEN 保持不变）")

        state = self.container_state()
        if state["exists"] == "no":
            changes.append("拉镜像并创建容器")
        elif state["status"] != "running":
            changes.append(f"容器当前是 {state['status']}，将启动")
        elif changes:
            changes.append("重建容器让新配置生效")

        commands: list[tuple[str, ...]] = []
        if changes:
            commands.append(tuple(self.compose_argv("pull", "manager")))
            commands.append(tuple(self.compose_argv("up", "-d")))

        return Plan(self.spec, checks, files, changes, commands, self.spec.warnings())

    # ------------------------------------------------------------------ #
    # apply
    # ------------------------------------------------------------------ #

    def apply(
        self,
        *,
        pull: bool = True,
        wait: bool = True,
        force_recreate: bool = False,
        force: bool = False,
    ) -> DeployResult:
        """把目标机收敛到 spec 描述的状态。可以反复跑。

        :param pull: 先 ``docker compose pull``
        :param wait: 等容器 healthy 并取一次 ``/api/status``
        :param force_recreate: 配置没变也重建容器
        :param force: 体检有 FAIL 也继续（**不推荐**，端口冲突之类硬错会在后面炸）
        :returns: :class:`DeployResult`
        :raises ValueError: spec 自相矛盾
        :raises PreflightFailed: 体检不过且没给 ``force=True``
        :raises DeployError: 目标机上的命令失败，或容器没能变 healthy
        """
        plan = self.plan()
        if plan.blocked and not force:
            failed = [c for c in plan.checks if c.level is CheckLevel.FAIL]
            raise PreflightFailed(
                "preflight failed on "
                f"{self.runner.label}:\n" + "\n".join(str(c) for c in failed),
                list(plan.checks),
            )
        # 无变更时不念提醒。一个每次都响的警告只会训练人忽略警告
        if plan.changes:
            for warning in plan.warnings:
                self.say(f"提醒：{warning}")

        token = parse_env(plan.files[self.spec.env_path])["AUTH_TOKEN"]

        self._ensure_dirs()

        wrote = False
        for path, text in plan.files.items():
            if self.runner.read_text(path, sudo=self.sudo) == text:
                continue
            mode = 0o600 if path == self.spec.env_path else 0o644
            self.write_file(text, path, mode=mode)
            self.say(f"写入 {path}")
            wrote = True

        self.write_file(self._state_json(), self.spec.state_path, mode=0o644)

        changed = wrote or force_recreate or self.container_state()["status"] != "running"
        if not changed:
            self.say("目标机已经是这个状态，不动容器")
            return DeployResult(self.spec, token, False, self.steps, self.api_status())

        if pull:
            self._pull()

        up = ["up", "-d"]
        if force_recreate:
            up += ["--no-deps", "--force-recreate", "manager"]
        self.say("docker compose " + " ".join(up))
        self._compose(*up, timeout=600).check()

        status: dict[str, Any] = {}
        if wait and not self.dry_run:
            status = self.wait_healthy()
        return DeployResult(self.spec, token, True, self.steps, status)

    def _pull(self) -> None:
        """拉镜像。**目标机上已经有这个镜像的话，拉失败不算致命。**

        pull 的语义是"尽力刷新"，不是"必须成功"。目标机连不上 registry 是很常见的
        （内网、离线、daemon 没配代理），而镜像可能是 ``docker load`` 进去的 —— 那种
        情况下让 pull 把整个部署挡死毫无道理。本地**没有**这个镜像时才是真的走不下去。
        """
        have_local = self.probe(
            ["docker", "image", "inspect", self.spec.image, "--format", "{{.Id}}"]
        ).ok
        self.say(f"拉镜像 {self.spec.image}（可能要几分钟）")
        result = self._compose("pull", "manager", timeout=PULL_TIMEOUT, check=False)
        if result.ok:
            return
        reason = (result.err.strip() or result.out.strip() or "(no output)").splitlines()[-1]
        if have_local:
            self.say(f"拉不动（{reason}），但目标机上已经有这个镜像，就用本地那份")
            return
        raise DeployError(
            f"目标机上没有 {self.spec.image}，而且拉不下来：\n  {reason}\n"
            "  如果目标机本来就上不了 registry，可以先把镜像送过去：\n"
            f"    docker save {self.spec.image} | ssh <目标机> docker load\n"
            "  然后重跑（sleight 会认出镜像已在本地）。daemon 没配代理的话，"
            "sleight preflight 会指出来。"
        )

    def _ensure_dirs(self) -> None:
        """建部署目录。

        用 ``sudo`` 建出来的目录归 root，之后写 compose/.env 就还得继续 sudo ——
        所以建完立刻把**顶层目录**交回当前用户（手册 A.2 的 ``chown`` 那步）。
        只 chown 我们刚建的这几个目录，**不递归** —— ``data/`` 里是容器以 root 身份
        写的浏览器用户数据，递归改所有权是在拿登录态冒险。
        """
        created = not self.probe(["test", "-d", self.spec.dir]).ok
        for path in (self.spec.dir, self.spec.data_dir, self.spec.backups_dir):
            self.mutate(["mkdir", "-p", path], sudo=self.sudo)
        if created:
            self.say(f"创建 {self.spec.dir}")
            if self.sudo:
                who = self.probe(["id", "-un"]).text or ""
                if who:
                    for path in (self.spec.dir, self.spec.data_dir, self.spec.backups_dir):
                        self.mutate(["chown", f"{who}:{who}", path], sudo=True, check=False)

    def _state_json(self) -> str:
        """状态文件。**不写 token** —— 那是 ``.env``（600）的事。"""
        previous = self.read_state()
        state = {
            "spec": self.spec.to_dict(),
            "image": self.spec.image,
            "previous_image": (
                previous.get("image")
                if previous.get("image") and previous.get("image") != self.spec.image
                else previous.get("previous_image")
            ),
            "deployed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "managed_by": "sleight",
        }
        return json.dumps(state, indent=2, ensure_ascii=False) + "\n"

    def wait_healthy(self, *, timeout: float = HEALTH_TIMEOUT) -> dict[str, Any]:
        """等到容器 healthy 且 ``/api/status`` 有响应。

        :returns: ``/api/status`` 的 body
        :raises DeployError: 超时，或容器已经退出
        """
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            state = self.container_state()
            if state["status"] in ("exited", "dead"):
                logs = self.probe(
                    ["docker", "logs", "--tail", "40", self.spec.container_name]
                )
                raise DeployError(
                    f"container {self.spec.container_name} is {state['status']} right after "
                    f"start:\n{logs.err.strip() or logs.out.strip()}"
                )
            # 没有 healthcheck 时（用户自己改过 compose）就只认 /api/status
            if state["status"] == "running" and state["health"] in ("healthy", "none"):
                body = self.api_status()
                if body:
                    self.say(
                        f"healthy —— 浏览器内核 {body.get('binary_version', '?')}，"
                        f"profile {body.get('profiles_total', 0)} 个"
                    )
                    return body
            if state["health"] != last:
                last = state["health"]
                self.say(f"等待就绪：{state['status']}/{state['health']}")
            time.sleep(2.0)
        raise DeployError(
            f"{self.spec.container_name} did not become healthy within {timeout:.0f}s; "
            f"look at: docker compose logs --tail=200 manager (in {self.spec.dir})"
        )

    # ------------------------------------------------------------------ #
    # 日常运维
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        """一次把该看的都看了。

        :returns: 容器状态 + ``/api/status`` + 端口映射 + 状态文件
        """
        state = self.container_state()
        return {
            "target": self.runner.label,
            "dir": self.spec.dir,
            "container": state,
            "ports": self.published_ports() if state["exists"] == "yes" else [],
            "api": self.api_status() if state["status"] == "running" else {},
            "state_file": self.read_state(),
            "url": self.spec.bound_url,
        }

    def logs(self, *, tail: int = 200) -> str:
        """取一段日志。``follow`` 交给 CLI —— 那需要把子进程的输出直接接到终端上。"""
        r = self._compose("logs", "--tail", str(tail), "manager", mutate=False, timeout=60)
        return r.out or r.err

    def backup(self) -> str:
        """停机归档整个 ``data/``。

        **必须停机。** 在线复制 SQLite 会拿到一个和浏览器用户目录时间点不一致的库，
        恢复出来的 profile 可能起不来（手册 A.8）。

        时间戳在控制机上算，不依赖目标机的 ``date`` —— 这样同一次操作的备份名到处一致。

        打包**在一个一次性 root 容器里**做（见 :meth:`as_root`）：``data/`` 里的文件是
        Manager 以 root 身份写的，宿主机上的普通用户 ``tar`` 会一路 ``Permission
        denied``，而且**会留下一个看着像备份的残档**。实测那个残档只有 1.9 KB / 12 条，
        真 profile 的文件一个都没进去 —— 这比没有备份危险得多。

        :returns: 目标机上的归档路径
        :raises DeployError: 打包失败。失败时不会留下任何 ``.tar.gz``
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"cloakbrowser-{stamp}.tar.gz"
        archive = f"{self.spec.backups_dir}/{name}"
        self.mutate(["mkdir", "-p", self.spec.backups_dir], sudo=self.sudo)
        running = self.container_state()["status"] == "running"
        if running:
            self.say("停 manager（一致性备份必须停机）")
            self._compose("stop", "manager", timeout=180).check()
        try:
            self.say(f"归档 data/ → {archive}")
            # 容器里是 root，产出的文件默认归 root —— 那样用户连删旧备份都要 sudo。
            # 取不到 uid 就干脆不 chown：宁可留个 root 拥有的档，也不能 chown 到 0:0。
            owner = self._host_owner()
            give_back = f"chown {owner} /backup/{name}.part && " if owner else ""
            # 先写 .part，成功了才改名 —— 失败绝不留下一个看着像备份的文件
            script = (
                f"if tar -czf /backup/{name}.part -C / data; then "
                f"{give_back}mv /backup/{name}.part /backup/{name}; "
                f"else rm -f /backup/{name}.part; exit 1; fi"
            )
            result = self.as_root(
                script,
                mounts=[(self.spec.data_dir, "/data:ro"), (self.spec.backups_dir, "/backup")],
                timeout=3600,
            )
            if not result.ok:
                raise DeployError(
                    f"backup failed, nothing was written to {self.spec.backups_dir}:\n  "
                    + (result.err.strip() or result.out.strip() or "(no output)")
                )
        finally:
            if running:
                self.say("重新启动 manager")
                self._compose("start", "manager", timeout=180)
        return archive

    def as_root(
        self, script: str, *, mounts: list[tuple[str, str]], timeout: float = 300.0
    ) -> CommandResult:
        """在一个一次性容器里以 root 跑一段 shell。

        为什么需要它：``//data`` 下的东西是 Manager 以 root 写的，宿主机上的普通用户
        既读不了也删不掉。要么要求目标机配免密 sudo，要么借用**已经在本地的那个镜像**
        起一个临时容器 —— 后者不多要任何权限，因为能发 ``docker compose`` 就意味着
        已经有 docker 组权限了。

        :param script: 在容器里执行的 shell
        :param mounts: ``(宿主机路径, 容器内路径[:选项])`` 列表
        :param timeout: 超时，秒
        :returns: :class:`~sleight.deploy.runner.CommandResult`，**非 0 不抛**
        """
        argv = ["docker", "run", "--rm", "--entrypoint", "sh"]
        for source, target in mounts:
            argv += ["-v", f"{source}:{target}"]
        argv += [self.spec.image, "-c", script]
        return self.mutate(argv, timeout=timeout, check=False)

    def _host_owner(self) -> str | None:
        """目标机上当前用户的 ``uid:gid``，用来把容器产出的文件交还给他。

        取不到就返回 ``None`` —— 调用方应当跳过 chown，而不是退化成 ``0:0``。
        """
        uid = self.probe(["id", "-u"]).text
        gid = self.probe(["id", "-g"]).text
        return f"{uid}:{gid}" if uid.isdigit() and gid.isdigit() else None

    def upgrade(self, image: str, *, backup: bool = True, wait: bool = True) -> DeployResult:
        """换镜像。

        :param image: 新的镜像标签或摘要
        :param backup: 先做一次停机备份（手册 A.9：升级前必须有可回滚的数据）
        :param wait: 等新容器 healthy
        :returns: :class:`DeployResult`
        :raises DeployError: 目标机上没有已部署的 compose（先 ``apply()``）
        """
        if self.runner.read_text(self.spec.compose_path, sudo=self.sudo) is None:
            raise DeployError(
                f"no deployment at {self.spec.dir} — run a deploy first, upgrade only swaps "
                "the image of an existing one"
            )
        previous = self.existing_env().get("MANAGER_IMAGE") or self.read_state().get("image")
        if backup:
            self.backup()
        self.spec = self.spec.replace(image=image)
        self.say(f"镜像 {previous or '?'} → {image}")
        result = self.apply(pull=True, wait=wait, force_recreate=True)
        self.say("验收：容器 healthy、profile 数量/ID 未变、抽样 profile 能启动并过 CDP")
        return result

    def rollback(self, *, wait: bool = True) -> DeployResult:
        """回到状态文件里记着的上一个镜像。

        :raises DeployError: 状态文件里没有旧镜像可回
        """
        previous = self.read_state().get("previous_image")
        if not previous:
            raise DeployError(
                f"{self.spec.state_path} has no previous_image to roll back to; "
                "pass the old tag to upgrade() explicitly"
            )
        return self.upgrade(previous, backup=False, wait=wait)

    def destroy(self, *, purge_data: bool = False, purge_image: bool = False) -> None:
        """停并删容器。

        **永远不带 ``-v``**，也永远不主动删 ``data/``。三件东西是分开的，删掉的代价
        完全不同：

        =============== ================================== ==================
        删什么           丢什么                              还能回来吗
        =============== ================================== ==================
        容器（默认）      运行中的进程                        ``deploy`` 一下就回来
        ``purge_data``   profile、Cookie、全部登录态          **不可逆**
        ``purge_image``  本机的镜像层                        要重新 pull（或再传一次）
        =============== ================================== ==================

        :param purge_data: 连 ``data/`` 一起删。调用方必须先向人确认过
        :param purge_image: 连镜像一起删。**同一台机上还有别的部署在用这个镜像的话
            会删不掉** —— 那不是错误，只记一条提示
        :raises DeployError: ``data/`` 删不掉
        """
        if self.runner.read_text(self.spec.compose_path, sudo=self.sudo) is None:
            self.say(f"{self.spec.dir} 下没有 compose，只尝试删容器")
            self.mutate(["docker", "rm", "-f", self.spec.container_name], check=False)
        else:
            self.say("docker compose down（不带 -v，卷和数据都留着）")
            self._compose("down", timeout=300).check()
        if purge_data:
            self.say(f"删除 {self.spec.data_dir} —— 登录态和 profile 一起没了")
            self._purge_data()
        if purge_image:
            self._purge_image()

    def _purge_image(self) -> None:
        """删镜像。**删不掉不算失败** —— 多半是同机别的容器还在用它。

        不加 ``--force``：强删会把还在跑的那个部署的镜像层抽走，症状是它下次重启
        起不来，而现场早就不在了。
        """
        self.say(f"删除镜像 {self.spec.image}")
        result = self.mutate(["docker", "image", "rm", self.spec.image], check=False)
        if result.ok:
            return
        detail = (result.err or result.out).strip().splitlines()
        self.say(
            f"镜像没删掉（{detail[-1] if detail else '无输出'}）—— 同机多半还有别的容器"
            f"在用它。要强删自己来：docker image rm -f {self.spec.image}"
        )

    def _purge_data(self) -> None:
        """删 ``data/``。

        先按普通用户删（从没启动过 profile 的话它就是用户自己的），失败了再借一个
        root 容器 —— 浏览器用户目录是容器以 root 写的，普通用户 ``rm`` 删不掉。
        """
        plain = self.mutate(["rm", "-rf", self.spec.data_dir], sudo=self.sudo, check=False)
        if plain.ok:
            return
        self.say("有 root 拥有的文件（容器写的），换一次性 root 容器来删")
        result = self.as_root(
            'rm -rf /target/data && echo removed', mounts=[(self.spec.dir, "/target")]
        )
        if not result.ok:
            raise DeployError(
                f"could not remove {self.spec.data_dir}:\n  "
                + (result.err.strip() or plain.err.strip() or "(no output)")
                + f"\n  manual fallback: sudo rm -rf {self.spec.data_dir}"
            )

    # ------------------------------------------------------------------ #
    # 拿一个能用的 Manager 客户端
    # ------------------------------------------------------------------ #

    @contextmanager
    def connect(self, *, token: str | None = None) -> Iterator[CloakBrowserManager]:
        """在上下文里给出一个连得上的 :class:`~sleight.providers.CloakBrowserManager`。

        Manager 一般只绑在目标机的 ``127.0.0.1`` 上，所以远程时这里会临时开一条
        SSH 端口转发（等价于手册 A.5 那条 ``ssh -N -L``），退出时拆掉。本机部署则
        直接连。

        :param token: 不给就从目标机的 ``.env`` 里读
        :raises DeployError: 目标机上没有 ``AUTH_TOKEN``
        """
        from ..providers.cloakbrowser import CloakBrowserManager

        auth = token or self.existing_token()
        if not auth:
            raise DeployError(
                f"no AUTH_TOKEN in {self.spec.env_path}; is anything deployed there?"
            )
        with self.runner.tunnel(self.spec.port) as port:
            yield CloakBrowserManager(f"http://127.0.0.1:{port}", token=auth, name=self.spec.name)

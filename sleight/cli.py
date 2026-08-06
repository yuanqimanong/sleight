"""``sleight`` 命令行。

把手册附录 A 的手工部署、和插件运维里那串 curl + python 单行命令，收成可重跑的子命令。
一台机器上装了 sleight 和 docker 就能 ``sleight deploy``；控制机装 sleight、目标机只要
有 docker 和 sshd，就能 ``sleight deploy --ssh deploy@host``。

只用标准库（argparse）。``sleight ui`` 那个 Web 界面需要 ``pip install "sleight[ui]"``。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import replace
from typing import Any

from .core.errors import SleightError
from .deploy.engine import Deployer
from .deploy.errors import DeployError, PreflightFailed
from .deploy.ops import ExtensionOps, ProfileOps
from .deploy.preflight import CheckLevel, worst
from .deploy.presets import (
    DEPLOY_TEMPLATES,
    FIELD_HELP,
    PROFILE_PRESETS,
    profile_spec_from,
)
from .deploy.runner import Runner, describe
from .deploy.spec import DEFAULT_IMAGE, DeploySpec
from .deploy.store import Deployment, Host, Store

__all__ = ["main"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3
EXIT_INTERRUPT = 130
EXIT_SIGPIPE = 141        # 128 + SIGPIPE，管道被下游关掉时的惯例


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _dump(obj: Any) -> None:
    _out(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _printer(args: argparse.Namespace) -> Callable[[str], None] | None:
    """进度回调。``--json`` 下静音 —— 那个输出是要给程序解析的。"""
    if args.json or args.quiet:
        return None
    return lambda message: _out(f"  · {message}")


def _table(rows: list[list[str]], headers: Sequence[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in rows]
    return "\n".join(out)


def _confirm(question: str, expect: str, *, assume_yes: bool) -> bool:
    """不可逆动作的确认。

    非交互时**一律当成拒绝** —— 在 CI 或管道里静默删掉 ``/data`` 是不能接受的默认值。
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        _err("拒绝执行：这是不可逆动作，非交互环境下必须显式加 --yes")
        return False
    try:
        got = input(f"{question}\n  输入 {expect!r} 确认，其它任何输入取消：")
    except EOFError:
        return False
    return got.strip() == expect


# --------------------------------------------------------------------------- #
# 目标机解析
# --------------------------------------------------------------------------- #

_SPEC_FLAGS = {
    "dir": "dir",
    "image": "image",
    "port": "port",
    "bind": "bind_ip",
    "shm_size": "shm_size",
    "project": "name",
    "container": "container_name",
    "expose": "expose",
    "allow_latest": "allow_latest",
}


def _store() -> Store:
    return Store()


def _template_spec(args: argparse.Namespace) -> DeploySpec:
    """模板打底 + 命令行覆盖。模板和界面上那几张卡片是同一份定义。"""
    name = getattr(args, "template", None)
    base: dict[str, Any] = {}
    if name:
        chosen = next((t for t in DEPLOY_TEMPLATES if t.key == name), None)
        if chosen is None:
            known = ", ".join(t.key for t in DEPLOY_TEMPLATES)
            raise DeployError(f"没有叫 {name!r} 的模板。有这些：{known}")
        base = dict(chosen.overrides)
    return DeploySpec(**{**base, **_spec_overrides(args)})


def _spec_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """命令行显式给了的字段。没给的是 ``None``，不参与覆盖 —— 所以
    ``sleight deploy --host hk-01`` 不会把库里存的配置打掉。"""
    return {
        field: getattr(args, flag)
        for flag, field in _SPEC_FLAGS.items()
        if getattr(args, flag, None) is not None
    }


def _resolve(args: argparse.Namespace) -> tuple[Runner, DeploySpec, bool, Deployment | None]:
    """命令行 + 本地库 → (Runner, DeploySpec, 是否 sudo, 库里的那条部署记录)。

    ``--host`` 收 ``主机`` 或 ``主机/部署名``：一台机器可以跑多个 Manager，只给主机名
    而它有多个时会报错并列出来 —— 猜一个然后往错的 Manager 上发命令是不能接受的。

    ``--ssh`` 是临时目标，不进库，也就没有部署记录（返回 ``None``）。
    """
    if args.host and args.ssh:
        raise DeployError("--host and --ssh are mutually exclusive: one names a target stored "
                          "in the local database, the other spells one out")

    deployment: Deployment | None = None
    if args.host:
        store = _store()
        deployment = store.resolve(args.host)
        host = store.require_host(deployment.host)
        spec = deployment.spec.replace(**_spec_overrides(args))
    else:
        host = Host(name=args.ssh or "local", ssh=args.ssh or "")
        spec = DeploySpec(**_spec_overrides(args))

    if args.ssh_port:
        host = replace(host, port=args.ssh_port)
    if args.identity:
        host = replace(host, identity=args.identity)
    if args.accept_new:
        host = replace(host, strict_host_key="accept-new")
    sudo = bool(args.sudo or host.sudo)
    return host.runner(batch=not args.interactive_auth), spec, sudo, deployment


def _deployer(args: argparse.Namespace) -> Deployer:
    runner, spec, sudo, deployment = _resolve(args)
    dep = Deployer(
        spec,
        runner,
        sudo=sudo,
        dry_run=getattr(args, "dry_run", False),
        on_progress=_printer(args),
    )
    #: 库里的那条记录（``--ssh`` 临时目标时是 None）。运维动作完成后回写它
    dep.record = deployment                                        # type: ignore[attr-defined]
    return dep


def _record(dep: Deployer, kind: str, *, ok: bool, detail: str = "", status: Any = None) -> None:
    """把这次动作记进本地库。``--ssh`` 临时目标没有记录可写，直接跳过。

    **永远不抛** —— 记不上账不该让已经成功的运维动作变成失败。
    """
    entry: Deployment | None = getattr(dep, "record", None)
    if entry is None:
        return
    try:
        store = _store()
        if ok and kind in ("deploy", "upgrade", "rollback"):
            store.record_deploy(entry.host, entry.name, image=dep.spec.image, status=status or {})
        store.log_event(entry.host, kind, ok=ok, deployment=entry.name, detail=detail)
    except Exception as exc:                                       # 记账不该阻断
        logging.getLogger("sleight.cli").debug("记流水失败: %s", exc)


def _tunnel_hint(dep: Deployer, *, local_port: int = 19000) -> str:
    """开发机怎么连上去。远程时给出手册 A.5 那条隧道命令。"""
    runner = dep.runner
    target = getattr(runner, "target", "")
    if not target:
        return f"  本机直连   {dep.spec.local_url}"
    port = getattr(runner, "port", None)
    identity = getattr(runner, "identity", None)
    bits = ["ssh", "-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30"]
    if identity:
        bits += ["-i", identity]
    if port:
        bits += ["-p", str(port)]
    bits += ["-L", f"127.0.0.1:{local_port}:127.0.0.1:{dep.spec.port}", target]
    return (
        f"  目标机上   {dep.spec.local_url}\n"
        f"  控制机     {describe(bits)}\n"
        f"             然后本地 http://127.0.0.1:{local_port}\n"
        f"             （或者直接 sleight tunnel --host …，省得记这条）"
    )


# --------------------------------------------------------------------------- #
# 部署
# --------------------------------------------------------------------------- #


def cmd_deploy(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    fresh = dep.existing_token() is None

    if args.dry_run:
        plan = dep.plan()
        _dump(_plan_json(plan)) if args.json else _out(plan.render(show_files=not args.brief))
        return EXIT_PREFLIGHT if plan.blocked else EXIT_OK

    result = dep.apply(pull=not args.no_pull, wait=not args.no_wait, force=args.force)
    _record(dep, "deploy", ok=True, status=result.status,
            detail="有变更" if result.changed else "无变更")
    if args.json:
        _dump({
            "changed": result.changed,
            "token": result.token,
            "url": dep.spec.bound_url,
            "status": result.status,
            "steps": result.steps,
        })
        return EXIT_OK

    _out()
    _out(result.summary if result.changed else "无变更：目标机已经是这个状态")
    _out()
    _out("连接方式")
    _out(_tunnel_hint(dep))
    _out()
    if fresh:
        _out(f"  AUTH_TOKEN  {result.token}")
        _out("              这是新生成的，只在这里完整显示一次；它也存在目标机的")
        _out(f"              {dep.spec.env_path}（权限 600），随时 sleight token 取回")
    else:
        _out(f"  AUTH_TOKEN  {result.token[:8]}…（沿用已有的，sleight token 取完整值）")
    _out()
    _out("  代码")
    _out("    from sleight.providers import CloakBrowserManager")
    _out('    mgr = CloakBrowserManager("http://127.0.0.1:19000")   # token 从 '
         "SLEIGHT_CLOAK_TOKEN 读")
    return EXIT_OK


def _plan_json(plan: Any) -> dict[str, Any]:
    return {
        "blocked": plan.blocked,
        "up_to_date": plan.up_to_date,
        "checks": [
            {"name": c.name, "level": c.level.value, "detail": c.detail, "hint": c.hint}
            for c in plan.checks
        ],
        "changes": plan.changes,
        "commands": [list(c) for c in plan.commands],
        "warnings": plan.warnings,
        "files": plan.files,
    }


def cmd_preflight(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    checks = dep.plan().checks
    if args.json:
        _dump([
            {"name": c.name, "level": c.level.value, "detail": c.detail, "hint": c.hint}
            for c in checks
        ])
    else:
        for check in checks:
            _out(str(check))
    return EXIT_PREFLIGHT if worst(checks) is CheckLevel.FAIL else EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    status = dep.status()
    if args.json:
        _dump(status)
        return EXIT_OK
    container = status["container"]
    api = status["api"]
    _out(f"目标      {status['target']}  {status['dir']}")
    _out(f"容器      {container['status']}  健康 {container['health']}")
    _out(f"镜像      {container['image'] or '—'}")
    _out(f"端口      {', '.join(status['ports']) or '—'}")
    if api:
        _out(f"Manager   浏览器内核 {api.get('binary_version', '?')}  "
             f"profile {api.get('profiles_total', 0)} 个  "
             f"运行中 {api.get('running_count', 0)} 个")
    else:
        _out("Manager   /api/status 无响应")
    if deployed := status["state_file"].get("deployed_at"):
        _out(f"上次部署  {deployed}")
    return EXIT_OK if container["status"] == "running" else EXIT_ERROR


def cmd_logs(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    if args.follow:
        argv = dep.compose_argv("logs", "-f", "--tail", str(args.tail), "manager")
        return dep.runner.stream(argv, cwd=dep.spec.dir)
    _out(dep.logs(tail=args.tail))
    return EXIT_OK


def cmd_upgrade(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    result = dep.upgrade(args.image, backup=not args.no_backup)
    _record(dep, "upgrade", ok=True, status=result.status, detail=f"→ {args.image}")
    _dump({"changed": result.changed, "status": result.status}) if args.json else _out(
        result.summary
    )
    return EXIT_OK


def cmd_rollback(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    result = dep.rollback()
    _record(dep, "rollback", ok=True, status=result.status, detail=f"→ {dep.spec.image}")
    _dump({"status": result.status}) if args.json else _out(result.summary)
    return EXIT_OK


def cmd_backup(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    archive = dep.backup()
    _record(dep, "backup", ok=True, detail=archive)
    _dump({"archive": archive}) if args.json else _out(f"已归档 {dep.runner.label}:{archive}")
    return EXIT_OK


def cmd_destroy(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    if args.purge_data and not _confirm(
        f"这会永久删除 {dep.runner.label}:{dep.spec.data_dir}\n"
        "  里面是 profile 数据库、指纹种子、Cookie 和全部登录状态，删了不可恢复。",
        dep.spec.name,
        assume_yes=args.yes,
    ):
        _err("已取消")
        return EXIT_ERROR
    dep.destroy(purge_data=args.purge_data)
    _record(dep, "destroy", ok=True, detail="连 data/ 一起删" if args.purge_data else "保留 data/")
    _out("容器已删除" + ("，data/ 也删了" if args.purge_data else "，data/ 保留"))
    return EXIT_OK


def cmd_token(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    token = dep.existing_token()
    if not token:
        raise DeployError(f"no AUTH_TOKEN in {dep.runner.label}:{dep.spec.env_path}")
    _dump({"token": token}) if args.json else _out(token)
    return EXIT_OK


def cmd_tunnel(args: argparse.Namespace) -> int:
    dep = _deployer(args)
    if not hasattr(dep.runner, "target"):
        _out(f"本机部署不需要隧道，直接访问 {dep.spec.local_url}")
        return EXIT_OK
    with dep.runner.tunnel(dep.spec.port) as port:
        _out(f"隧道已通  http://127.0.0.1:{port}  →  {dep.runner.label}:{dep.spec.port}")
        if token := dep.existing_token():
            _out(f"token     {token}")
        _out("Web UI、REST API、CDP WebSocket 和 noVNC 全走这一条。Ctrl-C 断开。")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            _out("\n已断开")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 主机清单
# --------------------------------------------------------------------------- #


def cmd_hosts_ls(args: argparse.Namespace) -> int:
    store = _store()
    hosts = store.hosts()
    deployments = store.deployments()
    if args.json:
        _dump([
            {**h.to_dict(), "deployments": [d.to_dict() for d in deployments if d.host == h.name]}
            for h in hosts
        ])
        return EXIT_OK
    if not hosts:
        _out(f"{store.path} 里还没有主机。加一台：")
        _out("  sleight hosts add hk-01 --ssh deploy@1.2.3.4 --dir /srv/cloakbrowser-manager")
        return EXIT_OK
    rows = [
        [
            h.name,
            h.ssh or "(本机)",
            str(h.port or ""),
            "是" if h.sudo else "",
            str(sum(1 for d in deployments if d.host == h.name)),
            h.notes,
        ]
        for h in hosts
    ]
    _out(_table(rows, ["主机", "SSH", "端口", "sudo", "Manager 数", "备注"]))
    _out(f"\n{store.path}")
    return EXIT_OK


def cmd_hosts_add(args: argparse.Namespace) -> int:
    """记一台主机，顺带建一个叫 default 的部署。

    一台没有部署记录的主机是没法用的（``deploy --host X`` 不知道往哪个目录发），
    所以这里一步到位 —— 要在同一台机上跑第二个 Manager 再用 ``deployments add``。
    """
    store = _store()
    store.put_host(Host(
        name=args.name,
        ssh=args.ssh or "",
        port=args.ssh_port,
        identity=args.identity or "",
        sudo=bool(args.sudo),
        strict_host_key="accept-new" if args.accept_new else "",
        notes=args.notes or "",
    ))
    spec = _template_spec(args)
    store.put_deployment(args.name, "default", spec)
    _out(f"已写入 {store.path}")
    _out(f"  主机 {args.name}  →  {args.ssh or '本机'}")
    _out(f"  部署 {args.name}/default  →  {spec.dir}  端口 {spec.port}  镜像 {spec.image}")
    _out(f"\n试一下：sleight preflight --host {args.name}")
    return EXIT_OK


def cmd_hosts_rm(args: argparse.Namespace) -> int:
    store = _store()
    count = len(store.deployments(host=args.name))
    store.delete_host(args.name)
    _out(f"已删掉主机 {args.name} 和它的 {count} 条部署记录（**目标机上的东西没有动**）")
    _out("要真的拆掉 Manager，先 sleight destroy 再删记录")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 部署记录（一台机上的 N 个 Manager）
# --------------------------------------------------------------------------- #


def cmd_deployments_ls(args: argparse.Namespace) -> int:
    store = _store()
    rows_data = store.deployments(host=args.only_host)
    if args.json:
        _dump([d.to_dict() for d in rows_data])
        return EXIT_OK
    if not rows_data:
        _out("还没有部署记录。加一个：")
        _out("  sleight hosts add hk-01 --ssh deploy@1.2.3.4 --dir /srv/cloakbrowser-manager")
        return EXIT_OK
    rows = [
        [
            d.ref,
            d.spec.dir,
            f"{d.spec.bind_ip}:{d.spec.port}",
            d.spec.container_name,
            (d.image or d.spec.image).split("/")[-1],
            d.deployed_at or "从未部署",
        ]
        for d in rows_data
    ]
    _out(_table(rows, ["主机/部署", "目录", "监听", "容器名", "镜像", "最后部署"]))
    return EXIT_OK


def cmd_deployments_add(args: argparse.Namespace) -> int:
    store = _store()
    spec = _template_spec(args)
    store.put_deployment(args.host_name, args.name, spec)
    _out(f"已记下 {args.host_name}/{args.name}  →  {spec.dir}  端口 {spec.port}")
    _out(f"试一下：sleight preflight --host {args.host_name}/{args.name}")
    return EXIT_OK


def cmd_deployments_rm(args: argparse.Namespace) -> int:
    store = _store()
    host_name, _, name = args.ref.partition("/")
    store.delete_deployment(host_name, name or "default")
    _out(f"已删掉记录 {args.ref}（**目标机上的东西没有动**）")
    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    store = _store()
    events = store.events(host=args.only_host, limit=args.limit)
    if args.json:
        _dump([e.to_dict() for e in events])
        return EXIT_OK
    if not events:
        _out("还没有流水。")
        return EXIT_OK
    rows = [
        [e.ts, e.host + ("/" + e.deployment if e.deployment else ""), e.kind,
         "成功" if e.ok else "失败", e.detail[:60]]
        for e in events
    ]
    _out(_table(rows, ["时间", "目标", "动作", "结果", "说明"]))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #


def _ext(args: argparse.Namespace) -> ExtensionOps:
    return ExtensionOps(_deployer(args))


def cmd_ext_ls(args: argparse.Namespace) -> int:
    ops = _ext(args)
    installed = ops.list_installed()
    if args.json:
        from dataclasses import asdict

        _dump([asdict(e) for e in installed])
        return EXIT_OK
    if not installed:
        _out(f"{ops.spec.extensions_dir} 下没有插件。推一个：")
        _out("  sleight ext push ./bypass-paywalls-chrome-clean-master --host …")
        return EXIT_OK
    rows = [
        [e.dirname, e.name or "?", e.version or "?", f"MV{e.manifest_version or '?'}",
         str(e.files), e.container_path]
        for e in installed
    ]
    _out(_table(rows, ["目录", "名字", "版本", "manifest", "文件数", "容器内路径"]))
    for ext in installed:
        for problem in ext.problems:
            _err(f"! {ext.dirname}: {problem}")
    return EXIT_OK


def cmd_ext_push(args: argparse.Namespace) -> int:
    ops = _ext(args)
    ext = ops.push(args.path, name=args.as_name)
    if args.json:
        from dataclasses import asdict

        _dump(asdict(ext))
    else:
        _out(f"已推送 {ext}")
        _out(f"下一步：sleight ext apply{_target_suffix(args)}   # 下发到每个 profile 并停实例")
    return EXIT_OK


def cmd_ext_rm(args: argparse.Namespace) -> int:
    ops = _ext(args)
    ops.remove(args.name)
    _out(f"已删除。运行 sleight ext apply{_target_suffix(args)} 才会从 profile 上摘掉")
    return EXIT_OK


def cmd_ext_apply(args: argparse.Namespace) -> int:
    ops = _ext(args)
    changes = ops.apply(names=args.only or None, restart=not args.no_restart)
    _record(ops.dep, "ext-apply", ok=True,
            detail=f"{sum(1 for c in changes if c.updated)}/{len(changes)} 个 profile 有改动")
    if args.json:
        _dump([
            {"id": c.id, "name": c.name, "updated": c.updated, "stopped": c.stopped,
             "before": c.before, "after": c.after}
            for c in changes
        ])
        return EXIT_OK
    updated = [c for c in changes if c.updated]
    _out(f"\n{len(updated)}/{len(changes)} 个 profile 有改动")
    for change in changes:
        _out(f"  {change.summary}")
    if any(c.stopped for c in changes):
        _out("\n被停掉的实例不用手动拉起 —— 下次 lease() 时 ensure_ready() 会带新参数启动。")
    if args.no_restart and updated:
        _err("\n! 没有停实例：正在跑的浏览器还在用旧参数，改动要到下次启动才生效")
    return EXIT_OK


def cmd_ext_verify(args: argparse.Namespace) -> int:
    ops = _ext(args)
    reports = ops.verify(launch=not args.no_launch, settle=args.settle)
    if args.json:
        _dump([
            {"id": r.id, "name": r.name, "running": r.running, "ok": r.ok,
             "expected": r.expected, "loaded": sorted(r.loaded)}
            for r in reports
        ])
    else:
        for report in reports:
            _out(f"  {report.summary}")
        for report in reports:
            if report.loaded:
                _out(f"\n{report.name or report.id} 的扩展 id：")
                for ext_id in sorted(report.loaded):
                    _out(f"  chrome-extension://{ext_id}")
    return EXIT_OK if all(r.ok for r in reports) else EXIT_ERROR


def cmd_ext_drift(args: argparse.Namespace) -> int:
    ops = _ext(args)
    report = ops.drift()
    if args.json:
        _dump(report)
        return EXIT_OK
    rows = [
        [
            str(p["name"] or p["id"]),
            str(p["status"]),
            str(len(p["configured"])),
            ", ".join(m.rsplit("/", 1)[-1] for m in p["missing"]) or "—",
            ", ".join(u.rsplit("/", 1)[-1] for u in p["unknown"]) or "—",
        ]
        for p in report["profiles"]
    ]
    _out(_table(rows, ["profile", "状态", "已配", "缺", "多余"]))
    if report["consistent"]:
        _out("\n一致：每个 profile 都配了目标机上装的全部插件。")
    else:
        _out("\n不一致。lease() 是随机租实例的，漏掉的那台会造成概率性失败")
        _out("（症状就是「有时候能过付费墙，有时候不能」）。修：")
        _out(f"  sleight ext apply{_target_suffix(args)}")
    return EXIT_OK if report["consistent"] else EXIT_ERROR


def _target_suffix(args: argparse.Namespace) -> str:
    if args.host:
        return f" --host {args.host}"
    if args.ssh:
        return f" --ssh {args.ssh}"
    return ""


# --------------------------------------------------------------------------- #
# profile
# --------------------------------------------------------------------------- #


def cmd_profiles_ls(args: argparse.Namespace) -> int:
    from .deploy.ops import extension_paths

    ops = ProfileOps(_deployer(args))
    profiles = ops.list()
    if args.json:
        _dump(profiles)
        return EXIT_OK
    if not profiles:
        _out("这个 Manager 上还没有 profile。用 ProfileSpec + ensure_profile() 建，"
             "或者在 Manager 的 Web UI 里建。")
        return EXIT_OK
    rows = [
        [
            str(p.get("id", ""))[:8],
            str(p.get("name") or ""),
            str(p.get("status") or ""),
            ",".join(t.get("tag", "") for t in (p.get("tags") or [])) or "—",
            str(len(extension_paths([str(a) for a in (p.get("launch_args") or [])]))),
        ]
        for p in profiles
    ]
    _out(_table(rows, ["id", "名字", "状态", "tags", "扩展数"]))
    return EXIT_OK


def cmd_templates(args: argparse.Namespace) -> int:
    """把模板和字段解释打出来 —— 和界面上看到的是同一份定义。"""
    if args.json:
        _dump({
            "deploy_templates": [t.to_dict() for t in DEPLOY_TEMPLATES],
            "profile_presets": [p.to_dict() for p in PROFILE_PRESETS],
            "help": {k: v.to_dict() for k, v in FIELD_HELP.items()},
        })
        return EXIT_OK
    _out("部署模板（hosts add / deployments add 的 --template）")
    for t in DEPLOY_TEMPLATES:
        _out(f"\n  {t.key:12} {t.label} —— {t.summary}")
        _out(f"               {t.detail}")
    _out("\n\n实例身份模板（profiles create 的 --preset）")
    _out("  预设的价值是**保证指纹自洽**：平台、时区、语言、GPU 串必须是同一台机器上")
    _out("  可能出现的组合。")
    for p in PROFILE_PRESETS:
        d = p.to_dict()
        _out(f"\n  {d['key']:12} {d['label']} —— {d['summary']}")
        _out(f"               {d['platform']} · {d['timezone']} · {d['locale']}")
    return EXIT_OK


def cmd_profiles_create(args: argparse.Namespace) -> int:
    """按身份模板建一个实例。**幂等**：同名的会被更新而不是再建一个。"""
    dep = _deployer(args)
    width, _, height = (args.screen or "1920x1080").partition("x")
    spec = profile_spec_from(
        args.preset, args.name,
        proxy=args.proxy, geoip=args.geoip, headless=args.headless,
        notes=args.notes, tags=tuple(t.strip() for t in (args.tags or "").split(",") if t.strip()),
        screen_width=int(width or 1920), screen_height=int(height or 1080),
        fingerprint_seed=args.seed,
    )
    with dep.connect() as mgr:
        info = mgr.ensure_profile(spec)
    _record(dep, "profile-create", ok=True, detail=f"{info.name}（{args.preset}）")
    if args.json:
        _dump({"id": info.id, "name": info.name, "tags": sorted(info.tags)})
    else:
        _out(f"实例 {info.name} 就绪（{info.id}）")
        _out("建好不会自动启动 —— 下次 lease() 时 ensure_ready() 会拉起它。")
    return EXIT_OK


def cmd_profiles_launch(args: argparse.Namespace) -> int:
    ProfileOps(_deployer(args), on_progress=_out).launch(args.profile)
    return EXIT_OK


def cmd_profiles_stop(args: argparse.Namespace) -> int:
    ops = ProfileOps(_deployer(args), on_progress=_out)
    if args.profile:
        ops.stop(args.profile)
    else:
        stopped = ops.stop_all()
        _out(f"停了 {len(stopped)} 个。登录态在 /data 里，不会丢。")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Web 界面
# --------------------------------------------------------------------------- #


def cmd_ui(args: argparse.Namespace) -> int:
    from .deploy.api import serve

    return serve(host=args.bind_ui, port=args.ui_port, token=args.ui_token)


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #


def _target_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("目标机")
    g.add_argument("--host", metavar="NAME[/部署名]",
                   help="本地库里配好的目标。一台机跑多个 Manager 时用 主机/部署名")
    g.add_argument("--ssh", metavar="USER@HOST", help="直接给 ssh 目标（不写 = 本机）")
    g.add_argument("--ssh-port", type=int, metavar="N", help="SSH 端口")
    g.add_argument("--identity", metavar="PATH", help="SSH 私钥")
    g.add_argument("--accept-new", action="store_true",
                   help="首次连接自动信任主机指纹（StrictHostKeyChecking=accept-new）")
    g.add_argument("--interactive-auth", action="store_true",
                   help="允许 ssh 交互提示（默认 BatchMode=yes，缺 key 时当场失败而不是挂住）")
    g.add_argument("--sudo", action="store_true",
                   help="建目录/改权限时用 sudo -n（目标机需配好免密 sudo）")
    g.add_argument("--dir", metavar="PATH", help=f"部署目录（默认 {DeploySpec().dir}）")
    return p


def _spec_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("部署参数")
    g.add_argument("--image", metavar="REF", help=f"镜像（默认 {DEFAULT_IMAGE}）")
    g.add_argument("--port", type=int, metavar="N", help="宿主机端口（默认 9000）")
    g.add_argument("--bind", metavar="IP", help="宿主机监听地址（默认 127.0.0.1）")
    g.add_argument("--expose", action="store_true", default=None,
                   help="确认要监听非回环地址。token 在 HTTP 上是明文，务必有防火墙/VPN/TLS")
    g.add_argument("--shm-size", metavar="SIZE", help="/dev/shm 大小，整个容器共享（默认 5gb）")
    g.add_argument("--project", metavar="NAME", help="compose 项目名（默认 cloakbrowser）")
    g.add_argument("--container", metavar="NAME", help="容器名（默认 cloakbrowser-manager）")
    g.add_argument("--allow-latest", action="store_true", default=None,
                   help="允许用 latest 这种不可追溯的标签")
    g.add_argument("--template", metavar="KEY",
                   choices=[t.key for t in DEPLOY_TEMPLATES],
                   help="按模板打底（sleight templates 看有哪些），命令行参数再覆盖它")
    return p


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    target = _target_parser()
    spec = _spec_parser()

    parser = argparse.ArgumentParser(
        prog="sleight",
        description="部署与运维 CloakBrowser Manager。",
        epilog="手册：https://github.com/yuanqimanong/sleight/blob/main/docs/",
    )
    parser.add_argument("--version", action="version", version=f"sleight {__version__}")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")
    parser.add_argument("-q", "--quiet", action="store_true", help="不打进度")
    parser.add_argument("-v", "--verbose", action="store_true", help="打 debug 日志")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    def add(name: str, help_: str, *parents: argparse.ArgumentParser) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, description=help_, parents=list(parents))

    # —— 部署 ——
    p = add("deploy", "部署或更新一个 Manager（幂等，可反复跑）", target, spec)
    p.add_argument("--dry-run", action="store_true", help="只打算做什么，不动目标机")
    p.add_argument("--brief", action="store_true", help="dry-run 时不打文件全文")
    p.add_argument("--no-pull", action="store_true", help="不 pull（用本地已有镜像）")
    p.add_argument("--no-wait", action="store_true", help="不等 healthy")
    p.add_argument("--force", action="store_true", help="体检有 FAIL 也继续（不推荐）")
    p.set_defaults(func=cmd_deploy)

    p = add("templates", "看部署模板、实例身份模板，和每个选项的解释")
    p.set_defaults(func=cmd_templates)

    p = add("preflight", "只体检，不部署", target, spec)
    p.set_defaults(func=cmd_preflight)

    p = add("status", "看容器、健康状态和 /api/status", target)
    p.set_defaults(func=cmd_status)

    p = add("logs", "看 Manager 日志", target)
    p.add_argument("-f", "--follow", action="store_true", help="持续跟随")
    p.add_argument("--tail", type=int, default=200, metavar="N")
    p.set_defaults(func=cmd_logs)

    p = add("upgrade", "换镜像（默认先做一次停机备份）", target)
    p.add_argument("image", help="新的镜像标签或摘要")
    p.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    p.set_defaults(func=cmd_upgrade)

    p = add("rollback", "回到状态文件里记着的上一个镜像", target)
    p.set_defaults(func=cmd_rollback)

    p = add("backup", "停机归档整个 data/", target)
    p.set_defaults(func=cmd_backup)

    p = add("destroy", "停并删容器。默认保留 data/", target)
    p.add_argument("--purge-data", action="store_true",
                   help="连 data/ 一起删 —— 全部登录态和 profile 都没了，不可恢复")
    p.add_argument("--yes", action="store_true", help="跳过确认")
    p.set_defaults(func=cmd_destroy)

    p = add("token", "从目标机的 .env 取回 AUTH_TOKEN", target)
    p.set_defaults(func=cmd_token)

    p = add("tunnel", "开一条 SSH 隧道，本地访问远程 Manager", target)
    p.set_defaults(func=cmd_tunnel)

    # —— 主机清单 ——
    p = add("hosts", "管理 ~/.sleight/hosts.toml")
    hsub = p.add_subparsers(dest="hosts_command", metavar="<子命令>")
    hp = hsub.add_parser("ls", help="列出配好的主机")
    hp.set_defaults(func=cmd_hosts_ls)
    hp = hsub.add_parser("add", help="记一台主机（顺带建一个 default 部署）",
                         parents=[target, spec])
    hp.add_argument("name", help="起个名字，之后 --host 用它")
    hp.add_argument("--notes", metavar="TEXT", help="备注，比如「香港那台」")
    hp.set_defaults(func=cmd_hosts_add)
    hp = hsub.add_parser("rm", help="从库里删掉（不动目标机）")
    hp.add_argument("name")
    hp.set_defaults(func=cmd_hosts_rm)
    p.set_defaults(func=lambda a: (_err("用 sleight hosts ls|add|rm"), EXIT_USAGE)[1])

    # —— 部署记录 ——
    p = add("deployments", "一台主机上的 N 个 Manager")
    dsub = p.add_subparsers(dest="deployments_command", metavar="<子命令>")
    dp = dsub.add_parser("ls", help="列出全部部署记录")
    dp.add_argument("--only-host", metavar="NAME", help="只看这台机的")
    dp.set_defaults(func=cmd_deployments_ls)
    dp = dsub.add_parser("add", help="在一台机上再记一个 Manager", parents=[spec])
    dp.add_argument("host_name", metavar="HOST", help="哪台主机（要先 hosts add 过）")
    dp.add_argument("name", help="这个 Manager 的名字，比如 second")
    # --dir 平时住在"目标机"那组里（临时目标也要用），这里得单独给一份
    dp.add_argument("--dir", metavar="PATH",
                    help=f"部署目录，同一台机上不能和别的部署重（默认 {DeploySpec().dir}）")
    dp.set_defaults(func=cmd_deployments_add)
    dp = dsub.add_parser("rm", help="删掉一条记录（不动目标机）")
    dp.add_argument("ref", metavar="HOST/NAME")
    dp.set_defaults(func=cmd_deployments_rm)
    p.set_defaults(func=lambda a: (_err("用 sleight deployments ls|add|rm"), EXIT_USAGE)[1])

    # —— 流水 ——
    p = add("history", "看部署/备份/升级/销毁的流水")
    p.add_argument("--only-host", metavar="NAME", help="只看这台机的")
    p.add_argument("-n", "--limit", type=int, default=30, metavar="N")
    p.set_defaults(func=cmd_history)

    # —— 插件 ——
    p = add("ext", "浏览器插件的推送、下发与验证")
    esub = p.add_subparsers(dest="ext_command", metavar="<子命令>")
    ep = esub.add_parser("ls", help="目标机上装了哪些插件", parents=[target])
    ep.set_defaults(func=cmd_ext_ls)
    ep = esub.add_parser("push", help="把本地插件目录推上去（校验 MV3 + 修权限）",
                         parents=[target])
    ep.add_argument("path", help="本地插件目录，里面要有 manifest.json")
    ep.add_argument("--as", dest="as_name", metavar="NAME", help="目标机上的目录名")
    ep.set_defaults(func=cmd_ext_push)
    ep = esub.add_parser("rm", help="删掉目标机上的插件目录", parents=[target])
    ep.add_argument("name")
    ep.set_defaults(func=cmd_ext_rm)
    ep = esub.add_parser(
        "apply", help="同步到每一个 profile 的 launch_args，然后停实例", parents=[target]
    )
    ep.add_argument("--only", nargs="*", metavar="DIR", help="只启用这些插件目录")
    ep.add_argument("--no-restart", action="store_true",
                    help="只改配置不停实例（正在跑的会继续用旧参数）")
    ep.set_defaults(func=cmd_ext_apply)
    ep = esub.add_parser("verify", help="查浏览器里到底加载了哪些扩展", parents=[target])
    ep.add_argument("--no-launch", action="store_true", help="不拉起停着的实例")
    ep.add_argument("--settle", type=float, default=6.0, metavar="SEC",
                    help="等 service worker 起来的时间（默认 6 秒）")
    ep.set_defaults(func=cmd_ext_verify)
    ep = esub.add_parser("drift", help="磁盘上装了什么 vs 每个 profile 配了什么",
                         parents=[target])
    ep.set_defaults(func=cmd_ext_drift)
    p.set_defaults(func=lambda a: (_err("用 sleight ext ls|push|apply|verify|drift|rm"),
                                   EXIT_USAGE)[1])

    # —— profile ——
    p = add("profiles", "profile 的查、启、停")
    psub = p.add_subparsers(dest="profiles_command", metavar="<子命令>")
    pp = psub.add_parser("ls", help="列出全部 profile", parents=[target])
    pp.set_defaults(func=cmd_profiles_ls)
    pp = psub.add_parser("create", help="按身份模板建一个实例", parents=[target])
    pp.add_argument("name", help="实例名，Manager 里唯一")
    pp.add_argument("--preset", default="windows_us",
                    choices=[x.key for x in PROFILE_PRESETS],
                    help="身份模板：决定平台/时区/语言/GPU 串，必须自洽（默认 windows_us）")
    pp.add_argument("--proxy", metavar="URL", help="socks5://user:pass@host:port")
    pp.add_argument("--tags", metavar="A,B", help="逗号分隔，Pool 靠它路由")
    pp.add_argument("--screen", default="1920x1080", metavar="WxH")
    pp.add_argument("--geoip", action="store_true", help="由代理出口 IP 推导时区和语言")
    pp.add_argument("--headless", action="store_true", help="无头（反检测场景通常别开）")
    pp.add_argument("--seed", type=int, metavar="N", help="固定指纹种子，指纹才可复现")
    pp.add_argument("--notes", metavar="TEXT")
    pp.set_defaults(func=cmd_profiles_create)

    pp = psub.add_parser("launch", help="拉起一个（id 或名字）", parents=[target])
    pp.add_argument("profile")
    pp.set_defaults(func=cmd_profiles_launch)
    pp = psub.add_parser("stop", help="停一个；不给参数则停全部", parents=[target])
    pp.add_argument("profile", nargs="?")
    pp.set_defaults(func=cmd_profiles_stop)
    p.set_defaults(func=lambda a: (_err("用 sleight profiles ls|create|launch|stop"), EXIT_USAGE)[1])

    # —— 界面 ——
    p = add("ui", "启动 Web 界面（需要 pip install \"sleight[ui]\"）")
    p.add_argument("--bind", dest="bind_ui", default="127.0.0.1", metavar="IP",
                   help="监听地址。默认只听本机 —— 这个界面能执行 ssh 和 docker")
    p.add_argument("--port", dest="ui_port", type=int, default=8700, metavar="N")
    p.add_argument("--token", dest="ui_token", metavar="TOKEN",
                   help="访问口令。绑到非回环地址时**必须**给")
    p.set_defaults(func=cmd_ui)

    return parser


# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    """入口。

    :param argv: 参数列表，``None`` 表示用 ``sys.argv[1:]``
    :returns: 退出码 —— 0 成功、1 失败、2 用法错、3 体检没过、130 Ctrl-C、
        141 输出管道被下游关掉（``| head`` 之类）
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return EXIT_USAGE

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return int(args.func(args) or EXIT_OK)
    except PreflightFailed as exc:
        _err(f"体检没过：\n{exc}")
        return EXIT_PREFLIGHT
    except (DeployError, SleightError, ValueError) as exc:
        _err(f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR
    except BrokenPipeError:
        # 下游（head / less）把管道关了。这不是错误，但如果不处理，解释器退出时还会
        # 往已关闭的 fd 上刷一次缓冲，再打一段 "Exception ignored" 的噪音。
        # 把 stdout 换成 devnull 就干净了 —— 但它只是消噪，stdout 不是真文件描述符
        # 时（被捕获、被嵌入）拿不到 fileno，那不该反过来把断管变成崩溃。
        with suppress(Exception):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_SIGPIPE
    except KeyboardInterrupt:
        _err("\n已中断")
        return EXIT_INTERRUPT


if __name__ == "__main__":                                # pragma: no cover
    sys.exit(main())

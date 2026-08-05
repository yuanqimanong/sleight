# 部署与运维 CLI

`sleight` 命令把手册[附录 A](详细文档手册.md) 的手工部署，和插件运维里那串 curl +
python 单行命令，收成了可重跑的子命令。

两种形态，**同一套代码**：

```
本机部署    装了 sleight 和 docker 的机器上直接 sleight deploy
远程部署    控制机装 sleight，目标机只要有 docker 和 sshd
```

区别只是换一个 Runner。所以不存在"本机能跑、远程跑不通"这种分裂。

---

## 目录

- [1. 三分钟](#1-三分钟)
- [2. 装什么](#2-装什么)
- [3. 目标机怎么指定](#3-目标机怎么指定)
- [4. 部署](#4-部署)
- [5. 日常运维](#5-日常运维)
- [6. 浏览器插件](#6-浏览器插件)
- [7. Web 界面](#7-web-界面)
- [8. 内建的护栏](#8-内建的护栏)
- [9. 当库来用](#9-当库来用)
- [10. 退出码与排错](#10-退出码与排错)

---

## 1. 三分钟

**本机**（这台机器上有 docker）：

```bash
sleight deploy --dir ~/cloakbrowser-manager
```

**远程**：

```bash
sleight deploy --ssh deploy@1.2.3.4 --dir /srv/cloakbrowser-manager --sudo
```

跑完会打印 `AUTH_TOKEN`、访问地址，和一条现成的 SSH 隧道命令。然后：

```bash
sleight status --ssh deploy@1.2.3.4
```

先看它要做什么，什么都不动：

```bash
sleight deploy --ssh deploy@1.2.3.4 --dry-run
```

`--dry-run` 打出来的 compose 和 `.env` 全文，**就是**真正会写上去的字节。

---

## 2. 装什么

| 在哪台机 | 需要 |
|---|---|
| 控制机 | `pip install sleight`；远程时还要有 `ssh`（Windows 10+ 自带） |
| 目标机 | docker + compose v2 插件；远程时还要 sshd。**不需要装 sleight** |

命令行只用标准库。只有 Web 界面要多装两个包：

```bash
pip install "sleight[ui]"
```

远程执行走系统 `ssh` 二进制而不是 paramiko，所以 `~/.ssh/config`、`ProxyJump`、
ssh-agent、`known_hosts` 全都照常生效 —— 你平时怎么连，sleight 就怎么连。

---

## 3. 目标机怎么指定

三种写法，优先级从左到右递增：

```bash
sleight status                          # 本机
sleight status --ssh deploy@1.2.3.4     # 临时指定
sleight status --host hk-01             # 用 ~/.sleight/hosts.toml 里配好的
```

配一台常用的：

```bash
sleight hosts add hk-01 --ssh deploy@1.2.3.4 --ssh-port 22 --identity ~/.ssh/id_ed25519 --dir /srv/cloakbrowser-manager --port 9000 --sudo
```

写出来是这样，手改也行：

```toml
[hosts.hk-01]
ssh = "deploy@1.2.3.4"
port = 22
identity = "~/.ssh/id_ed25519"
sudo = true

[hosts.hk-01.deploy]
dir = "/srv/cloakbrowser-manager"
port = 9000
```

`[hosts.X.deploy]` 里可以放 `DeploySpec` 的任意字段（`image`、`shm_size`、`bind_ip`、
`container_name`…）。命令行参数覆盖它，**没给的参数不会把配置里的值打掉**。

> **清单里没有 token。** 每台机的 `AUTH_TOKEN` 就在它自己的 `.env`（权限 600）里 ——
> 能 SSH 上去就能读到，控制机上再存一份只是多一个泄漏点。要用时 `sleight token
> --host hk-01` 现取。

### 关于 SSH 认证

默认 `BatchMode=yes`：**缺 key 时当场失败，而不是挂在一个你看不见的密码提示前**。
用密码认证的话加 `--interactive-auth`。首次连接可以用 `--accept-new` 自动信任指纹
（不给的话尊重你自己的 ssh 配置，sleight 不替你做主）。

一次部署要发几十条命令，所以非 Windows 上会自动开 `ControlMaster` 复用连接 ——
只认证一次。

### 关于 sudo

`--sudo` 只影响**建目录和改权限**，docker 命令永远不带 sudo（当前用户应该在
docker 组里）。而且用的是 `sudo -n`（非交互）：目标机没配免密 sudo 就当场报错，
不会挂死。

不想配 sudo 就把部署目录放在自己有权限的地方：`--dir ~/cloakbrowser-manager`。

---

## 4. 部署

```bash
sleight deploy [--host NAME | --ssh USER@HOST] [参数…]
```

| 参数 | 说明 |
|---|---|
| `--dir` | 部署目录，默认 `/srv/cloakbrowser-manager` |
| `--image` | 镜像，默认 `cloakhq/cloakbrowser-manager:v0.0.10` |
| `--port` | 宿主机端口，默认 9000 |
| `--bind` | 监听地址，默认 `127.0.0.1` |
| `--expose` | 确认要监听非回环地址（见下） |
| `--shm-size` | `/dev/shm`，默认 `5gb`，**整个容器共享** |
| `--project` / `--container` | compose 项目名 / 容器名，一台机上跑第二个 Manager 时要改 |
| `--dry-run` | 只打算做什么。`--brief` 则不打文件全文 |
| `--no-pull` / `--no-wait` | 不拉镜像 / 不等 healthy |
| `--force` | 体检有 FAIL 也继续（不推荐） |

`deploy` 是**幂等**的：配置没变、容器在跑，它连 `up -d` 都不发，更不会换 token 或
动 `data/`。所以可以放心反复跑，也可以拿它当"确保是这个状态"用。

改了参数（比如 `--port 9100`）再跑，它会重写 `.env` 并重建容器，`AUTH_TOKEN` 保持不变。

### 只体检

```bash
sleight preflight --host hk-01
```

查的东西：能不能连上、docker 与 compose v2、端口占用（被自己的容器占着算重新部署，
不算冲突）、部署目录权限、内存、磁盘、镜像、以及**有没有别的容器已经挂了同一个
`/data`**（两个 Manager 共用一个数据目录会互相踩 profile 数据库）。

### 部署之后

```bash
sleight token --host hk-01            # 取回完整 AUTH_TOKEN
sleight tunnel --host hk-01           # 开隧道，替代手册 A.5 那个 .bat
```

`tunnel` 一条隧道同时承载 Web UI、REST API、CDP WebSocket 和 noVNC —— 所有实例共用
Manager 的同一个端口。

```python
from sleight.providers import CloakBrowserManager

mgr = CloakBrowserManager("http://127.0.0.1:19000")   # token 从 SLEIGHT_CLOAK_TOKEN 读
```

---

## 5. 日常运维

```bash
sleight status --host hk-01                 # 容器 + 健康 + /api/status + 端口映射
sleight logs --host hk-01 -f --tail 200
sleight backup --host hk-01                 # 停机归档整个 data/
sleight upgrade --host hk-01 cloakhq/cloakbrowser-manager:v0.0.11
sleight rollback --host hk-01               # 回到上一个镜像
sleight destroy --host hk-01                # 停并删容器，data/ 保留
```

`backup` **必须停机**：在线复制 SQLite 会拿到一个和浏览器用户目录时间点不一致的库，
恢复出来的 profile 可能起不来。归档完自动重启，打包失败也会重启。

打包是**在一个一次性 root 容器里**做的，不需要 sudo。原因：`data/` 里的浏览器用户目录
是 Manager 以 root 身份写的，宿主机上的普通用户 `tar` 会一路 `Permission denied` ——
而且会留下一个**看着像备份的残档**（实测 12 条 / 1.9 KB，真 profile 的文件一个都没进去，
而完整归档是 238 条 / 134 KB）。这比没有备份危险得多，所以现在是：先写 `.part`，
成功了才改名，失败绝不留下 `.tar.gz`。归档最后会 `chown` 回当前用户，免得删旧备份还要 sudo。

`destroy --purge-data` 同理：先按普通用户 `rm`，遇到 root 拥有的文件自动换 root 容器。

`upgrade` 默认先做一次备份（`--no-backup` 跳过），然后改 `.env` 的 `MANAGER_IMAGE`、
`pull`、`up -d --force-recreate`，并把旧镜像记进 `.sleight-deploy.json` ——
`rollback` 就是靠它。

升级后的验收（命令不替你做，得你自己看）：profile 数量/ID/代理未变、抽样 profile
能启动并通过 CDP 操作、Cookie 和登录状态仍可用。**先恢复少量任务观察错误率，再恢复
全部流量。**

### profile

```bash
sleight profiles ls --host hk-01
sleight profiles launch --host hk-01 Win-US-02      # id 或名字都行
sleight profiles stop --host hk-01 Win-US-02
sleight profiles stop --host hk-01                  # 不给名字 = 全部停
```

停实例**不会丢登录态** —— 那些在 `/data/profiles/<id>/` 里。真正会丢的是删 profile。

---

## 6. 浏览器插件

这一节对应的是最容易出错的那套流程。先说清楚它为什么容易错：

- 插件不由爬虫加载，只能靠 Chromium 启动参数。`--load-extension` 必须和
  `--disable-extensions-except` **成对出现**，否则前者指定的插件也会被一并禁掉。
- 两个参数里填的是**容器内**路径（`/data/extensions/…`），不是宿主机路径。填错
  Chromium 静默不加载。
- `PUT /api/profiles/{id}` 是**整字段替换**。手工加一个插件时忘了把原有路径写进去，
  等于把旧插件摘掉了。
- 参数只在浏览器**进程启动时**读。改完必须停一次实例。
- 必须遍历**每一个** profile。`lease()` 默认随机租一个空闲实例，漏掉一个就意味着
  有一定概率租到没插件的那台 —— 症状是「有时候能过付费墙，有时候不能」。
- 容器里的浏览器进程不是 root（实测 uid 1001），root 独占的目录它读不到。

完整流程现在是三条命令：

```bash
sleight ext push ./plugins/bypass-paywalls-chrome-clean-master --host hk-01
sleight ext apply --host hk-01
sleight ext verify --host hk-01
```

**`push`** 先在本地校验 MV3（Chromium 146 已不支持 MV2，传上去也不会加载，所以不合格
就根本不传），整目录替换式地传过去（旧版本删掉的文件不会残留），`chmod -R a+rX`
修权限，最后从容器里读一次 `manifest.json` 确认挂载和权限都对。

**`apply`** 重算每个 profile 的两个参数：读现值 → 摘掉旧的扩展参数 → **其它
`launch_args` 原样保留** → 按目标机上装了的插件重新加上 → PUT。然后停掉在跑的实例。
不用手动拉起，下次 `lease()` 时 `ensure_ready()` 会带新参数启动。

- `--only bpc adguard` 只启用其中几个，顺序即参数里的顺序
- `--no-restart` 只改配置不停实例（那些实例会继续用旧参数跑）

**`verify`** 是决定性检查，不用跑爬虫：拉起实例，查 `/cdp/json/list` 里有没有
`chrome-extension://<32位id>`。MV3 的 service worker 起来要几秒，所以它会轮询
（`--settle` 调等待上限）。

还有两条：

```bash
sleight ext ls --host hk-01           # 装了什么，MV几，文件数，权限对不对
sleight ext drift --host hk-01        # 磁盘上装了什么 vs 每个 profile 配了什么
```

`drift` 专门抓那个最难查的症状：**只改了一部分 profile**。不一致时退出码是 1，
可以直接进监控。

---

## 7. Web 界面

```bash
pip install "sleight[ui]"
sleight ui
```

打开 <http://127.0.0.1:8700>。界面能做：加主机、体检 + 预览、部署（进度实时流式打出来）、
看状态和日志、备份、profile 启停、插件推送/下发/验证/漂移。

拉镜像要几分钟，所以长动作走后台 job + SSE，不是一个转圈等超时的 POST。

> ⚠️ **这个界面能在你的目标机上执行 ssh 和 docker 命令**，等于一个远程执行入口。
> 所以它默认只监听 `127.0.0.1`；要绑到别的地址**必须**同时给口令，否则拒绝启动：
>
> ```bash
> sleight ui --bind 0.0.0.0 --port 8700 --token "$(openssl rand -hex 16)"
> ```

界面本身不存任何 Manager token，要用时现从目标机的 `.env` 读。

---

## 8. 内建的护栏

这些不是可选项，是写死在代码里的。每一条都对应手册里的一句"禁止"或一次真实事故。

| 护栏 | 为什么 |
|---|---|
| 存量 `AUTH_TOKEN` 绝不无声更换 | 换掉等于所有在用的客户端同时 401 |
| 镜像标签是 `latest` 直接拒绝（除非 `--allow-latest`） | 不可追溯的生产版本 |
| 没有标签也没有摘要同样拒绝 | 隐式 `latest`，一样的问题 |
| 非回环监听必须显式 `--expose` | token 在 HTTP 上是明文 |
| 另一个容器已挂同一个 `data/` → 拒绝部署 | 两个 Manager 会互相踩 profile 数据库 |
| `destroy` 永不带 `-v` | `down -v` 会连卷一起删 |
| 删 `data/` 要 `--purge-data`，且非交互时必须再加 `--yes` | 全部登录态在里面，不可逆 |
| `upgrade` 默认先停机备份 | 没有可回滚的数据就不该动生产 |
| `.env` 权限 600，内容走 stdin 不进 argv | 进 argv 就会出现在目标机的进程列表里 |
| 文件先写临时文件再 `mv` | 中途断线不会留下半个 `.env` |
| `sudo -n`，且只用于建目录/改权限 | 交互式 sudo 在 SSH 上会挂死 |
| 建目录后只 `chown` 顶层，**不递归** | `data/` 里是容器写的浏览器用户数据 |
| 备份/删 `data/` 借一次性 root 容器，不要求 sudo | 那些文件归 root，普通用户读不了也删不掉 |
| 打包失败绝不留下 `.tar.gz` | 一个只有十几条的残档看着和真备份一样 |
| compose 里是 `${AUTH_TOKEN:?…}` | `.env` 丢了要当场报错，而不是拉起一个免鉴权的 Manager |

---

## 9. 当库来用

CLI 能做的，Python 里都能做 —— 想接进自己的运维脚本时用这个。

```python
from sleight.deploy import DeploySpec, Deployer, SSHRunner
from sleight.deploy.ops import ExtensionOps

spec = DeploySpec(dir="/srv/cloakbrowser-manager", port=9000)
runner = SSHRunner("deploy@1.2.3.4", identity="~/.ssh/id_ed25519")

with runner:
    dep = Deployer(spec, runner, sudo=True, on_progress=print)

    plan = dep.plan()                     # 只读，什么都不动
    if plan.blocked:
        raise SystemExit(plan.render())

    result = dep.apply()
    print(result.summary, result.token)

    ExtensionOps(dep).apply()             # 下发插件 + 停实例

    with dep.connect() as mgr:            # 远程会自动开隧道，退出时拆掉
        for profile in mgr.list_profiles():
            print(profile["name"], profile["status"])
```

`dep.connect()` 给出的就是一个普通的
[`CloakBrowserManager`](详细文档手册.md#附录-bmanager-http-api-速查)，
`lease()` / `ensure_profile()` 那一套照常用。

`on_progress` 是每完成一步的回调 —— CLI 用它打进度，Web 界面用它推 SSE。

---

## 10. 退出码与排错

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 失败（也包括 `status` 说容器没跑、`ext drift` 发现不一致） |
| 2 | 用法错 |
| 3 | 体检没过 |
| 130 | Ctrl-C |

`--json` 让每个命令输出机器可读的结果，`-v` 打 debug 日志（含每一条发出去的命令）。

几个常见情况：

**`the docker socket is not accessible`** —— 当前用户不在 docker 组：
`sudo usermod -aG docker $USER`，然后**重新登录**（组成员身份只对新会话生效）。

**`ssh to X was refused`** —— BatchMode 下不会弹密码框。配好 key（`ssh-copy-id`）
或者加 `--interactive-auth`。

**`port 9000 is already in use by something else`** —— 换 `--port`，或者
`ss -ltnp | grep :9000` 看是谁。

**`container … did not become healthy`** —— 看日志：`sleight logs --host X --tail 200`。
常见原因是 `/dev/shm` 太小或内存不够。

**插件推上去了但浏览器里没有** —— 按顺序查：`sleight ext ls`（MV几、权限对不对）
→ `sleight ext drift`（profile 配了没有）→ `sleight ext verify`（浏览器里到底有没有）。
三个里第一个报问题的那个就是原因。

**`docker pull` 卡到 `context deadline exceeded`** —— 你的 shell 有代理但 daemon 没有。
daemon 是 systemd 服务，不继承你的环境。`sleight preflight` 会先把这条指出来并给出
`/etc/systemd/system/docker.service.d/proxy.conf` 的写法。

**拉起一个 profile 很慢** —— 正常。`POST /launch` 是同步的，要等 Chromium 真的起来；
实测冷启动约 69 秒（3.8 GB 内存的机器，含起 Xvnc 和写默认书签）。sleight 给
launch/stop 用的是 300 秒超时而不是通用的 15 秒 —— 否则会在浏览器**其实已经起来**的
情况下抛 `ConnectionError`。

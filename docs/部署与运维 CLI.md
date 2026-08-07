# 部署与运维

把 CloakBrowser Manager 部署到**本机**或**任意一台能 SSH 上去的机器**，然后日常运维它。
两条等价的路：命令行，或者 `sleight ui` 的网页界面。

> 只讲怎么用。想知道底下到底做了什么，看
> [详细文档手册 · 附录 A](详细文档手册.md#附录-acloakbrowser-manager-docker-部署)。

- [最佳实践：从零到能跑](#最佳实践从零到能跑)
- [CLI 还是 Web](#cli-还是-web)
- [目标机与多 Manager](#目标机与多-manager)
- [日常运维](#日常运维)
- [浏览器插件](#浏览器插件)
- [清理：四种「删」](#清理四种删)
- [内建的护栏](#内建的护栏)
- [当库来用](#当库来用)
- [退出码与排错](#退出码与排错)

---

## 最佳实践：从零到能跑

### 前提

目标机上要有 **docker + compose v2**，当前用户在 `docker` 组里：

```bash
sudo apt install -y docker.io docker-compose-v2      # Debian/Ubuntu/Kali
sudo usermod -aG docker "$USER"                       # 之后要重新登录
```

控制机（跑 sleight 的这台）只要 Python ≥ 3.11 和 `ssh`：

```bash
pip install sleight            # 部署引擎只用标准库
pip install "sleight[ui]"      # 想用网页界面再加这个
```

### 本机部署

```bash
sleight preflight                     # 先体检，不动任何东西
sleight deploy                        # 幂等，反复跑没关系
sleight token                         # 取 AUTH_TOKEN
```

三条命令。`preflight` 会把「docker 装了没、端口占没占、内存够不够、daemon 有没有代理」
一次性说清楚，**部署本身也会先体检一遍**，所以急的话可以直接 `deploy`。

### 远程部署

先配好 SSH 免密（`ssh-copy-id`），确认 `ssh 目标机 echo ok` 能通，然后：

```bash
sleight hosts add hk-01 --ssh deploy@10.0.0.12 --template standard \
        --dir /srv/cloakbrowser-manager --sudo

sleight preflight --host hk-01
sleight deploy    --host hk-01
sleight token     --host hk-01
```

`hosts add` 会**当场测一次连接**，连不上就不入库 —— 不会让你部署到一半才发现 SSH 没配好。

### 用起来

Manager 默认只绑在目标机的 `127.0.0.1`，所以远程要开一条隧道：

```bash
sleight tunnel --host hk-01
# 隧道已通  http://127.0.0.1:39217  →  deploy@10.0.0.12:9000
# token     f174…
```

本地端口是**自动挑一个空闲的**，每次都不一样 —— 照着它打印的那个用。前台开着，
Ctrl-C 断开。Web UI、REST API、CDP WebSocket、noVNC 全走这一条。

```python
import os
from sleight.providers import CloakBrowserManager, ProfileSpec

mgr = CloakBrowserManager("http://127.0.0.1:39217")   # 用隧道打印的那个端口
                                                     # token 从 SLEIGHT_CLOAK_TOKEN 读

info = mgr.ensure_profile(ProfileSpec.windows_us("hk-01", proxy=os.environ["PROXY"]))
with mgr.lease(instance_id=info.id) as inst, inst.session(human=True) as s:
    s.open("https://example.com")
    print(s.title())
```

```bash
export SLEIGHT_CLOAK_TOKEN=$(sleight token --host hk-01)
```

### 选多大

```bash
sleight templates          # 看模板，以及每个选项填错会怎样
```

| 模板 | 实例数 | 建议内存 |
|---|---|---|
| `trial` | 1–2 | 4 GB 也能起 |
| `standard` | ~3 | 8 GB 起，16 GB 更稳 |
| `large` | ~5 | 16 GB，并按真实页面压测 |
| `private-net` | 同 standard | 监听 `0.0.0.0`，**必须**有防火墙或 TLS |

不确定就 `standard`。**不要为每个实例单独跑一个 Manager** —— 拆分的依据是资源隔离和
故障域，不是实例数量。

---

## CLI 还是 Web

两条路能力相同、共用同一个 `~/.sleight/sleight.db`，随时来回切。

|  | 命令行 | 网页界面 |
|---|---|---|
| 装什么 | `pip install sleight` | `pip install "sleight[ui]"` |
| 适合 | 脚本、CI、批量、SSH 里直接干 | 第一次上手、看状态、给不熟悉的人用 |
| 引导 | `sleight templates` 里有解释 | 三步引导，每个选项都带「填错会怎样」和推荐值 |

```bash
sleight ui                                   # 默认只听 127.0.0.1:8700
sleight ui --bind 0.0.0.0 --token 一串口令      # 绑非回环时**必须**给 token
```

> 这个界面能执行 `ssh` 和 `docker`。默认只听本机不是保守，是它就该这样。

界面分区：**目标机 · 部署 · 实例 · 插件 · 流水 · 危险区**，右上角切明暗主题。

---

## 目标机与多 Manager

一切记在 `~/.sleight/sleight.db`（SQLite，CLI 和界面共用）。存量 `hosts.toml` 首次
使用会自动导入。

```bash
sleight hosts ls
sleight hosts add hk-01 --ssh deploy@10.0.0.12 --sudo --notes "香港那台"
sleight hosts rm hk-01
```

`local`（本机）是内置目标，删不掉也不占资源。

### 一台机上跑第二个 Manager

```bash
sleight deployments add hk-01 second --dir /srv/cbm-2 --port 9001
sleight deploy --host hk-01/second
```

**目录和端口必须和第一个不同**，工具会拒绝重叠 —— 两个 Manager 共用一个 `data/`
会互相踩 profile 数据库。一台机有多个部署时，`--host` 要写 `主机/部署名`，只写主机名
会让你选。

### 流水

```bash
sleight history                       # 全部
sleight history --host hk-01 -n 10    # 只看这台（--only-host 是同一个）
```

### SSH 与 sudo

- 默认 `BatchMode=yes` —— 缺 key 当场失败，不会挂在密码提示上。要交互加 `--interactive-auth`。
- 首次连接用 `--accept-new` 自动信任指纹。
- `--sudo` 用的是 `sudo -n`（免密）。只在建目录、改权限时用；跑 docker 不需要。

---

## 日常运维

```bash
sleight status  --host hk-01           # 容器、健康状态、/api/status
sleight logs    --host hk-01 --tail 200
sleight backup  --host hk-01           # 停机归档整个 data/
sleight upgrade --host hk-01 --image cloakhq/cloakbrowser-manager:v0.0.11
sleight rollback --host hk-01          # 回到状态文件里记的上一个镜像
```

`upgrade` 默认**先做一次停机备份**再换镜像。

### 实例

```bash
sleight profiles ls     --host hk-01
sleight profiles create news-hk-01 --host hk-01 --preset windows_hk --proxy "$PROXY"
sleight profiles launch news-hk-01 --host hk-01     # 名字是位置参数
sleight profiles stop   news-hk-01 --host hk-01
sleight profiles stop   --host hk-01                # 不给名字 = 全停
```

`--preset` 只有 `windows_us` / `windows_hk` / `macos_us` / `linux_us` 四个 —— 它们的
价值是**保证指纹自洽**（平台、时区、语言、GPU 串得是同一台机器上可能出现的组合）。

冷启动一个实例约 **70 秒**（含 Xvnc 拉起），别以为卡住了。

---

## 浏览器插件

```bash
sleight ext push ./plugins/bypass-paywalls --host hk-01   # 传过去 + MV3 与权限检查
sleight ext apply  --host hk-01                            # 下发到**全部**实例并重启
sleight ext verify --host hk-01                            # 浏览器里真的加载了吗
sleight ext drift  --host hk-01                            # 有实例漏了吗
sleight ext ls     --host hk-01
sleight ext rm     名字 --host hk-01
```

**一定要 `apply` 到全部实例。** `lease()` 是随机租的，漏掉一台就是概率性失败 ——
`drift` 专门查这个。

`verify` 是唯一能确认「插件真的加载了」的手段：加载成功的扩展会以
`chrome-extension://<32 位 id>/…` 出现在 CDP target 列表里。MV3 的 service worker
起来要几秒，太快查会是空的。

---

## 清理：四种「删」

作用范围完全不同，别搞混：

| | 命令 | 丢什么 | 还能回来吗 |
|---|---|---|---|
| ① 删容器 | `sleight destroy` | 运行中的进程 | `deploy` 一下就回来 |
| ② 连 data/ | `destroy --purge-data --yes` | profile、Cookie、**全部登录态** | **不可逆** |
| ③ 连镜像 | `destroy --purge-image` | 本机镜像层 | 要重新 pull |
| ④ 删记录 | `sleight hosts rm` / `deployments rm` | 只是控制机不再记得它 | 目标机毫发无伤 |

彻底清场：

```bash
sleight destroy --host hk-01 --purge-data --purge-image --yes
```

- 删 `data/` 必须 `--yes`（界面上要把部署名原样打一遍）。删镜像不用 —— 它可逆。
- 镜像被同机别的部署占用时**删不掉**，只提示不报错。工具不会 `-f` 强删：那会把还在
  跑的那个部署的镜像层抽走，症状要等它下次重启才出现。
- 只删记录而容器还在跑，会留下没人管的孤儿。界面会拦一下，CLI 会提醒。
- **部署目录本身不会被删。** 就算 `--purge-data --purge-image` 全上，目标机上仍然留着：

  ```
  /path/to/deploy/
    docker-compose.yaml
    .env                    ← AUTH_TOKEN 在里面（权限 600）
    .sleight-deploy.json
    backups/                ← 你自己的备份
  ```

  这是刻意的：备份是你的东西，不该被"清理容器"顺手带走；留着 compose 和 .env
  则意味着 `deploy` 一下就能原样恢复（token 都不变）。**真要连目录一起清，自己来**：

  ```bash
  ssh 目标机 rm -rf /path/to/deploy     # 备份也一起没，确认过再执行
  ```

---

## 内建的护栏

这些事情工具**直接拒绝**，不是写在文档里靠人记：

| 拒绝什么 | 为什么 |
|---|---|
| `latest` 之类不可追溯的标签 | 回滚时不知道回到哪 · 要用加 `--allow-latest` |
| `docker compose down -v` | 会连卷一起删 · 内部永远不带 `-v` |
| 监听非回环地址 | token 在 HTTP 上是明文 · 想好了加 `--expose` |
| 同一台机两个 Manager 共用 `data/` | 会互相踩 profile 数据库 |
| 目录 / 端口 / 容器名和已有部署撞车 | 静默覆盖比报错难查得多 |
| 悄悄轮换在用的 `AUTH_TOKEN` | 换了所有客户端全断 |
| `dir` 写成 `~/…` | 那是**目标机上**的路径，这里不展开 `~`；要写全 |

想看它到底会写哪些字节：

```bash
sleight deploy --dry-run              # 打印 compose 和 .env 全文，不动目标机
sleight deploy --dry-run --brief      # 只看摘要
```

---

## 当库来用

CLI 和界面都只是这层的壳：

```python
from sleight.deploy.spec import DeploySpec
from sleight.deploy.runner import SSHRunner
from sleight.deploy.engine import Deployer

spec = DeploySpec(dir="/srv/cloakbrowser-manager", port=9000)
dep = Deployer(spec, SSHRunner("deploy@10.0.0.12", sudo=True), on_progress=print)

dep.apply()                                   # 幂等
dep.status()
dep.backup()
dep.upgrade("cloakhq/cloakbrowser-manager:v0.0.11")
dep.destroy(purge_data=False, purge_image=True)

with dep.connect() as mgr:                    # 远程会自动开隧道，退出时收掉
    print(mgr.system_status())
```

插件与实例运维在 `sleight.deploy.ops`（`ExtensionOps` / `ProfileOps`），
主机与流水在 `sleight.deploy.store.Store`。

> `sleight.deploy` **不会**被 `import sleight` 拉起来 —— 驱动层始终只有
> `websocket-client` 一个依赖。

---

## 退出码与排错

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 失败（消息在 stderr） |
| `2` | 用法错误 |
| `141` | 下游管道关了（`sleight templates \| head` 这种），不是错误 |

| 症状 | 多半是 | 怎么办 |
|---|---|---|
| `permission denied ... docker.sock` | 用户不在 `docker` 组 | `usermod -aG docker $USER` 后重新登录 |
| `Cannot connect to the Docker daemon` | 守护进程没起 | `systemctl start docker` |
| pull 卡到 `context deadline exceeded` | shell 有代理但 **daemon 没有** | 给 systemd 配 `HTTPS_PROXY`；`preflight` 会点出来 |
| 「本机部署」跑到了别的机器上 | `docker context` 指向远程 | `docker context use default`；`preflight` 会点出来 |
| 目标机上不了 registry | 网络受限 | `docker save 镜像 \| ssh 目标机 docker load`，再 `deploy`（会认出镜像已在本地） |
| `deploy` 一直等 healthy | 实例冷启动慢 | 约 70 秒是正常的；`--no-wait` 可跳过 |
| 隧道秒退 | 远端端口没在监听（容器没起） | 先 `sleight status`；本地端口是自动挑的，不会撞车 |

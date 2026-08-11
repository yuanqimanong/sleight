# CLI 参考

`sleight` 是**运维** CLI——部署和管理 CloakBrowser Manager、profile、插件、浏览器内核。
它**不驱动浏览器做任务**（那是 Python API 和 MCP 的事，见 [[浏览器操作API]]）。

## 全局开关

```bash
sleight --version
sleight --json <命令>     # 机器可解析输出，并静音进度
sleight -q <命令>         # 安静
sleight -v <命令>         # 啰嗦（调试）
```

## 目标机参数（多数命令共用）

```bash
--host NAME[/部署名]      # 本地库里配好的目标；一台机多个 Manager 时用 主机/部署名
--ssh USER@HOST           # 直接给 ssh 目标（不写 = 本机）
--ssh-port N  --identity PATH  --sudo  --accept-new
```
`--host` 与 `--ssh` 互斥：前者从本地库取，后者是临时目标（不进库）。

## 浏览器内核

```bash
sleight browser ls                              # 可装的 + 已装的
sleight browser install [名字] [--version TAG] [--force]
sleight browser path [名字]                     # 打印可执行文件路径
sleight browser rm 名字 [--version TAG]
```
默认名字是 `fingerprint-chromium`。见 [[安装]]、[[浏览器内核选择]]。

## 部署与运维

```bash
sleight preflight --host hk-01          # 只体检，不部署
sleight deploy --host hk-01             # 幂等，可反复跑
sleight deploy --host hk-01 --dry-run   # 打印将写入的确切字节，不动目标机
sleight status --host hk-01             # 容器/健康/端口 + Manager /api/status
sleight logs --host hk-01               # Manager 日志
sleight upgrade --host hk-01            # 换镜像（默认先停机备份）
sleight rollback --host hk-01           # 回到状态文件记着的上一个镜像
sleight backup --host hk-01             # 停机归档整个 data/
sleight destroy --host hk-01            # 停并删容器，默认保留 data/
sleight token --host hk-01              # 从目标机 .env 取回 AUTH_TOKEN
sleight tunnel --host hk-01             # SSH 隧道，本地访问远程 Manager
sleight templates                       # 部署模板 / 实例身份模板 / 地区 / 字段解释
```

部署参数（`deploy`/`upgrade` 等共用）：
`--image REF --port N --bind IP --expose --shm-size SIZE --project NAME --container NAME --allow-latest --template KEY`

**不可逆动作会被拒绝而不是记在文档里**：不许 `latest` tag、不许 `down -v`、
不许悄悄轮换正在用的 `AUTH_TOKEN`、不许在同一个 `/data` 上起第二个 Manager。
`destroy` 这类要 `--yes`，非交互环境下不给就直接拒绝。

## 主机与部署记录

```bash
sleight hosts ls
sleight hosts add hk-01 --ssh deploy@10.0.0.12 --dir /srv/cbm --sudo [--notes "香港那台"]
sleight hosts rm hk-01                                  # 只从本地库删，不动目标机

sleight deployments ls --host hk-01
sleight deployments add hk-01 second --dir /srv/cbm-2 --port 9001   # 同机第二个 Manager
sleight deployments rm hk-01 second

sleight history --host hk-01            # 部署/备份/升级/销毁流水
```
存在 `~/.sleight/sleight.db`，CLI 与 Web 界面共用。

## 插件

```bash
sleight ext ls --host hk-01
sleight ext push ./plugins/my-ext --host hk-01     # MV3 校验 + 权限检查后推上去
sleight ext apply --host hk-01                     # 下发到 profile，然后重启
sleight ext verify --host hk-01                    # 浏览器真的加载了吗
sleight ext drift --host hk-01                     # 磁盘与 profile 配置是否漂移
sleight ext rm 名字 --host hk-01
```
> `apply` 是**整体替换** `--load-extension`：不带 `--names` 时用目标机上装的全部。
> 想只挂某几个就显式给 `--names`，但注意那会把该 profile 上其它扩展摘掉。

## 实例（profile）

```bash
sleight profiles ls --host hk-01
sleight profiles create Win-US-02 --preset windows --region us_east \
    [--proxy URL] [--tags a,b] [--screen 1920x1080] [--geoip] [--headless] \
    [--seed 42] [--notes "文本"] --host hk-01
sleight profiles launch <id> --host hk-01
sleight profiles stop <id> --host hk-01
```

- `--preset`：`windows` / `macos` / `linux`（决定平台与 GPU 串）
- `--region`：`us_east` `us_west` `uk` `de` `hk` `sg` `jp` `cn`（决定时区 + 语言）
- 平台与地区**正交**，组合出自洽的身份。见 [[指纹与人格]]。
- **同名是更新而不是再建**（幂等）。
- CLI **没有**删 profile 的子命令——删只能在 Web 界面做。

## Web 界面

```bash
sleight ui [--bind 127.0.0.1] [--port 8700] [--token TOKEN]
```
需要 `pip install "sleight[ui]"`。详见 [[Web界面教程]]。

## MCP

**不是** `sleight` 的子命令，是**独立的 console script**：

```bash
SLEIGHT_CDP_URL=http://127.0.0.1:9222 sleight-mcp
```
见 [[MCP与Agent网关]]。

---
下一步 → [[远程部署]] · [[Web界面教程]] · [[分布式与实例池]]

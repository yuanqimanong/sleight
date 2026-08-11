# Web 界面教程

这页解决：`sleight ui` 是什么、怎么起、每个页签能干什么、哪些事它做不了。

**定位**：Web 界面是**运维台**——部署和管理 CloakBrowser Manager、profile、插件。
它**不驱动浏览器做任务**（那是 Python API 和 MCP 的事）。

## 起界面

```bash
pip install "sleight[ui]"     # 需要 fastapi + uvicorn
sleight ui                    # 默认 http://127.0.0.1:8700
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--bind IP` | `127.0.0.1` | 监听地址。**默认只听本机是有意的** —— 这个界面能执行 ssh 和 docker |
| `--port N` | `8700` | 端口 |
| `--token TOKEN` | 无 | 访问口令 |

### 鉴权规则（重要）

- 监听**回环地址且不给 `--token`** → 直接放行。任何本机进程都能调用全部接口。
- 绑**非回环地址**（如 `0.0.0.0`）→ **必须**给 `--token`，否则拒绝启动。

**已知风险，明说**：口令会出现在直达链接的 URL 查询串里，也会被写进浏览器 `localStorage`——
因此会进浏览器历史和可能的访问日志。代码里没有缓解手段。**没有** HTTPS/TLS、**没有**
用户/角色/会话、**没有** CSRF token、**没有**限流。

> 结论：**不要把它暴露到公网**。要远程访问，用 `sleight tunnel` 开 SSH 隧道到本地。

## 界面结构

左边是**目标树**（主机 → 该主机上的部署），右边是 5 个页签：

| 页签 | 能做什么 |
|---|---|
| **概览** | 容器状态、健康、端口映射、Manager `/api/status`；取回 `AUTH_TOKEN`；查看操作流水 |
| **部署** | 改部署参数并重新部署；体检（preflight）；部署前预览将写入的文件全文 |
| **实例** | 列出 / 新建 / 删除 profile；launch / stop / stop-all |
| **插件** | 推送插件、下发到 profile、验证浏览器是否真的加载了、检查漂移 |
| **危险区** | 升级、回滚、停机备份、销毁 |

## 三步引导向导（从零到跑起一个 Manager）

第一次用点「新建」会进向导：

**① 连接目标机** —— 填 SSH 目标（或选本机）。点「测试连接」会真的去跑一遍：
`uname -sm` → `docker version` → `docker compose version` → 读 `/proc/meminfo` → `id -un`。
**连接测试通过之前不会保存任何东西**，避免把一个连不上的主机存进库。

**② 选规模** —— 四个模板卡片：`trial` / `standard` / `large` / `private-net`。
选完可以逐项改参数，每个输入框旁边有一行小字说明"填错了会坏什么"和推荐值
（这份说明后端定义一次，CLI 的 `sleight templates` 和界面共用同一份）。

**③ 体检并创建** —— 先跑 preflight（磁盘、内存、端口占用、docker 版本…），
再展示**将要写入的文件全文**，确认后才真正部署。

## 长任务的进度

拉镜像可能几分钟。这类动作走 job + SSE：前端订阅 `/api/jobs/{id}/events`，
实时把输出一行行推到日志面板，结束时收到 `done` 事件。

## REST 端点

界面就是这套 API 的一个前端，你也可以直接调（记得带鉴权头）：

| 分组 | 端点 |
|---|---|
| 配置 | `GET /api/defaults`（前端启动时拉的唯一配置源：版本、默认镜像、spec 缺省、模板、地区、字段说明）|
| 探活 | `POST /api/probe` |
| 主机/部署 | `GET|POST /api/hosts`、`DELETE /api/hosts/{name}`、`GET|POST /api/deployments`、`DELETE /api/deployments/{host}/{name}` |
| 流水 | `GET /api/events?host=&limit=50` |
| 运维 | `POST /api/hosts/{name}/preflight|deploy|upgrade|rollback|destroy|backup`、`GET .../status|logs|token`（都支持 `?deployment=<名>`）|
| 实例 | `GET|POST /api/hosts/{name}/profiles`、`DELETE .../profiles/{pid}?confirm=`、`POST .../profiles/{pid}/{action}` |
| 任务 | `GET /api/jobs`、`GET /api/jobs/{id}`、`GET /api/jobs/{id}/events`（SSE）|

## Web 端做不到、只能用 CLI 的事

这是实话实说的清单：

| 做不到的事 | 用什么替代 |
|---|---|
| 删除插件 | `sleight ext rm` |
| SSH 隧道 | `sleight tunnel` |
| 装浏览器内核 | `sleight browser install`（纯本机命令，界面没有暴露）|
| 新建实例时填**指纹种子**和备注 | `sleight profiles create --seed N --notes TEXT`（后端 API 其实接受，只是表单没做输入框）|
| 部署参数里的 `project` / `allow_latest` / `nofile` / `restart` / 日志轮转 | CLI 有 `--project`、`--allow-latest`；其余两边都没有 |
| 独立的「流水 / 任务」页签 | 概览页点「操作流水」按钮；`GET /api/jobs` 存在但前端没用 |
| 改 uvicorn 的 reload/workers/日志级别 | 没有参数，日志级别固定 `info` |

---
下一步 → [[CLI-参考]] · [[远程部署]]

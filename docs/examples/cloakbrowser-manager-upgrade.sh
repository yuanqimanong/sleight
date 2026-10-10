#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly CLOAK_DIR=/data/apps/cloakbrowser-manager
readonly CLOAK_CONTAINER=cloakbrowser-manager
readonly CLOAK_BASE="$CLOAK_DIR/docker-compose.yaml"
readonly CLOAK_OVERRIDE="$CLOAK_DIR/docker-compose.override.yaml"
cd "$CLOAK_DIR"

die() { printf '%s\n' "$*" >&2; exit 1; }
[[ -f "$CLOAK_BASE" && -f "$CLOAK_OVERRIDE" ]] || die '缺少原 Compose 或升级 override 文件。'
[[ -S /var/run/docker.sock ]] || die '没有找到 /var/run/docker.sock；当前 watchdog 配置需要此 socket。'
docker compose version >/dev/null

CLOAK_PROJECT=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$CLOAK_CONTAINER")
CLOAK_SERVICE=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$CLOAK_CONTAINER")
[[ -n "$CLOAK_PROJECT" && "$CLOAK_PROJECT" != '<no value>' && "$CLOAK_SERVICE" == manager ]] || die '现有容器的 Compose project/service 与此脚本不匹配。'

# 读取当前容器口令，只传给 Compose，不打印或写入命令行参数。
AUTH_TOKEN=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CLOAK_CONTAINER" | sed -n 's/^AUTH_TOKEN=//p')
[[ -n "$AUTH_TOKEN" ]] || die '当前容器未设置 AUTH_TOKEN，停止操作。'
export AUTH_TOKEN
export MANAGER_BIND_IP=0.0.0.0
export MANAGER_PORT=31900

compose_new() {
    docker compose --project-directory "$CLOAK_DIR" -p "$CLOAK_PROJECT" \
        -f "$CLOAK_BASE" -f "$CLOAK_OVERRIDE" "$@"
}

case "${1:-upgrade}" in
    --enable-watchdog)
        CLOAK_HEALTH=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CLOAK_CONTAINER")
        CLOAK_RUNNING_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CLOAK_CONTAINER")
        [[ "$CLOAK_HEALTH" == healthy && "$CLOAK_RUNNING_IMAGE" == *':v0.1.6'* ]] || die '请先完成 v0.1.6 升级并确认 Manager healthy。'
        compose_new up -d --no-deps memory-watchdog
        compose_new logs --tail=30 memory-watchdog
        exit 0
        ;;
    upgrade) ;;
    *) die '用法：bash cloakbrowser-manager-upgrade.sh [--enable-watchdog]' ;;
esac

[[ $(docker inspect --format '{{.State.Running}}' "$CLOAK_CONTAINER") == true ]] || die '旧 Manager 当前未运行，停止操作。'
CLOAK_DATA_SOURCE=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CLOAK_CONTAINER")
[[ "$CLOAK_DATA_SOURCE" == "$CLOAK_DIR/data" && -d "$CLOAK_DIR/data" && ! -L "$CLOAK_DIR/data" ]] || die '实际 /data 挂载不是此目录的普通 data 文件夹，停止操作。'

# 在旧容器仍运行时核对合并结果，避免切换端口、口令或数据目录。
compose_new config --format json | docker exec -i "$CLOAK_CONTAINER" python -c '
import json, os, sys
manager = json.load(sys.stdin)["services"]["manager"]
if manager.get("environment", {}).get("AUTH_TOKEN") != os.environ.get("AUTH_TOKEN"):
    raise SystemExit("合并配置的 AUTH_TOKEN 与当前容器不一致，停止操作。")
if not any(str(p.get("target")) == "8080" and str(p.get("published")) == "31900" and p.get("host_ip") == "0.0.0.0" for p in manager.get("ports", [])):
    raise SystemExit("合并配置没有保留 0.0.0.0:31900 -> 8080，停止操作。")
if not any(v.get("type") == "bind" and v.get("target") == "/data" and v.get("source") == "/data/apps/cloakbrowser-manager/data" for v in manager.get("volumes", [])):
    raise SystemExit("合并配置的 /data 挂载不匹配，停止操作。")
print("端口、AUTH_TOKEN 和 /data 挂载检查通过；口令未显示。")
'

sudo -v
df -h "$CLOAK_DIR"
sudo du -sh "$CLOAK_DIR/data"
CLOAK_STAMP=$(date +%Y%m%d-%H%M%S)
CLOAK_BACKUP="$CLOAK_DIR/backups/$CLOAK_STAMP"
mkdir -p "$CLOAK_DIR/backups"
mkdir "$CLOAK_BACKUP"
printf '%s\n' "$CLOAK_PROJECT" > "$CLOAK_BACKUP/project.txt"
cp -p -- "$CLOAK_BASE" "$CLOAK_BACKUP/docker-compose.source.yaml"
if [[ -f "$CLOAK_DIR/.env" ]]; then cp -p -- "$CLOAK_DIR/.env" "$CLOAK_BACKUP/env.source"; fi

CLOAK_OLD_IMAGE=$(docker inspect --format '{{.Image}}' "$CLOAK_CONTAINER")
CLOAK_OLD_REF=$(docker inspect --format '{{.Config.Image}}' "$CLOAK_CONTAINER")
CLOAK_ROLLBACK_TAG="cloakbrowser-rollback:$CLOAK_STAMP"
printf '%s\n' "$CLOAK_OLD_REF" > "$CLOAK_BACKUP/original-image-ref.txt"
printf '%s\n' "$CLOAK_ROLLBACK_TAG" > "$CLOAK_BACKUP/rollback-image-tag.txt"
docker image tag "$CLOAK_OLD_IMAGE" "$CLOAK_ROLLBACK_TAG"
docker image save -o "$CLOAK_BACKUP/old-image.tar" "$CLOAK_ROLLBACK_TAG"
printf 'services:\n  manager:\n    image: "%s"\n' "$CLOAK_ROLLBACK_TAG" > "$CLOAK_BACKUP/rollback-image.override.yaml"
docker compose --project-directory "$CLOAK_DIR" -p "$CLOAK_PROJECT" \
    -f "$CLOAK_BASE" -f "$CLOAK_BACKUP/rollback-image.override.yaml" \
    config > "$CLOAK_BACKUP/rollback-compose.yaml"
compose_new config > "$CLOAK_BACKUP/new-compose.yaml"

# 先拉取新镜像，下载期间旧 Manager 继续运行。
compose_new pull manager memory-watchdog

CLOAK_STOPPED=0
CLOAK_ROLLOUT_STARTED=0
on_error() {
    local rc=$?
    trap - ERR
    printf '升级未完成，退出码：%s\n' "$rc" >&2
    if [[ "$CLOAK_STOPPED" == 1 && "$CLOAK_ROLLOUT_STARTED" == 0 ]]; then
        docker start "$CLOAK_CONTAINER" >/dev/null || true
        printf '尚未启动新版，已尝试重新启动原容器。\n' >&2
    fi
    if [[ -f "$CLOAK_BACKUP/snapshot.complete" ]]; then
        printf '回退命令：bash %s/cloakbrowser-manager-rollback.sh %s\n' "$CLOAK_DIR" "$CLOAK_BACKUP" >&2
    fi
    exit "$rc"
}
trap on_error ERR

# 如已存在 watchdog，先停它，再停止 Manager，保证备份期间不会重启。
compose_new stop memory-watchdog
docker stop --time 45 "$CLOAK_CONTAINER" >/dev/null
CLOAK_STOPPED=1
sudo tar --acls --xattrs --numeric-owner -cpf "$CLOAK_BACKUP/data.tar" -C "$CLOAK_DIR" data
sudo sha256sum "$CLOAK_BACKUP/data.tar" "$CLOAK_BACKUP/old-image.tar" > "$CLOAK_BACKUP/SHA256SUMS"
touch "$CLOAK_BACKUP/snapshot.complete"
printf '%s\n' "$CLOAK_BACKUP" > "$CLOAK_DIR/.cloak-last-backup"

# 先只升级 Manager，用户完成实际采集验收后再启动 watchdog。
CLOAK_ROLLOUT_STARTED=1
docker compose --project-directory "$CLOAK_DIR" -p "$CLOAK_PROJECT" \
    -f "$CLOAK_BACKUP/new-compose.yaml" up -d --pull never manager

for ((CLOAK_CHECK=0; CLOAK_CHECK<36; CLOAK_CHECK++)); do
    CLOAK_STATE=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CLOAK_CONTAINER")
    if [[ "$CLOAK_STATE" == 'running healthy' ]]; then break; fi
    if [[ "$CLOAK_STATE" == exited* || "$CLOAK_STATE" == dead* ]]; then break; fi
    sleep 5
done
[[ "$CLOAK_STATE" == 'running healthy' ]] || { printf 'Manager 未通过健康检查，请执行回退。\n' >&2; false; }
trap - ERR

printf '\nManager 已升级并通过健康检查。备份目录：%s\n' "$CLOAK_BACKUP"
printf '请先检查旧实例、登录态、插件及旧 sleight 的单个采集任务。\n'
printf '验收后启用监控：bash %s/cloakbrowser-manager-upgrade.sh --enable-watchdog\n' "$CLOAK_DIR"
printf '需要回退时：bash %s/cloakbrowser-manager-rollback.sh %s\n' "$CLOAK_DIR" "$CLOAK_BACKUP"

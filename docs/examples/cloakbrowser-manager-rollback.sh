#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly CLOAK_DIR=/data/apps/cloakbrowser-manager
readonly CLOAK_CONTAINER=cloakbrowser-manager
cd "$CLOAK_DIR"
die() { printf '%s\n' "$*" >&2; exit 1; }

if [[ $# -gt 0 ]]; then
    CLOAK_BACKUP=$(readlink -f -- "$1")
else
    [[ -f "$CLOAK_DIR/.cloak-last-backup" ]] || die '没有找到备份记录，请指定备份目录。'
    CLOAK_BACKUP=$(readlink -f -- "$(cat "$CLOAK_DIR/.cloak-last-backup")")
fi
[[ "$CLOAK_BACKUP" == "$CLOAK_DIR"/backups/* && -d "$CLOAK_BACKUP" ]] || die '备份目录必须位于本项目的 backups 目录内。'
for CLOAK_FILE in snapshot.complete project.txt data.tar old-image.tar SHA256SUMS rollback-compose.yaml rollback-image-tag.txt original-image-ref.txt docker-compose.source.yaml; do
    [[ -f "$CLOAK_BACKUP/$CLOAK_FILE" ]] || die "备份不完整，缺少：$CLOAK_FILE"
done
CLOAK_PROJECT=$(cat "$CLOAK_BACKUP/project.txt")
[[ -n "$CLOAK_PROJECT" ]] || die '备份中缺少 Compose project 名称。'
[[ -d "$CLOAK_DIR/data" && ! -L "$CLOAK_DIR/data" ]] || die '当前 data 目录不存在或为符号链接，停止操作。'

if docker inspect "$CLOAK_CONTAINER" >/dev/null 2>&1; then
    CLOAK_CURRENT_PROJECT=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$CLOAK_CONTAINER")
    CLOAK_CURRENT_DATA=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CLOAK_CONTAINER")
    [[ "$CLOAK_CURRENT_PROJECT" == "$CLOAK_PROJECT" && "$CLOAK_CURRENT_DATA" == "$CLOAK_DIR/data" ]] || die '当前 Manager 的 project 或数据挂载与备份不匹配。'
fi

# 停机前确认备份可读取，旧镜像可加载；不依赖镜像仓库在线。
sudo -v
sudo sha256sum -c "$CLOAK_BACKUP/SHA256SUMS"
docker image load -i "$CLOAK_BACKUP/old-image.tar"
CLOAK_ROLLBACK_TAG=$(cat "$CLOAK_BACKUP/rollback-image-tag.txt")
CLOAK_ORIGINAL_REF=$(cat "$CLOAK_BACKUP/original-image-ref.txt")
if [[ "$CLOAK_ORIGINAL_REF" != *@* && "$CLOAK_ORIGINAL_REF" != sha256:* ]]; then
    docker image tag "$CLOAK_ROLLBACK_TAG" "$CLOAK_ORIGINAL_REF"
fi

# 仅停止同一 Compose project 的 watchdog 和指定 Manager。
CLOAK_WATCHER_LIST=$(docker ps -aq \
    --filter "label=com.docker.compose.project=$CLOAK_PROJECT" \
    --filter 'label=com.docker.compose.service=memory-watchdog')
mapfile -t CLOAK_WATCHERS <<< "$CLOAK_WATCHER_LIST"
for CLOAK_WATCHER in "${CLOAK_WATCHERS[@]}"; do
    [[ -n "$CLOAK_WATCHER" ]] || continue
    docker stop --time 10 "$CLOAK_WATCHER" >/dev/null
    docker rm "$CLOAK_WATCHER" >/dev/null
done
CLOAK_TARGET_ID=$(docker ps -aq --filter "name=^/$CLOAK_CONTAINER$")
if [[ -n "$CLOAK_TARGET_ID" ]]; then
    docker stop --time 45 "$CLOAK_CONTAINER" >/dev/null
fi

# 保留升级后的数据，不与旧数据库混合，也不执行递归删除。
CLOAK_STAMP=$(date +%Y%m%d-%H%M%S)
CLOAK_FAILED_DATA="$CLOAK_DIR/data.after-upgrade-$CLOAK_STAMP"
[[ ! -e "$CLOAK_FAILED_DATA" ]] || die '保留数据的目标目录已存在。'
sudo mv -- "$CLOAK_DIR/data" "$CLOAK_FAILED_DATA"
if ! sudo tar --acls --xattrs --numeric-owner -xpf "$CLOAK_BACKUP/data.tar" -C "$CLOAK_DIR"; then
    if [[ -e "$CLOAK_DIR/data" ]]; then
        sudo mv -- "$CLOAK_DIR/data" "$CLOAK_DIR/data.restore-incomplete-$CLOAK_STAMP"
    fi
    sudo mv -- "$CLOAK_FAILED_DATA" "$CLOAK_DIR/data"
    die '恢复数据失败，已放回升级后的数据；容器保持停止，请检查磁盘空间与权限。'
fi

if [[ -f "$CLOAK_DIR/docker-compose.override.yaml" ]]; then
    mv -- "$CLOAK_DIR/docker-compose.override.yaml" "$CLOAK_DIR/docker-compose.override.disabled-$CLOAK_STAMP.yaml"
fi
cp -p -- "$CLOAK_BACKUP/docker-compose.source.yaml" "$CLOAK_DIR/docker-compose.yaml"
if [[ -f "$CLOAK_BACKUP/env.source" ]]; then
    if [[ -f "$CLOAK_DIR/.env" ]]; then cp -p -- "$CLOAK_DIR/.env" "$CLOAK_DIR/.env.after-upgrade-$CLOAK_STAMP"; fi
    cp -p -- "$CLOAK_BACKUP/env.source" "$CLOAK_DIR/.env"
elif [[ -f "$CLOAK_DIR/.env" ]]; then
    mv -- "$CLOAK_DIR/.env" "$CLOAK_DIR/.env.after-upgrade-$CLOAK_STAMP"
fi

docker compose --project-directory "$CLOAK_DIR" -p "$CLOAK_PROJECT" \
    -f "$CLOAK_BACKUP/rollback-compose.yaml" up -d --pull never --no-build manager

for ((CLOAK_CHECK=0; CLOAK_CHECK<36; CLOAK_CHECK++)); do
    CLOAK_STATE=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CLOAK_CONTAINER")
    if [[ "$CLOAK_STATE" == 'running healthy' ]]; then break; fi
    if [[ "$CLOAK_STATE" == exited* || "$CLOAK_STATE" == dead* ]]; then break; fi
    sleep 5
done
[[ "$CLOAK_STATE" == 'running healthy' ]] || die '旧镜像与旧数据已经恢复，但 Manager 未通过健康检查，请查看 Manager 日志。'

printf '\n已启动升级前的镜像和数据。升级后的数据保留在：%s\n' "$CLOAK_FAILED_DATA"
printf '请检查 healthy、原实例登录态及旧 sleight 的单个采集任务，再恢复调度。\n'
docker inspect --format 'image={{.Config.Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CLOAK_CONTAINER"

#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd)"
KERNEL_DIR="$DIR/kernel/linux"
KERNEL_LINUX_VOLUME="vamos-kernel-linux"
CCACHE_VOLUME="vamos-kernel-ccache"
CLANGD_CACHE_VOLUME="vamos-kernel-clangd-cache"

if [ ! -f "$KERNEL_DIR/Makefile" ]; then
  "$DIR/vamos" setup
fi

KERNEL_REV="$(git -C "$KERNEL_DIR" rev-parse HEAD)"

"$DIR/tools/build/prepare_builder_image.sh"

docker volume create "$KERNEL_LINUX_VOLUME" >/dev/null
docker run --rm \
  --entrypoint sh \
  -v "$KERNEL_LINUX_VOLUME:/linux" \
  vamos-builder \
  -lc "mkdir -p /linux && chown $(id -u):$(id -g) /linux && chmod 0775 /linux"

docker volume create "$CCACHE_VOLUME" >/dev/null
docker run --rm \
  --entrypoint sh \
  -v "$CCACHE_VOLUME:/ccache" \
  vamos-builder \
  -lc "mkdir -p /ccache && chown $(id -u):$(id -g) /ccache && chmod 0775 /ccache"

docker volume create "$CLANGD_CACHE_VOLUME" >/dev/null
docker run --rm \
  --entrypoint sh \
  -v "$CLANGD_CACHE_VOLUME:/clangd-cache" \
  vamos-builder \
  -lc "mkdir -p /clangd-cache/index && chown -R $(id -u):$(id -g) /clangd-cache && chmod 0775 /clangd-cache /clangd-cache/index"

if docker run --rm --entrypoint sh -v "$KERNEL_LINUX_VOLUME:/linux" vamos-builder \
  -lc "test \"\$(git -c safe.directory=/linux -C /linux rev-parse HEAD 2>/dev/null)\" = '$KERNEL_REV'" \
  >/dev/null; then
  exit 0
fi

echo "Syncing kernel/linux into Docker volume"
sync_container_id=$(docker run -d \
  --entrypoint tail \
  -v "$DIR:/repo:ro" \
  -v "$KERNEL_LINUX_VOLUME:/linux" \
  vamos-builder -f /dev/null)

cleanup() {
  docker container rm -f "$sync_container_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker exec "$sync_container_id" sh -lc "rm -rf /linux/* /linux/.[!.]* /linux/..?*"
docker exec -u "$(id -u):$(id -g)" "$sync_container_id" sh -lc \
  "cd /linux && git clone --no-local /repo/kernel/linux . >/dev/null 2>&1 && git checkout --force '$KERNEL_REV' >/dev/null 2>&1"

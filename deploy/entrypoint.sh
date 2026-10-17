#!/usr/bin/env bash
# 容器入口：确保数据库在位，然后起 Web 服务。
# 端口/口令/库路径都从环境变量来（PORT / HOST / AUTH / QX_DB），
# 这样同一份镜像能直接丢到任何按 $PORT 注入的平台上。
set -euo pipefail

DB="${QX_DB:-/data/queries.db}"

if [ ! -f "$DB" ] && [ -n "${DB_URL:-}" ]; then
  echo "[deploy] 本地没有数据库，从 DB_URL 下载"
  mkdir -p "$(dirname "$DB")"
  case "${DB_URL}" in
    *.gz)
      curl -fL --retry 5 --retry-delay 3 -o "$DB.gz" "$DB_URL"
      gunzip -c "$DB.gz" > "$DB"
      rm -f "$DB.gz"
      ;;
    *)
      curl -fL --retry 5 --retry-delay 3 -o "$DB.part" "$DB_URL"
      mv "$DB.part" "$DB"
      ;;
  esac
fi

if [ ! -f "$DB" ]; then
  echo "[deploy] 找不到数据库 $DB" >&2
  echo "[deploy] 要么把 data/queries.db 挂到 /data（见 docker-compose.yml），" >&2
  echo "[deploy] 要么设置 DB_URL 指向一个能直接下载的 .db / .db.gz" >&2
  exit 1
fi

echo "[deploy] 数据库 ${DB} $(du -h "$DB" | cut -f1)"
echo "[deploy] 监听 ${HOST:-0.0.0.0}:${PORT:-8765}${AUTH:+（已开启访问口令）}"

exec python -m src.server \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8765}" \
  --db "$DB"

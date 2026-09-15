#!/usr/bin/env bash
# 开发形态启动：后端热重载（:8720）+ Vite dev server（:5173，代理 API 与 WebSocket）。
#
#   ./scripts/dev.sh
#
# 改后端代码会自动重启；改前端代码由 Vite 热更新，无需手动刷新页面。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_common.sh
. "$ROOT/scripts/_common.sh"

BACKEND_PORT="${SPARK_PORT:-8720}"
FRONTEND_PORT="${VITE_PORT:-5173}"

echo "==> 环境检查"
require_node
check_ffmpeg

if port_in_use "$BACKEND_PORT"; then
  echo "✗ 后端端口 $BACKEND_PORT 已被占用，请先停止占用进程（或设 SPARK_PORT 换端口）"
  exit 1
fi

ensure_backend_venv "$ROOT"
ensure_frontend_deps "$ROOT"

pids=()
cleanup() {
  echo
  echo "==> 停止服务"
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> 启动后端 http://127.0.0.1:$BACKEND_PORT"
(cd "$ROOT/backend" && exec .venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port "$BACKEND_PORT") &
pids+=($!)

# 等后端就绪，避免前端首个请求打空
for _ in $(seq 1 30); do
  if curl -fsS -m 1 "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
    echo "    后端就绪"
    break
  fi
  sleep 0.5
done

echo "==> 启动前端 http://localhost:$FRONTEND_PORT"
(cd "$ROOT/frontend" && exec npm run dev) &
pids+=($!)

echo
echo "    打开 http://localhost:$FRONTEND_PORT 开始使用（Ctrl+C 停止全部）"
wait

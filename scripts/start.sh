#!/usr/bin/env bash
# 生产形态启动：构建前端 → 后端单端口托管（含 API 与 WebSocket）。
#
#   ./scripts/start.sh              # 默认 http://127.0.0.1:8720
#   SPARK_PORT=9000 ./scripts/start.sh
#   ./scripts/start.sh --reload     # 额外参数透传给 uvicorn
#
# 首次运行会自动创建虚拟环境并安装依赖，随后每次启动只做前端增量构建。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_common.sh
. "$ROOT/scripts/_common.sh"

PORT="${SPARK_PORT:-8720}"

echo "==> 环境检查"
require_node
check_ffmpeg

if port_in_use "$PORT"; then
  echo "✗ 端口 $PORT 已被占用。请先停止占用进程，或换端口："
  echo "    SPARK_PORT=9000 ./scripts/start.sh"
  echo "  当前占用："
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sed 's/^/    /' || true
  exit 1
fi

ensure_backend_venv "$ROOT"
ensure_frontend_deps "$ROOT"

echo "==> 构建前端"
(cd "$ROOT/frontend" && npm run build)

echo "==> 启动服务：http://127.0.0.1:$PORT"
echo "    接口文档：http://127.0.0.1:$PORT/docs"
echo "    停止服务：Ctrl+C"
cd "$ROOT/backend"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" "$@"

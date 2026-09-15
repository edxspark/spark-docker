#!/usr/bin/env bash
# 开发模式：同时启动后端（自动重载，:8720）与前端 Vite dev server（:5173）。
# 访问 http://localhost:5173，API 与 WebSocket 由 Vite 代理到后端。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.nvm/versions/node/v24.17.0/bin:/opt/homebrew/bin:$PATH"

if [ ! -x "$ROOT/backend/.venv/bin/python" ]; then
  echo "==> 创建后端虚拟环境"
  (cd "$ROOT/backend" && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]")
fi

if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "==> 安装前端依赖"
  (cd "$ROOT/frontend" && npm install)
fi

cleanup() {
  echo
  echo "==> 停止服务"
  kill 0 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> 启动后端 http://127.0.0.1:8720"
(cd "$ROOT/backend" && .venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8720) &

sleep 2

echo "==> 启动前端 http://localhost:5173"
(cd "$ROOT/frontend" && npm run dev) &

wait

#!/usr/bin/env bash
# 一键启动（生产形态）：构建前端 → 用后端单端口托管。
# 访问 http://127.0.0.1:8720
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.nvm/versions/node/v24.17.0/bin:/opt/homebrew/bin:$PATH"

PORT="${SPARK_PORT:-8720}"

echo "==> 检查后端虚拟环境"
if [ ! -x "$ROOT/backend/.venv/bin/python" ]; then
  echo "    未找到 backend/.venv，正在创建（需要 uv：brew install uv）"
  (cd "$ROOT/backend" && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .)
fi

echo "==> 检查前端依赖"
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  (cd "$ROOT/frontend" && npm install)
fi

echo "==> 构建前端"
(cd "$ROOT/frontend" && npm run build)

echo "==> 启动服务 http://127.0.0.1:$PORT"
cd "$ROOT/backend"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" "$@"

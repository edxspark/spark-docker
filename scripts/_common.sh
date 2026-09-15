#!/usr/bin/env bash
# 共用工具：解析 node/npm/ffmpeg 路径。被 start.sh 与 dev.sh source。
# 注意：GUI 或非登录 shell 启动的进程往往拿不到 nvm / Homebrew 的 PATH，
# 因此这里主动探测常见位置，避免「命令找不到」。

set -euo pipefail

# 把 Homebrew 目录补进 PATH（ffmpeg、uv 常在这里）
for dir in /opt/homebrew/bin /usr/local/bin; do
  case ":$PATH:" in
    *":$dir:"*) ;;
    *) [ -d "$dir" ] && PATH="$dir:$PATH" ;;
  esac
done
export PATH

resolve_node() {
  command -v node >/dev/null 2>&1 && return 0

  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"

  # 直接查 nvm 的版本目录，取版本号最大的一个。
  # 刻意不 source nvm.sh：该初始化脚本在 set -euo pipefail 下会中断调用方
  # （读写未定义变量并可能自行 exit），症状是脚本静默退出、没有任何报错。
  local candidate
  candidate="$(ls -d "$nvm_dir"/versions/node/*/bin 2>/dev/null | sort -V | tail -1 || true)"
  if [ -n "$candidate" ] && [ -x "$candidate/node" ]; then
    PATH="$candidate:$PATH"
    export PATH
  fi

  command -v node >/dev/null 2>&1
}

require_node() {
  if ! resolve_node; then
    echo "✗ 未找到 node。请安装：brew install node"
    echo "  （已尝试 PATH、\$NVM_DIR 与 ~/.nvm/versions/node/*/bin）"
    exit 1
  fi
  echo "    node $(node --version)  npm $(npm --version)"
}

require_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "✗ 未找到 uv。请安装：brew install uv"
    exit 1
  fi
}

ensure_backend_venv() {
  local root="$1"
  if [ -x "$root/backend/.venv/bin/python" ]; then
    return 0
  fi
  require_uv
  echo "==> 创建后端虚拟环境（Python 3.12）"
  (cd "$root/backend" && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]")
}

ensure_frontend_deps() {
  local root="$1"
  if [ -d "$root/frontend/node_modules" ]; then
    return 0
  fi
  echo "==> 安装前端依赖"
  (cd "$root/frontend" && npm install)
}

check_ffmpeg() {
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    echo "    ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
  else
    echo "! 未检测到 ffmpeg —— 视频合成会失败。请执行：brew install ffmpeg"
  fi
}

port_in_use() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

#!/usr/bin/env bash
# 登录 YouTube 并把登录态导出成 yt-dlp 能用的 cookies.txt。
#
# 为什么需要：YouTube 对匿名请求做「Sign in to confirm you're not a bot」风控，
# 下载会直接失败。实测本机可用的办法只有 cookies（见 scripts/youtube_login.py 顶部说明）。
#
# 登录成功后会立刻用一次真实解析验证 cookies 是否真的有效，
# 免得下次跑任务才发现还是失败。

set -euo pipefail
cd "$(dirname "$0")/.."
exec backend/.venv/bin/python scripts/youtube_login.py "$@"

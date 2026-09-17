#!/usr/bin/env python
"""登录 YouTube 并导出 yt-dlp 能用的 cookies.txt（命令行版）。

用法：
    ./scripts/youtube_login.sh              # 打开浏览器登录并验证
    ./scripts/youtube_login.sh --check      # 只验证已有的 cookies 是否还有效

为什么需要这个：YouTube 现在对匿名请求做「Sign in to confirm you're not a bot」
风控，下载直接失败。报错原文建议用 --cookies-from-browser 或 --cookies，
但实测本机这两条路都不通：

  - --cookies-from-browser chrome/edge/brave → 没有 cookies 数据库（用户浏览器是 Arc）
  - --cookies-from-browser safari            → macOS 沙箱拒绝读取，需要完全磁盘访问权限
  - 换 player_client（tv/web_safari/mweb/ios）→ 风控过了，但 YouTube 只回分镜图
    （sb0~sb3），一个音视频格式都没有，等于没用

所以这里开一个浏览器窗口让用户登录一次，把登录态转成 Netscape cookies.txt
交给 yt-dlp。导出后立刻真实解析一次做验证，而不是只看「文件里有 cookie」。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import settings
from app.services import youtube_auth

STATE_FILE = settings.auth_dir / "youtube.json"
COOKIES_FILE = settings.auth_dir / "youtube_cookies.txt"
PROBE_URL = "https://www.youtube.com/watch?v=EN7frwQIbKc"


async def do_check() -> int:
    if not COOKIES_FILE.exists():
        print(f"✘ 还没有导出过 cookies（期望文件：{COOKIES_FILE}）")
        print("  先运行：./scripts/youtube_login.sh")
        return 1
    print(f"验证 {COOKIES_FILE} …（会真实解析一次视频，约 10~60 秒）")
    ok, detail = await youtube_auth.auth_manager.check(COOKIES_FILE, probe_url=PROBE_URL)
    print(("✔ " if ok else "✘ ") + detail)
    return 0 if ok else 1


async def do_login(timeout: int) -> int:
    print("正在打开浏览器窗口，请在窗口里登录 Google 账号（登录后会自动继续）…")
    session = await youtube_auth.auth_manager.start_login(
        STATE_FILE, COOKIES_FILE, timeout=timeout, probe_url=PROBE_URL
    )
    last = ""
    while session.running():
        snapshot = session.snapshot()
        if snapshot["message"] != last:
            last = snapshot["message"]
            print(f"  · {last}")
        await asyncio.sleep(2)

    snapshot = session.snapshot()
    print()
    print(f"状态：{snapshot['status']}")
    print(f"说明：{snapshot['message']}")
    if snapshot.get("cookie_count"):
        print(f"cookies 文件：{snapshot['cookies_path']}（{snapshot['cookie_count']} 条）")
    if snapshot["status"] == "success":
        print("\n✔ 完成。下载任务会自动使用这份 cookies，无需再填路径。")
        return 0
    print("\n✘ 未成功。可重试：./scripts/youtube_login.sh")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="登录 YouTube 并导出 yt-dlp 用的 cookies")
    parser.add_argument("--check", action="store_true", help="只验证已有 cookies，不重新登录")
    parser.add_argument("--timeout", type=int, default=300, help="等待登录的秒数（默认 300）")
    args = parser.parse_args()
    return asyncio.run(do_check() if args.check else do_login(args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())

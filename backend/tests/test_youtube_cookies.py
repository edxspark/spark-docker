"""YouTube cookies 的解析、导出与下载器接线。

事故：下载报「Sign in to confirm you're not a bot」，提示去配置 cookies 文件，
但用户并没有一个能用的 cookies 文件。实测本机可用的办法只有 cookies：

  - --cookies-from-browser chrome/edge/brave → 没有 cookies 数据库
  - --cookies-from-browser safari            → macOS 沙箱拒绝读取
  - 换 player_client（tv/web_safari/mweb/ios）→ 风控过了，但只回分镜图（sb0~sb3），
    拿不到任何音视频格式

所以由应用自己开浏览器登录一次并导出 cookies.txt。这里钉住导出格式与接线。
"""

from __future__ import annotations

import http.cookiejar as cookiejar
import json

import pytest

from app.providers.base import ProviderError
from app.providers.downloader.ytdlp import resolve_cookies_file
from app.services.settings_store import DownloadConfig
from app.services.youtube_auth import login_cookie_count, to_netscape

_COOKIES = [
    {
        "domain": ".youtube.com", "name": "SAPISID", "value": "abc/def",
        "path": "/", "secure": True, "httpOnly": True, "expires": 1900000000,
    },
    {
        "domain": ".google.com", "name": "SID", "value": "xyz",
        "path": "/", "secure": True, "httpOnly": False, "expires": 1900000000,
    },
    {
        "domain": "www.youtube.com", "name": "PREF", "value": "f6=400",
        "path": "/", "secure": False, "httpOnly": False, "expires": -1,
    },
]


def test_httponly_cookies_survive_the_round_trip(tmp_path):
    """HttpOnly 的 cookie 必须带 #HttpOnly_ 前缀，否则会被 MozillaCookieJar 丢掉。

    而 Google 的关键登录 cookie（SAPISID / __Secure-1PSID）恰恰都是 HttpOnly：
    丢了它们，cookies.txt 看起来正常，实际照样撞风控。
    """
    path = tmp_path / "cookies.txt"
    path.write_text(to_netscape(_COOKIES), encoding="utf-8")

    jar = cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    names = {c.name for c in jar}

    assert "SAPISID" in names, "HttpOnly 的关键登录 cookie 被丢掉了"
    assert "SID" in names
    assert "PREF" in names


def test_session_cookie_expiry_is_zero_not_negative(tmp_path):
    """会话 cookie 的 expires 必须是 0；写 -1 会被部分解析器判为已过期。"""
    text = to_netscape(_COOKIES)
    pref_line = next(line for line in text.splitlines() if line.endswith("PREF\tf6=400"))
    fields = pref_line.split("\t")
    assert fields[4] == "0", f"会话 cookie 的 expires 应为 0，实际 {fields[4]!r}"


def test_netscape_header_is_present():
    """缺了文件头，yt-dlp 会当成无效 cookies 文件。"""
    assert to_netscape([]).startswith("# Netscape HTTP Cookie File")


def test_domain_flag_reflects_include_subdomains():
    # 注意：#HttpOnly_ 开头的行是数据不是注释，过滤时必须留下——
    # 按 "#" 一刀切会把最关键的登录 cookie 滤掉（我第一版就踩了这个坑）
    lines = [
        line for line in to_netscape(_COOKIES).splitlines()
        if not line.startswith("# ") and line.strip()
    ]
    by_name = {line.split("\t")[5]: line.split("\t") for line in lines}
    assert by_name["SAPISID"][1] == "TRUE", "以 . 开头的域名应标记为包含子域"
    assert by_name["PREF"][1] == "FALSE", "主机名精确匹配不应标记包含子域"


def test_login_detection_uses_google_session_cookies():
    assert login_cookie_count(_COOKIES) == 1          # SAPISID
    assert login_cookie_count([{"name": "PREF"}]) == 0


# ------------------------------------------------------------------ 下载器接线


def test_missing_configured_cookies_file_fails_loudly(tmp_path):
    """配置了不存在的路径时必须直接报错。

    静默忽略会让用户看到一个毫无头绪的「需要登录」，根因却是路径写错了。
    """
    config = DownloadConfig(cookies_file=str(tmp_path / "nope.txt"))
    with pytest.raises(ProviderError) as err:
        resolve_cookies_file(config)
    assert "不存在" in str(err.value)


def test_empty_configured_cookies_file_fails_loudly(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ProviderError) as err:
        resolve_cookies_file(DownloadConfig(cookies_file=str(path)))
    assert "空" in str(err.value)


def test_configured_cookies_file_wins(tmp_path):
    path = tmp_path / "mine.txt"
    path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    assert resolve_cookies_file(DownloadConfig(cookies_file=str(path))) == path


def test_exports_are_reusable_via_storage_state(tmp_path):
    """导出流程要能从 Playwright storage_state 里取到 cookie。"""
    import asyncio

    from app.services.youtube_auth import export_cookies

    state = tmp_path / "youtube.json"
    state.write_text(json.dumps({"cookies": _COOKIES, "origins": []}), encoding="utf-8")
    out = tmp_path / "cookies.txt"

    count = asyncio.run(export_cookies(state, out))
    assert count == 3
    assert "SAPISID" in out.read_text(encoding="utf-8")

"""测试夹具。

关键：必须在导入 app 之前设置数据目录与数据库 URL，
因为 app.db 在模块导入时就会创建引擎。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="spark-test-"))
os.environ["SPARK_DATA_DIR"] = str(_TMP_ROOT)
os.environ["SPARK_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_ROOT / 'test.db'}"


@pytest.fixture(scope="session")
def tmp_root() -> Path:
    return _TMP_ROOT


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_root():
    yield
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def sample_video(tmp_root: Path) -> Path:
    """生成一段带音轨的测试视频（12 秒，640x360）。"""
    import subprocess

    from app.utils.ffmpeg import ffmpeg_available, resolve_binary

    if not ffmpeg_available():
        pytest.skip("未安装 ffmpeg，跳过依赖媒体处理的测试")

    path = tmp_root / "fixtures" / "sample.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path

    subprocess.run(
        [
            resolve_binary("ffmpeg") or "ffmpeg",
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def sample_srt(tmp_root: Path) -> Path:
    """一段覆盖 12 秒的英文测试字幕。"""
    path = tmp_root / "fixtures" / "sample.en.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """1
00:00:00,200 --> 00:00:02,000
Hello everyone, welcome back to the channel.

2
00:00:02,100 --> 00:00:04,500
Today we are going to learn something important.

3
00:00:04,600 --> 00:00:07,000
This is the first example of the video.

4
00:00:07,100 --> 00:00:09,400
And here is the second example.

5
00:00:09,500 --> 00:00:11,800
Thank you for watching, please subscribe.
""",
        encoding="utf-8",
    )
    return path

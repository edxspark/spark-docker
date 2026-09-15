#!/usr/bin/env python
"""离线端到端演示 / 自检脚本。

不访问 YouTube、不需要任何 API 密钥，用 ffmpeg 生成一段合成视频与英文假字幕，
然后跑完整条流水线：断句 → 翻译 → 配音 → 时间轴对齐 → 混音 → 烧录字幕 → 渲染 → 发布。

用途：
- 验证本机 ffmpeg / 依赖是否装好
- 在没有密钥、没有网络的情况下确认流水线可用
- 查看成片效果与各阶段产物

用法：
    cd backend && .venv/bin/python ../scripts/verify_pipeline.py
    # 指定输出目录
    cd backend && .venv/bin/python ../scripts/verify_pipeline.py --out /tmp/spark-demo
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

OUT_ROOT = Path(tempfile.mkdtemp(prefix="spark-demo-"))
os.environ.setdefault("SPARK_DATA_DIR", str(OUT_ROOT / "data"))
os.environ.setdefault("SPARK_DATABASE_URL", f"sqlite+aiosqlite:///{OUT_ROOT / 'data' / 'demo.db'}")

from app.core.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Task, TaskItem, TaskLog, TaskStatus  # noqa: E402
from app.pipeline.runner import task_runner  # noqa: E402
from app.providers.base import DownloadResult, ProbeResult, VideoInfo  # noqa: E402
from app.services.settings_store import settings_store  # noqa: E402
from app.utils import ffmpeg as ffmpeg_utils  # noqa: E402

SCRIPT = [
    (0.2, 2.2, "Hello everyone, welcome back to the channel."),
    (2.4, 5.0, "Today we are going to learn something really useful."),
    (5.2, 7.6, "This is the first example of the video."),
    (7.8, 10.2, "And here is the second example, please pay attention."),
    (10.4, 12.6, "Thank you for watching, see you in the next video."),
]


def srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def ensure_tools() -> None:
    if not ffmpeg_utils.ffmpeg_available():
        print("✗ 未检测到 ffmpeg。macOS 请执行：brew install ffmpeg")
        raise SystemExit(1)


def make_fixture_video(dest: Path, duration: float) -> Path:
    """生成一段带音轨与画面变化的测试视频。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    print(f"→ 生成测试视频（{duration}s，640x360，带音轨）…")
    subprocess.run(
        [
            ffmpeg_utils._bin(settings.ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=25:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=330:duration={duration}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(dest),
        ],
        check=True,
    )
    return dest


def make_fixture_srt(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    blocks = [
        f"{i}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}"
        for i, (start, end, text) in enumerate(SCRIPT, start=1)
    ]
    dest.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return dest


class DemoDownloader:
    """把本地夹具当作「已从 YouTube 下载」的结果。"""

    name = "demo"

    def __init__(self, video: Path, subtitle: Path) -> None:
        self.video = video
        self.subtitle = subtitle

    async def probe(self, url: str) -> ProbeResult:
        return ProbeResult(
            source_type="video",
            title="Demo Video: How To Learn Anything Faster",
            author="Demo Channel",
            source_id="demo000001",
            entries=[
                VideoInfo(
                    video_id="demo000001",
                    url=url,
                    title="Demo Video: How To Learn Anything Faster",
                    description="An offline fixture standing in for a real YouTube video.",
                    author="Demo Channel",
                    duration=12.8,
                )
            ],
        )

    async def download(self, item: VideoInfo, output_dir: Path, *, progress=None, cancel_check=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"{item.video_id}.mp4"
        shutil.copy2(self.video, target)
        if progress:
            progress(45.0, "下载中 45%")
            progress(100.0, "下载完成，正在合并音视频…")
        return DownloadResult(
            video_path=target,
            subtitle_path=self.subtitle,
            thumbnail_path=None,
            info=item,
            subtitle_kind="manual",
        )

    async def fetch_thumbnail(self, item: VideoInfo, out_path: Path):
        return None


async def run(out_dir: Path, aspect: str, keep_mock_media: bool) -> int:
    ensure_tools()
    settings.ensure_dirs()

    fixtures = out_dir / "fixtures"
    video = make_fixture_video(fixtures / "demo.mp4", 12.8)
    subtitle = make_fixture_srt(fixtures / "demo.en.srt")

    await init_db()
    async with SessionLocal() as session:
        await settings_store.update(
            session,
            {
                "translator": {"provider": "mock"},
                "tts": {"provider": "mock", "voice": "xiaoxian", "sample_rate": 24000, "concurrency": 3},
                "publish": {"provider": "mock", "auto_publish": True, "default_tags": ["演示", "搬运"]},
                "video": {
                    "target_aspect": aspect,
                    "burn_subtitles": True,
                    "keep_bgm": True,
                    "bgm_volume": 0.12,
                    "max_speedup": 1.35,
                    "preset": "veryfast",
                    "crf": 26,
                },
            },
        )

    import app.pipeline.runner as runner_module

    real_build_all = runner_module.build_all
    demo = DemoDownloader(video, subtitle)

    def fake_build_all(config, account_file):
        providers = real_build_all(config, account_file)
        providers["downloader"] = demo
        return providers

    runner_module.build_all = fake_build_all

    async with SessionLocal() as session:
        task = Task(
            title="离线演示任务",
            source_url="https://www.youtube.com/watch?v=demo000001",
            source_type="video",
            source_id="demo000001",
            author="Demo Channel",
            status=TaskStatus.PENDING.value,
            total_items=1,
        )
        session.add(task)
        await session.flush()
        session.add(
            TaskItem(
                task_id=task.id, idx=0, video_id="demo000001",
                url=task.source_url, title="Demo Video: How To Learn Anything Faster",
                author="Demo Channel", duration=12.8, status=TaskStatus.PENDING.value,
            )
        )
        await session.commit()
        task_id = task.id

    print("→ 启动流水线…")
    handle = await task_runner.submit(task_id)
    assert handle.task is not None
    await handle.task

    from sqlalchemy import select

    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        items = (await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))).scalars().all()
        logs = (await session.execute(select(TaskLog).where(TaskLog.task_id == task_id))).scalars().all()

    assert task is not None
    item = items[0]

    print()
    print("=" * 68)
    print(f"任务状态：{task.status}｜{task.message}")
    print(f"条目状态：{item.status}｜{item.message}")
    if item.error:
        print(f"错误信息：{item.error}")
    print("-" * 68)
    for log in logs:
        mark = {"error": "✗", "warning": "!"}.get(log.level, "·")
        print(f"  {mark} [{log.stage or '-'}] {log.message}")
    print("-" * 68)

    stats = item.stats or {}
    translate = stats.get("translate") or {}
    tts = stats.get("tts") or {}
    output = stats.get("output") or {}
    print(f"字幕来源：{stats.get('subtitle_source', {}).get('kind', '-')}"
          f"（{stats.get('subtitle_source', {}).get('cues', 0)} 条原始字幕）")
    print(f"断句结果：{stats.get('sentences', 0)} 句")
    print(f"翻译：{translate.get('sentences', 0)} 句，{translate.get('tokens', 0)} tokens")
    print(f"配音：{tts.get('segments', 0)} 段，{tts.get('characters', 0)} 字符，音色 {tts.get('voice', '-')}")
    print(f"成片：{output.get('resolution', '-')}｜{output.get('size_mb', 0)} MB｜时长 {output.get('duration', 0)}s")
    print(f"发布：{item.publish_status}｜{item.publish_url}")
    print("=" * 68)

    if item.output_path:
        final = settings.data_dir / item.output_path
        print(f"\n成片路径：{final}")
        # 抽两帧便于肉眼确认字幕已烧录
        for index, at in enumerate((3.0, 9.0), start=1):
            frame = out_dir / f"frame_{index}.png"
            try:
                await ffmpeg_utils.run_ffmpeg(["-ss", str(at), "-i", str(final), "-frames:v", "1", "-q:v", "2", str(frame)])
                print(f"抽帧：{frame}")
            except Exception as exc:  # noqa: BLE001
                print(f"抽帧失败：{exc}")
        print(f"中文字幕：{settings.data_dir / item.subtitle_zh_path if item.subtitle_zh_path else '-'}")

    ok = task.status == TaskStatus.SUCCEEDED.value and item.publish_status == "published"
    print("\n结果：" + ("✓ 流水线端到端跑通（Mock 模式）" if ok else "✗ 流水线未跑通"))
    if not keep_mock_media:
        print("提示：Mock 语音合成产出的是静音音轨，仅用于验证对轴逻辑；配置阿里云密钥后会替换为真实配音。")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="离线验证搬运流水线")
    parser.add_argument("--out", type=Path, default=OUT_ROOT, help="输出目录（默认临时目录）")
    parser.add_argument("--aspect", default="9:16", choices=["original", "9:16", "16:9"], help="输出画面比例")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run(args.out.resolve(), args.aspect, args.quiet))


if __name__ == "__main__":
    raise SystemExit(main())

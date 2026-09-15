#!/usr/bin/env python
"""字幕字号/位置快速预览。

痛点：成片重渲染要几十分钟，为了试一个字号不值当。
本脚本直接从已完成的成片里抽一帧，按不同字号各渲染一张，拼成对照图，
几秒钟就能定下合适的值。

用法：
    cd backend
    # 用某个已完成任务的字幕与成片
    .venv/bin/python ../scripts/preview_subtitles.py --task 13
    # 指定成片与字幕文件
    .venv/bin/python ../scripts/preview_subtitles.py --video out.mp4 --subtitle zh.ass
    # 自定义待比较的字号（1080p 基准）
    .venv/bin/python ../scripts/preview_subtitles.py --task 13 --sizes 28,32,36,40,48,56

输出：preview_subtitles.png（同时打印各字号的实测像素高度）
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def _resolve_inputs(task_id: int | None, video: Path | None, subtitle: Path | None, data_dir: Path):
    """确定成片、原文/译文 SRT 的来源。"""
    import sqlite3

    if task_id is not None:
        db = data_dir / "spark.db"
        if not db.exists():
            raise SystemExit(f"找不到数据库：{db}")
        con = sqlite3.connect(db)
        row = con.execute(
            "select output_path, subtitle_source_path, subtitle_zh_path, stats from task_items "
            "where task_id=? and output_path != '' limit 1",
            (task_id,),
        ).fetchone()
        con.close()
        if not row:
            raise SystemExit(f"任务 #{task_id} 没有可用的成片")
        out_rel, en_rel, zh_rel, stats_raw = row
        video = video or (data_dir / out_rel)
        subtitle = subtitle or (data_dir / (zh_rel or en_rel))

    if not video or not video.exists():
        raise SystemExit(f"成片不存在：{video}")
    if not subtitle or not subtitle.exists():
        raise SystemExit(f"字幕不存在：{subtitle}")
    return video, subtitle


async def build_contact_sheet(
    video: Path,
    subtitle: Path,
    *,
    sizes: list[int],
    at: float,
    out_path: Path,
    alignment: str,
    font_name: str,
) -> list[tuple[int, int]]:
    """逐字号渲染同一帧，纵向拼接并标注，返回 [(设定字号, 实测文字高)]。"""
    from app.services import ass_subtitles as A
    from app.services.subtitles import parse_subtitle_file
    from app.utils import ffmpeg as ffmpeg_utils
    from app.utils.binaries import resolve_binary

    ffmpeg = resolve_binary("ffmpeg")
    if not ffmpeg:
        raise SystemExit("未找到 ffmpeg")

    media = await ffmpeg_utils.probe(video)
    width, height = A.output_resolution(media.width, media.height, "original")
    cues = parse_subtitle_file(subtitle)

    # 选一条足够长的字幕来渲染，避免拿到空帧
    target = max(cues, key=lambda c: c.end - c.start) if cues else None
    if target is None:
        raise SystemExit("字幕文件里没有内容")
    stamp = target.start + (target.end - target.start) / 2

    # 输入定位（-ss 在 -i 前）会把输出帧的 PTS 重置为 0，
    # 而 ASS 用的是原始时间轴，直接渲染会「一条都匹配不上」。
    # 这里把待渲染的这条字幕平移到 0 起点，保证一定命中。
    from app.services.subtitles import Cue as _Cue

    preview_cue = _Cue(start=0.0, end=max(0.5, target.end - target.start), text=target.text)

    # 临时文件放系统临时区，避免在 data/ 里留下中间产物
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="spark-preview-"))
    work.mkdir(parents=True, exist_ok=True)
    tiles: list[Path] = []
    measurements: list[tuple[int, int]] = []

    for size in sizes:
        style = A.SubtitleStyle(
            font_name=font_name or ffmpeg_utils.resolve_subtitle_font(""),
            font_size=size,
            margin_v=40,
            alignment=alignment,
            outline=1,
        )
        ass = A.build_ass([preview_cue], width=width, height=height, style=style)
        ass_path = work / f"size_{size}.ass"
        ass_path.write_text(ass, encoding="utf-8")

        raw_png = work / f"raw_{size}.png"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{stamp:.3f}", "-i", str(video), "-frames:v", "1",
                "-vf", f"subtitles={ass_path}", raw_png,
            ],
            check=True,
        )
        # 同一帧的无字幕版本，用于差分隔离出字幕像素
        plain_png = work / "raw_plain.png"
        if not plain_png.exists():
            subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{stamp:.3f}", "-i", str(video), "-frames:v", "1", plain_png,
                ],
                check=True,
            )
        # 标注字号
        label = f"font_size={size}  (1080p baseline, actual {style.scaled_font_size(height)}px)"
        tile = work / f"tile_{size}.png"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw_png),
                "-vf",
                f"drawtext=text='{label}':fontsize=18:fontcolor=yellow:box=1:boxcolor=black@0.6:x=8:y=8",
                tile,
            ],
            check=True,
        )
        tiles.append(tile)

        # 实测文字高度：与「无字幕同帧」做差分，只统计字幕带来的像素变化。
        # 直接找亮像素会被画面本身的亮部带偏（实测所有字号都量成一样的值）。
        diff = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", str(raw_png), "-i", str(plain_png),
                "-filter_complex",
                "[0:v]format=gray[a];[1:v]format=gray[b];[a][b]blend=all_mode=difference,format=gray",
                "-f", "rawvideo", "-",
            ],
            capture_output=True,
        ).stdout
        rows = [y for y in range(height) if any(diff[y * width + x] > 40 for x in range(0, width, 2))]
        measurements.append((size, (rows[-1] - rows[0] + 1) if rows else 0))

    # 纵向拼接成一张对照图（输出是单帧 PNG，用 vstack 而不是 concat）
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for tile in tiles:
        cmd += ["-i", str(tile)]
    inputs = "".join(f"[{i}:v]" for i in range(len(tiles)))
    cmd += [
        "-filter_complex",
        f"{inputs}vstack=inputs={len(tiles)},scale=1280:-2",
        "-frames:v", "1",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)

    import shutil as _shutil
    _shutil.rmtree(work, ignore_errors=True)
    return measurements


def main() -> int:
    parser = argparse.ArgumentParser(description="字幕字号快速预览")
    parser.add_argument("--task", type=int, help="已完成任务的 ID（自动找成片与字幕）")
    parser.add_argument("--video", type=Path, help="成片路径")
    parser.add_argument("--subtitle", type=Path, help="字幕文件路径（srt/ass）")
    parser.add_argument(
        "--sizes", default="28,32,36,40,48,56",
        help="待比较的字号列表（1080p 基准），逗号分隔",
    )
    parser.add_argument("--at", type=float, default=0.0, help="抽帧时间点（秒），默认取最长字幕的中间")
    parser.add_argument("--alignment", default="bottom", choices=["bottom", "middle", "top"])
    parser.add_argument("--font", default="", help="字体名，留空自动选择")
    parser.add_argument("--out", type=Path, default=Path("preview_subtitles.png"), help="输出图片路径")
    parser.add_argument(
        "--data-dir", type=Path,
        default=BACKEND_DIR.parent / "data", help="数据目录",
    )
    args = parser.parse_args()

    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]
    video, subtitle = _resolve_inputs(args.task, args.video, args.subtitle, args.data_dir)

    print(f"成片：{video}")
    print(f"字幕：{subtitle}")
    print(f"待比较字号：{sizes}（1080p 基准）")
    print()

    measurements = asyncio.run(
        build_contact_sheet(
            video, subtitle,
            sizes=sizes,
            at=args.at,
            out_path=args.out.resolve(),
            alignment=args.alignment,
            font_name=args.font,
        )
    )

    print("各字号实测渲染高度：")
    for size, height_px in measurements:
        bar = "█" * max(1, round(height_px / 2))
        print(f"  {size:3d} -> {height_px:3d}px  {bar}")
    print()
    print(f"对照图已生成：{args.out.resolve()}")
    print("参考：行业规范建议字高约占画面高度的 4.5%（1080p 约 48px）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

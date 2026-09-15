"""ffmpeg / ffprobe 封装：音频拼接、变速对齐、混音、烧录字幕、画面比例转换。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings


class FFmpegError(RuntimeError):
    pass


# 常见安装位置：GUI 启动的进程往往拿不到 shell 的 PATH（如 Homebrew）
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin")


def resolve_binary(name: str) -> str | None:
    """在 PATH 与常见目录中查找可执行文件。"""
    found = shutil.which(name)
    if found:
        return found
    for directory in _EXTRA_BIN_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


_cache: dict[str, str | None] = {}


def _bin(name: str) -> str:
    if name not in _cache:
        _cache[name] = resolve_binary(name)
    resolved = _cache[name]
    if not resolved:
        raise FFmpegError(f"未找到可执行文件 {name}，请先安装 ffmpeg（macOS: brew install ffmpeg）")
    return resolved


async def _run(cmd: list[str], *, timeout: float | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FFmpegError(f"命令超时（{timeout}s）: {' '.join(cmd[:3])} ...") from None
    return proc.returncode or 0, stdout.decode("utf-8", "ignore"), stderr.decode("utf-8", "ignore")


def ffmpeg_available() -> bool:
    return bool(resolve_binary(settings.ffmpeg_bin)) and bool(resolve_binary(settings.ffprobe_bin))


async def run_ffmpeg(args: list[str], *, timeout: float | None = 3600) -> None:
    cmd = [_bin(settings.ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y", *args]
    code, _, stderr = await _run(cmd, timeout=timeout)
    if code != 0:
        raise FFmpegError(f"ffmpeg 执行失败(code={code}): {stderr.strip()[-1200:]}")


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    has_video: bool = False


async def probe(path: Path) -> MediaInfo:
    cmd = [
        _bin(settings.ffprobe_bin),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    code, stdout, stderr = await _run(cmd, timeout=120)
    if code != 0:
        raise FFmpegError(f"ffprobe 执行失败: {stderr.strip()[-500:]}")
    data = json.loads(stdout or "{}")
    info = MediaInfo(duration=float(data.get("format", {}).get("duration") or 0.0))
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            info.has_video = True
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
        elif stream.get("codec_type") == "audio":
            info.has_audio = True
    return info


async def audio_duration(path: Path) -> float:
    return (await probe(path)).duration


async def concat_audio(segments: list[Path], out_path: Path) -> Path:
    """把多段音频按时序拼接为单轨（要求各段采样率/声道一致）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not segments:
        raise FFmpegError("没有可拼接的音频片段")
    if len(segments) == 1:
        shutil.copy2(segments[0], out_path)
        return out_path
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text(
        "\n".join(f"file '{seg.resolve().as_posix()}'" for seg in segments),
        encoding="utf-8",
    )
    try:
        await run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
        ])
    finally:
        list_file.unlink(missing_ok=True)
    return out_path


async def make_silence(duration: float, out_path: Path, *, sample_rate: int = 48000) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await run_ffmpeg([
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=stereo",
        "-t", f"{max(duration, 0.01):.3f}",
        "-c:a", "libmp3lame", "-q:a", "4",
        str(out_path),
    ])
    return out_path


async def atempo(src: Path, dst: Path, speed: float) -> Path:
    """变速不变调。speed>1 加快。atempo 单次范围 0.5~2.0，这里做链式处理。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.5f}")
    await run_ffmpeg(["-i", str(src), "-filter:a", ",".join(filters), "-c:a", "libmp3lame", "-q:a", "3", str(dst)])
    return dst


async def stretch_fit(src: Path, dst: Path, target_duration: float, *, max_speed: float = 1.35) -> Path:
    """把一段语音塞进 target_duration 的时间窗：不够长补静音，太长则加速（上限 max_speed）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    actual = await audio_duration(src)
    if target_duration <= 0.05:
        return await atempo(src, dst, max_speed) if actual > 0.05 else src
    if actual <= target_duration:
        gap = target_duration - actual
        if gap < 0.03:
            shutil.copy2(src, dst)
            return dst
        # 用 adelay 在尾部留白：先补静音再拼接，避免重编码两次
        silence = dst.with_name(dst.stem + ".tail.mp3")
        await make_silence(gap, silence)
        await concat_audio([src, silence], dst)
        silence.unlink(missing_ok=True)
        return dst
    speed = min(actual / target_duration, max_speed)
    return await atempo(src, dst, speed)


async def fit_segment(
    src: Path,
    dst: Path,
    target_duration: float,
    *,
    max_speed: float = 1.35,
) -> tuple[Path, float]:
    """把语音严格塞进目标时长，返回 (文件, 实际时长)。

    策略：短了补尾部静音，长了先加速（不超过 max_speed），仍超长则直接截断。
    严格等于目标时长可以避免逐句累积漂移，配音与画面对齐更稳。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    target = max(target_duration, 0.08)
    actual = await audio_duration(src)

    working = src
    if actual > target:
        speed = min(actual / target, max_speed)
        if speed > 1.001:
            sped = dst.with_name(dst.stem + ".tmp.mp3")
            working = await atempo(src, sped, speed)
            actual = await audio_duration(working)

    # 统一按目标时长裁剪/补齐
    await run_ffmpeg([
        "-i", str(working),
        "-af", f"apad=whole_dur={target:.3f}",
        "-t", f"{target:.3f}",
        "-c:a", "libmp3lame", "-q:a", "3",
        str(dst),
    ])

    # 清理临时文件（源文件由调用方管理）
    if working is not src:
        working.unlink(missing_ok=True)
    return dst, target


async def build_timeline_track(
    segments: list[tuple[float, float, Path]],
    out_path: Path,
    *,
    total_duration: float,
    work_dir: Path,
    max_speedup: float = 1.35,
    on_progress=None,
) -> Path:
    """按字幕时间轴把逐句配音铺成一条完整音轨。

    segments: [(start, end, audio_path), ...]
    - 每句用 fit_segment 严格压进 [start, end]
    - 句间空隙用静音填充
    - 末尾补齐到 total_duration
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    ordered = sorted((s for s in segments if s[2] and Path(s[2]).exists()), key=lambda s: s[0])
    if not ordered:
        raise FFmpegError("没有任何可用的配音片段")

    parts: list[Path] = []
    temps: list[Path] = []
    cursor = 0.0
    try:
        for index, (start, end, audio) in enumerate(ordered):
            start = max(0.0, float(start))
            target = max(0.08, float(end) - start)
            if start > cursor + 0.03:
                gap_path = work_dir / f"gap_{index:04d}.mp3"
                await make_silence(start - cursor, gap_path)
                parts.append(gap_path)
                temps.append(gap_path)
                cursor = start

            seg_path = work_dir / f"seg_{index:04d}.mp3"
            await fit_segment(audio, seg_path, target, max_speed=max_speedup)
            parts.append(seg_path)
            temps.append(seg_path)
            cursor += target

            if on_progress and (index % 10 == 0 or index == len(ordered) - 1):
                on_progress((index + 1) / len(ordered), f"对齐第 {index + 1}/{len(ordered)} 句")

        if total_duration and total_duration > cursor + 0.05:
            tail = work_dir / "gap_tail.mp3"
            await make_silence(total_duration - cursor, tail)
            parts.append(tail)
            temps.append(tail)

        await concat_audio(parts, out_path)
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)
        if on_progress:
            on_progress(1.0, "配音时间轴完成")
    return out_path


async def mix_voice_with_bgm(
    voice: Path,
    bgm_source: str | Path,
    out_path: Path,
    *,
    bgm_volume: float = 0.12,
    voice_volume: float = 1.0,
    total_duration: float | None = None,
) -> Path:
    """人声轨 + 原视频音轨（压低）混音。bgm_source 可以是视频文件路径，直接用其音频轨。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    filter_complex = (
        f"[0:a]volume={voice_volume:.3f}[v];"
        f"[1:a]volume={bgm_volume:.3f}[b];"
        "[v][b]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )
    args = [
        "-i", str(voice),
        "-i", str(bgm_source),
        "-filter_complex", filter_complex,
        "-map", "[aout]",
    ]
    if total_duration:
        args += ["-t", f"{total_duration:.3f}"]
    args += ["-c:a", "aac", "-b:a", "192k", str(out_path)]
    await run_ffmpeg(args)
    return out_path


# 中文字幕字体候选：按「跨平台可用性 + libass 实际能加载」排序。
# 注意：macOS 的 PingFang SC 会被 fontconfig 解析到 libass 打不开的 .ttc（PingFangUI.ttc），
# 触发 "Error opening font" 并静默回退，因此不列入候选；确需使用请显式配置并自行验证。
_FONT_CANDIDATES = (
    "Heiti SC",
    "Hiragino Sans GB",
    "Source Han Sans CN",
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Microsoft YaHei",
    "WenQuanYi Micro Hei",
    "Songti SC",
    "Arial Unicode MS",
)
_font_cache: set[str] | None = None


def available_fonts() -> set[str]:
    """通过 fontconfig 列出系统可用字体名（进程内缓存；无 fontconfig 时返回空集）。"""
    global _font_cache
    if _font_cache is not None:
        return _font_cache
    names: set[str] = set()
    fc_list = resolve_binary("fc-list")
    if fc_list:
        try:
            output = subprocess.run(
                [fc_list, ":", "family"], capture_output=True, text=True, timeout=20
            ).stdout
            for line in output.splitlines():
                for family in line.split(","):
                    family = family.strip().replace("\\-", "-")
                    if family:
                        names.add(family)
        except Exception:  # noqa: BLE001 - 字体探测失败不应中断渲染
            pass
    _font_cache = names
    return names


def resolve_subtitle_font(preferred: str = "") -> str:
    """挑一个系统里真实存在、且 libass 能加载的中文字体。

    - 显式配置且系统存在该字体时优先使用；
    - 否则按候选列表取第一个可用项（避免静默回退到无中文字形的字体）；
    - 无法探测字体时退回候选列表首项。
    """
    fonts = available_fonts()
    preferred = (preferred or "").strip()
    if preferred and (not fonts or preferred in fonts):
        return preferred
    if fonts:
        for candidate in _FONT_CANDIDATES:
            if candidate in fonts:
                return candidate
    return _FONT_CANDIDATES[0]


def build_subtitle_style(font_size: int, font_name: str, margin_v: int) -> str:
    resolved = resolve_subtitle_font(font_name)
    return (
        f"FontName={resolved},FontSize={font_size},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,"
        "Outline=2,Shadow=0,Alignment=2,"
        f"MarginV={margin_v}"
    )


def _escape_sub_path(path: Path) -> str:
    # ffmpeg subtitles 滤镜对路径中的冒号/反斜杠敏感
    text = path.resolve().as_posix()
    return text.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


async def burn_subtitles(video: Path, subtitle: Path, out_path: Path, *, style: str) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await run_ffmpeg([
        "-i", str(video),
        "-vf", f"subtitles='{_escape_sub_path(subtitle)}':force_style='{style}'",
        "-c:a", "copy",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        str(out_path),
    ])
    return out_path


async def render_final(
    *,
    video: Path,
    audio: Path | None,
    subtitle: Path | None,
    out_path: Path,
    target_aspect: str = "original",
    burn: bool = True,
    style: str = "",
    crf: int = 20,
    preset: str = "medium",
) -> Path:
    """最终合成：替换音轨 + 可选画面比例转换 + 可选烧录字幕。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = ["-i", str(video)]
    if audio is not None:
        args += ["-i", str(audio)]

    filters: list[str] = []
    if target_aspect == "9:16":
        # 竖屏：主画面等比居中，背景用放大模糊铺满，避免黑边
        filters.append(
            "split=2[bg][fg];"
            "[bg]scale=w=1080:h=1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5[bgb];"
            "[fg]scale=w=1080:h=1920:force_original_aspect_ratio=decrease[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )
    elif target_aspect == "16:9":
        filters.append(
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    if burn and subtitle is not None:
        sub_filter = f"subtitles='{_escape_sub_path(subtitle)}':force_style='{style}'"
        filters.append(sub_filter)

    if filters:
        args += ["-vf", ",".join(filters)]
    if audio is not None:
        args += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
    await run_ffmpeg(args)
    return out_path


async def extract_cover(video: Path, out_path: Path, *, at: float = 1.0) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await run_ffmpeg(["-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out_path)])
    return out_path

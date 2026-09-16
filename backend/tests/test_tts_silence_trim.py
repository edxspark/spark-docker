"""配音「断断续续」的回归测试。

事故现象：用 ChatTTS 配音，成片听起来一顿一顿、句子说不完整，长文本尤其明显。

排查结论（实测数据）：ChatTTS 每次生成都会在句首留 0~0.9 秒静音，
而 fit_segment 只按目标时长 apad/裁剪，**从不裁这些静音**。于是

  1. 窗口不够时 -t 从尾部硬切 → 句尾被切掉；
  2. 窗口比首静音还短时，截出来的整段都是静音 → 整句话凭空消失
     （实测「它们之间」窗口 0.25s、首静音 0.35s，语音损失 100%）；
  3. atempo 的加速额度被静音白吃，真正需要压缩的语音反而没被压。

真实字幕样本上，修复前有一半的句子被截断、平均丢 33.7% 语音。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.utils import ffmpeg as ffmpeg_utils
from app.utils.ffmpeg import (
    concat_audio,
    ffmpeg_available,
    fit_segment,
    resolve_binary,
    trim_silence,
)

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="未安装 ffmpeg")

_SAMPLE_RATE = 24000


def _silence(duration: float, out: Path) -> Path:
    subprocess.run(
        [
            resolve_binary("ffmpeg"), "-v", "error", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r={_SAMPLE_RATE}:cl=mono",
            "-t", f"{duration:.3f}",
            "-c:a", "libmp3lame", "-q:a", "3", str(out),
        ],
        check=True,
    )
    return out


def _tone(duration: float, out: Path, freq: int = 440) -> Path:
    subprocess.run(
        [
            resolve_binary("ffmpeg"), "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}:sample_rate={_SAMPLE_RATE}",
            "-c:a", "libmp3lame", "-q:a", "3", str(out),
        ],
        check=True,
    )
    return out


async def _make_clip(tmp: Path, *, lead: float, speech: float, tail: float = 0.0) -> Path:
    """造一段「首静音 + 语音 + 尾静音」的素材，模拟 ChatTTS 的输出。"""
    pieces: list[Path] = []
    if lead > 0:
        pieces.append(_silence(lead, tmp / "lead.mp3"))
    pieces.append(_tone(speech, tmp / "tone.mp3"))
    if tail > 0:
        pieces.append(_silence(tail, tmp / "tail.mp3"))
    if len(pieces) == 1:
        return pieces[0]
    out = tmp / "clip.mp3"
    await concat_audio(pieces, out)
    return out


def _mean_volume(path: Path) -> float:
    """返回平均音量(dB)。整段静音约 -90dB，有语音则明显更高。"""
    result = subprocess.run(
        [resolve_binary("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0].strip())
    return -100.0


def _speech_seconds(path: Path) -> float:
    """真正有人声的秒数 = 总时长 - 静音时长。判断「丢了多少语音」比音量更直接。"""
    result = subprocess.run(
        [resolve_binary("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
         "-af", "silencedetect=noise=-38dB:d=0.15", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    import re as _re

    events = [
        (m.group(1), float(m.group(2)))
        for m in _re.finditer(r"silence_(start|end):\s*(-?[\d.]+)", result.stderr)
    ]
    spans: list[tuple[float, float]] = []
    current: float | None = None
    for kind, at in events:
        if kind == "start":
            current = at
        elif current is not None:
            spans.append((current, at))
            current = None
    total = float(subprocess.run(
        [resolve_binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip() or 0)
    if current is not None:
        spans.append((current, total))
    return total - sum(end - start for start, end in spans)


async def _duration(path: Path) -> float:
    return await ffmpeg_utils.audio_duration(path)


# --------------------------------------------------------------------- trim_silence


async def test_trim_silence_removes_leading_silence(tmp_path):
    """句首静音必须被裁掉——它是「整句话变静音」的直接原因。"""
    clip = await _make_clip(tmp_path, lead=1.0, speech=0.6)
    before = await _duration(clip)

    out = tmp_path / "trimmed.mp3"
    await trim_silence(clip, out)
    after = await _duration(out)

    assert after < before - 0.6, f"首静音没被裁掉：{before:.2f}s → {after:.2f}s"
    # 语音本身还在
    assert _mean_volume(out) > -50, "裁剪后语音丢失了"


async def test_trim_silence_keeps_internal_pauses(tmp_path):
    """句中停顿是自然韵律，不能被一起裁掉（否则变成念经）。"""
    a = _tone(0.5, tmp_path / "a.mp3")
    gap = _silence(0.5, tmp_path / "gap.mp3")
    b = _tone(0.5, tmp_path / "b.mp3")
    clip = tmp_path / "two.mp3"
    await concat_audio([a, gap, b], clip)

    out = tmp_path / "trimmed.mp3"
    await trim_silence(clip, out)

    # 句中 0.5s 停顿应当保留，总时长仍接近 1.5s
    assert await _duration(out) > 1.2, "句中停顿被误删"


async def test_trim_silence_on_pure_silence_returns_usable_file(tmp_path):
    """Mock 提供者产出的是整段静音：不能裁成空文件，否则后续 apad 无从补起。"""
    clip = _silence(0.8, tmp_path / "silent.mp3")
    out = tmp_path / "trimmed.mp3"
    await trim_silence(clip, out)

    assert out.exists() and out.stat().st_size > 0
    assert await _duration(out) > 0.1


# --------------------------------------------------------------------- fit_segment


async def test_fit_segment_no_longer_deletes_short_cues(tmp_path):
    """事故核心：窗口比首静音还短时，修复前整句话被切成静音。

    复刻真实数据「它们之间」：窗口 0.25s，ChatTTS 输出首静音 0.35s + 语音 0.4s。
    """
    clip = await _make_clip(tmp_path, lead=0.35, speech=0.40)
    window = 0.25

    broken = tmp_path / "broken.mp3"
    await fit_segment(clip, broken, window, trim=False)
    fixed = tmp_path / "fixed.mp3"
    await fit_segment(clip, fixed, window, trim=True)

    assert _mean_volume(broken) < -50, "前提不成立：不裁剪时本该整段静音"
    assert _mean_volume(fixed) > -50, "裁剪后仍然没有语音，句子被吞了"


async def test_fit_segment_keeps_more_speech_than_without_trim(tmp_path):
    """裁剪后加速额度用在真正的语音上，保留率必须更高。"""
    clip = await _make_clip(tmp_path, lead=0.5, speech=2.0)
    window = 1.6  # 2.5/1.6 = 1.56 > 1.35，不裁就必然截断

    broken = tmp_path / "broken.mp3"
    await fit_segment(clip, broken, window, trim=False)
    fixed = tmp_path / "fixed.mp3"
    await fit_segment(clip, fixed, window, trim=True)

    # 不裁：0.5s 首静音占掉窗口，加速后仍超长被 -t 截掉一截语音
    # 裁后：首静音被去掉，同样窗口里装得下更多语音
    broken_speech = _speech_seconds(broken)
    fixed_speech = _speech_seconds(fixed)
    assert fixed_speech > broken_speech * 1.15, (
        f"裁剪没有提升语音保留：{broken_speech:.2f}s → {fixed_speech:.2f}s"
    )


async def test_fit_segment_survives_all_silence_segment(tmp_path):
    """Mock 提供者产出整段静音：裁剪会得到空文件，此时必须回退而不是报错。

    这里曾经真的抛了 FFmpegError——ffprobe 遇到没有 MP3 帧的空文件是报错而非返回 0。
    """
    clip = _silence(0.8, tmp_path / "silent.mp3")
    out = tmp_path / "fit.mp3"
    _, actual = await fit_segment(clip, out, 1.0, trim=True)

    assert actual == pytest.approx(1.0, abs=1e-6)
    assert await _duration(out) == pytest.approx(1.0, abs=0.08)


async def test_fit_segment_still_lands_exactly_on_target(tmp_path):
    """裁剪不能破坏「严格等于窗口时长」这个前提——否则逐句累积漂移。"""
    clip = await _make_clip(tmp_path, lead=0.4, speech=1.0, tail=0.3)
    for window in (0.25, 1.0, 2.5):
        out = tmp_path / f"fit_{window}.mp3"
        _, actual = await fit_segment(clip, out, window)
        assert actual == pytest.approx(max(window, 0.08), abs=1e-6)
        assert await _duration(out) == pytest.approx(max(window, 0.08), abs=0.08)


async def test_fit_segment_cleans_up_intermediate_files(tmp_path):
    """既裁剪又变速时，中间文件（.trim.wav / .tmp.mp3）不能留在输出目录。"""
    clip = await _make_clip(tmp_path, lead=0.5, speech=2.0)
    out = tmp_path / "out.mp3"
    await fit_segment(clip, out, 1.6, trim=True)

    leftovers = [
        p.name for p in tmp_path.iterdir()
        if ".trim." in p.name or ".tmp." in p.name
    ]
    assert leftovers == [], f"残留中间文件：{leftovers}"


# --------------------------------------------------------------------- ChatTTS 侧


def test_chattts_detects_real_container_from_magic():
    """ChatTTS 官方 webui 固定返回 wav，而调用方给的路径是 .mp3。

    必须能按文件头认出真实容器，否则扩展名说谎（wav 字节装进 .mp3），
    体积大三倍，外部工具/浏览器预览还会按扩展名误判。
    """
    chattts = pytest.importorskip("app.providers.tts.chattts")
    detect = getattr(chattts, "_suffix_for", None)
    if detect is None:
        # ChatTTS 提供者本身仍是未提交的在建功能；容器探测随它一起落地
        pytest.skip("ChatTTS 提供者尚未包含容器探测")

    assert detect(b"RIFF\x00\x00\x00\x00WAVEfmt ") == ".wav"
    assert detect(b"ID3\x04\x00") == ".mp3"
    assert detect(b"\xff\xfb\x90\x00") == ".mp3"
    assert detect(b"OggS\x00\x02") == ".ogg"
    assert detect(b"\x00\x01\x02\x03") == ""

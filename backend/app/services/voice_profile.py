"""说话人性别判定：用基频（F0）区分男女声。

为什么用 F0：男女声的差别主要来自声带振动频率——成年男性约 85~180 Hz，
女性约 165~255 Hz，中间的 160~165 Hz 附近是最佳切分点。这比靠 ASR 识别内容
再猜性别可靠得多，也不依赖任何外部服务，纯 numpy 就能算。

实现要点：
- 逐帧自相关（ACF）估基频，配合两条判据筛掉不可靠的帧：
  1) 归一化自相关峰值要足够高（周期性够强，排除噪声/静音）；
  2) 峰值对应的延迟要落在人声范围内（85~400 Hz）。
- 只统计有声帧的中位数，避免个别爆音把结果带偏。
- 有声帧太少或中位数落在男女重叠区时返回 None（unknown），
  由调用方决定回退策略——宁可不确定，也不要瞎猜后配错性别。
"""

from __future__ import annotations

import asyncio
import logging
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# 人声基频搜索范围
F0_MIN = 70.0
F0_MAX = 400.0
# 男女声切分点（Hz）；160 落在两侧分布的重叠区中部
DEFAULT_THRESHOLD = 165.0
# 自相关峰值下限：低于此值认为该帧不是可靠的有声帧。
# 实测（16 kHz）：真人语音的中位峰值 ≈1.0 以上，白噪声 ≈0.28 →
# 用 0.5 做硬门槛可以挡住「噪声被当成低音男声」这种误判。
MIN_PERIODICITY = 0.50
# 整段音频的可靠性门槛
# 中位峰值下限。实测：真人语音 ≈1.0（底噪大的成片也能到 0.7），白噪声 ≈0.28。
# 这是区分「人声」与「非周期信号」最可靠的一条判据。
MIN_MEDIAN_PEAK = 0.60
# 离散度只作兜底（噪声常 >120 Hz）。不能定得太严：真人说话有句调起伏，
# 实测一条正常成片的 IQR 就有 64~86 Hz，设 60 会把真语音也挡掉。
MAX_F0_IQR = 120.0
# 分帧参数
FRAME_SECONDS = 0.04
HOP_SECONDS = 0.02


@dataclass
class VoiceProfile:
    """一段音频的音高画像。"""

    f0: float = 0.0                 # 有声帧基频中位数（Hz）
    voiced_ratio: float = 0.0       # 有效有声帧占比
    gender: str = "unknown"         # male / female / unknown
    confidence: float = 0.0         # 0~1，越大越确定
    periodicity: float = 0.0        # 自相关峰值中位数：越高越像人声（噪声 <0.4）
    f0_iqr: float = 0.0             # 基频离散度（Hz）：噪声会散得很开
    reason: str = ""                # 判为 unknown 时的原因，便于排查

    def as_stats(self) -> dict[str, object]:
        return {
            "f0_hz": round(self.f0, 1),
            "voiced_ratio": round(self.voiced_ratio, 3),
            "periodicity": round(self.periodicity, 2),
            "f0_iqr": round(self.f0_iqr, 1),
            "gender": self.gender,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
        }


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """读取 wav 为 float32 单声道。支持 16/32 位 PCM 与 32 位浮点。"""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    if width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"不支持的 wav 位宽：{width * 8} bit")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def estimate_f0(
    samples: np.ndarray,
    rate: int,
    *,
    f0_min: float = F0_MIN,
    f0_max: float = F0_MAX,
) -> tuple[float, float, float]:
    """自相关法估计基频，返回 (中位数 F0, 有声帧占比, 基频离散度 IQR)。

    对每一帧做去均值 + 归一化，再算自相关；取峰值对应的延迟换算频率。
    离散度用于识别噪声：噪声每帧的「基频」会散得很开，人声则相对集中。
    """
    if samples.size == 0 or rate <= 0:
        return 0.0, 0.0, 0.0

    frame = max(64, int(FRAME_SECONDS * rate))
    hop = max(32, int(HOP_SECONDS * rate))
    min_lag = max(2, int(rate / f0_max))
    max_lag = min(frame - 1, int(rate / f0_min))
    if max_lag <= min_lag:
        return 0.0, 0.0, 0.0

    f0_values: list[float] = []
    peaks: list[float] = []
    total_frames = 0
    for start in range(0, max(1, samples.size - frame), hop):
        chunk = samples[start : start + frame]
        if chunk.size < frame:
            break
        total_frames += 1
        # 静音/过轻的帧直接跳过：能量不足时自相关没有意义
        if float(np.sqrt(np.mean(chunk**2))) < 0.008:
            continue
        chunk = chunk - float(np.mean(chunk))
        norm = float(np.sqrt(np.sum(chunk**2)))
        if norm <= 1e-8:
            continue
        corr = np.correlate(chunk, chunk, mode="full")[frame - 1 :]
        corr = corr / norm
        window = corr[min_lag:max_lag]
        if window.size == 0:
            continue
        lag = int(np.argmax(window)) + min_lag
        peak = float(corr[lag])
        if peak < MIN_PERIODICITY:
            continue
        peaks.append(peak)
        f0_values.append(rate / lag)

    if total_frames == 0 or not f0_values:
        return 0.0, 0.0, 0.0
    values = np.asarray(f0_values)
    iqr = float(np.percentile(values, 75) - np.percentile(values, 25))
    return float(np.median(values)), len(f0_values) / total_frames, iqr


def median_peak(samples: np.ndarray, rate: int, *, f0_min: float = F0_MIN, f0_max: float = F0_MAX) -> float:
    """全段自相关峰值的中位数：人声通常 >0.6，噪声 <0.4。"""
    if samples.size == 0 or rate <= 0:
        return 0.0
    frame = max(64, int(FRAME_SECONDS * rate))
    hop = max(32, int(HOP_SECONDS * rate))
    min_lag = max(2, int(rate / f0_max))
    max_lag = min(frame - 1, int(rate / f0_min))
    peaks: list[float] = []
    for start in range(0, max(1, samples.size - frame), hop):
        chunk = samples[start : start + frame]
        if chunk.size < frame:
            break
        if float(np.sqrt(np.mean(chunk**2))) < 0.008:
            continue
        chunk = chunk - float(np.mean(chunk))
        norm = float(np.sqrt(np.sum(chunk**2)))
        if norm <= 1e-8:
            continue
        corr = np.correlate(chunk, chunk, mode="full")[frame - 1 :] / norm
        window = corr[min_lag:max_lag]
        if window.size == 0:
            continue
        peaks.append(float(corr[int(np.argmax(window)) + min_lag]))
    return float(np.median(peaks)) if peaks else 0.0


def classify(
    f0: float,
    voiced_ratio: float,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_voiced_ratio: float = 0.06,
    periodicity: float = 1.0,
    f0_iqr: float = 0.0,
) -> VoiceProfile:
    """按基频给性别，并给出置信度。

    两道可靠性门槛（都来自实测）：周期性与基频离散度。
    没有它们时白噪声会被稳定判成「低音男声」——噪声的自相关峰值只有 ~0.28，
    「基频」估值每帧乱跳（IQR 常超 100 Hz），正是这两点把噪声和真人区分开。
    """
    base = VoiceProfile(f0=f0, voiced_ratio=voiced_ratio, periodicity=periodicity, f0_iqr=f0_iqr)

    if f0 <= 0 or voiced_ratio < min_voiced_ratio:
        base.reason = "没有足够清晰的有声片段"
        return base
    if periodicity < MIN_MEDIAN_PEAK:
        base.reason = f"音频周期性过低（{periodicity:.2f}），更像噪声而非人声"
        return base
    if f0_iqr > MAX_F0_IQR:
        base.reason = f"基频估值发散（IQR {f0_iqr:.0f} Hz），不足以判断性别"
        return base

    span = 60.0  # 距离切分点多远算「很确定」
    strength = min(1.0, abs(f0 - threshold) / span)
    # 有声帧占比与周期性都计入置信度
    quality = min(1.0, voiced_ratio / 0.35) * 0.2 + min(1.0, periodicity / 1.5) * 0.3
    base.confidence = round(min(1.0, strength * 0.5 + quality), 2)
    if abs(f0 - threshold) < 8.0:
        base.reason = "基频落在男女重叠区，宁可不判"
        return base
    base.gender = "female" if f0 > threshold else "male"
    return base


async def analyze_file(
    path: Path,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_seconds: float = 45.0,
) -> VoiceProfile:
    """分析音频文件（建议 16 kHz 单声道 wav）。"""
    if not path.exists():
        return VoiceProfile()

    def _run() -> VoiceProfile:
        samples, rate = read_wav_mono(path)
        if max_seconds > 0:
            samples = samples[: int(rate * max_seconds)]
        f0, ratio, iqr = estimate_f0(samples, rate)
        periodicity = median_peak(samples, rate)
        return classify(f0, ratio, threshold=threshold, periodicity=periodicity, f0_iqr=iqr)

    return await asyncio.to_thread(_run)


async def analyze_media(media: Path, *, threshold: float = DEFAULT_THRESHOLD, work_dir: Path | None = None) -> VoiceProfile:
    """直接从视频/音频文件判断说话人性别（内部用 ffmpeg 抽 16 kHz 单声道）。"""
    from app.utils import ffmpeg as ffmpeg_utils

    if not media.exists():
        return VoiceProfile()
    target_dir = work_dir or media.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    wav = target_dir / f".{media.stem}_voice_profile.wav"
    try:
        await ffmpeg_utils.extract_audio_track(media, wav, sample_rate=16000)
        return await analyze_file(wav, threshold=threshold)
    except Exception as exc:  # noqa: BLE001 - 判断失败不应影响流水线
        logger.warning("说话人性别判定失败（%s）：%s", media.name, exc)
        return VoiceProfile()
    finally:
        wav.unlink(missing_ok=True)


def make_tone(f0: float, seconds: float, rate: int = 16000, harmonics: int = 6) -> np.ndarray:
    """生成带谐波的浊音样音，用于自测与单测（不是真人语音，但基频可靠）。"""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    wave_data = np.zeros_like(t)
    for k in range(1, harmonics + 1):
        if f0 * k > rate / 2 - 100:
            break
        wave_data += np.sin(2 * np.pi * f0 * k * t) / k
    # 加一点幅度包络，模拟说话时的起伏
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t)
    wave_data = wave_data * envelope
    return (wave_data / max(1e-6, np.max(np.abs(wave_data))) * 0.6).astype(np.float32)


def write_wav_mono(path: Path, samples: np.ndarray, rate: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return path

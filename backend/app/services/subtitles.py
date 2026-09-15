"""字幕解析、清洗与断句。

支持 SRT / WebVTT / YouTube json3 三种来源，重点是处理 YouTube 自动字幕的
"滚动重复行" 问题，并把碎片化成句，便于逐句翻译与配音对轴。
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

# --------------------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------------------


@dataclass
class Cue:
    """一条字幕。start/end 为秒。"""

    start: float
    end: float
    text: str
    index: int = 0
    source_indexes: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def shifted(self, offset: float) -> Cue:
        return replace(self, start=max(0.0, self.start + offset), end=max(0.0, self.end + offset))


# --------------------------------------------------------------------------------------
# 时间戳
# --------------------------------------------------------------------------------------

_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})")


def parse_timestamp(value: str) -> float:
    match = _TS_RE.search(value.strip())
    if not match:
        raise ValueError(f"无法解析时间戳: {value!r}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int(match.group(4).ljust(3, "0"))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    whole = int(secs)
    millis = int(round((secs - whole) * 1000))
    if millis == 1000:  # 四舍五入进位
        whole += 1
        millis = 0
    if whole == 60:
        minutes += 1
        whole = 0
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"


# --------------------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_VTT_CUE_SETTING_RE = re.compile(r"\s+(?:align|line|position|size|region|vertical):\S+")


def clean_caption_text(text: str) -> str:
    text = html.unescape(text or "")
    text = _TAG_RE.sub("", text)          # <c.colorE5E5E5>、<00:00:01.234> 之类
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _clean_block_lines(lines: list[str], previous: str) -> tuple[list[str], str]:
    """清洗一个字幕块内的文本行，并去掉与「上一条字幕最后一行」重复的滚动行。

    YouTube 自动字幕的滚动模式会让每条字幕都重复带上一条的内容，
    因此去重必须跨条目进行，否则会把上一句重复计入。
    返回 (保留的行, 本条最后一行)。
    """
    cleaned = [clean_caption_text(line) for line in lines]
    cleaned = [line for line in cleaned if line]
    kept: list[str] = []
    for line in cleaned:
        if line == previous:
            continue
        if kept and line == kept[-1]:
            continue
        kept.append(line)
    return kept, (kept[-1] if kept else previous)


def parse_srt(content: str) -> list[Cue]:
    cues: list[Cue] = []
    previous_line = ""
    blocks = re.split(r"\r?\n\s*\r?\n", content.strip())
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        ts_line_idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if ts_line_idx is None:
            continue
        start_raw, _, end_raw = lines[ts_line_idx].partition("-->")
        try:
            start = parse_timestamp(start_raw)
            end = parse_timestamp(end_raw)
        except ValueError:
            continue
        kept, previous_line = _clean_block_lines(lines[ts_line_idx + 1 :], previous_line)
        text = " ".join(kept)
        if text:
            cues.append(Cue(start=start, end=max(end, start + 0.05), text=text))
    return cues


def parse_vtt(content: str) -> list[Cue]:
    cues: list[Cue] = []
    previous_line = ""
    body = re.sub(r"^WEBVTT.*?(?:\r?\n\r?\n)", "", content.strip(), count=1, flags=re.S)
    for block in re.split(r"\r?\n\s*\r?\n", body):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        ts_line_idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if ts_line_idx is None:
            continue
        start_raw, _, end_raw = lines[ts_line_idx].partition("-->")
        try:
            start = parse_timestamp(start_raw)
            end = parse_timestamp(_VTT_CUE_SETTING_RE.sub("", end_raw))
        except ValueError:
            continue
        kept, previous_line = _clean_block_lines(lines[ts_line_idx + 1 :], previous_line)
        text = " ".join(kept)
        if text:
            cues.append(Cue(start=start, end=max(end, start + 0.05), text=text))
    return cues


def parse_json3(content: str) -> list[Cue]:
    """YouTube 自动字幕 json3 格式。"""
    data = json.loads(content)
    cues: list[Cue] = []
    for event in data.get("events", []):
        segs = event.get("segs") or []
        text = clean_caption_text("".join(seg.get("utf8", "") for seg in segs))
        if not text:
            continue
        start = float(event.get("tStartMs", 0)) / 1000.0
        duration = float(event.get("dDurationMs", 0)) / 1000.0
        cues.append(Cue(start=start, end=start + max(duration, 0.05), text=text))
    return cues


def parse_subtitle_file(path: Path) -> list[Cue]:
    """按扩展名/内容自动选择解析器。"""
    content = path.read_text(encoding="utf-8", errors="ignore")
    suffix = path.suffix.lower()
    if suffix == ".json3" or content.lstrip().startswith("{"):
        return parse_json3(content)
    if suffix == ".vtt" or content.lstrip().startswith("WEBVTT"):
        return parse_vtt(content)
    return parse_srt(content)


# --------------------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------------------


def to_srt(cues: list[Cue]) -> str:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        text = cue.text.strip()
        if not text:
            continue
        blocks.append(f"{i}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def to_plain_text(cues: list[Cue]) -> str:
    return "\n".join(cue.text for cue in cues if cue.text.strip())


# --------------------------------------------------------------------------------------
# 清洗与断句
# --------------------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?。！？…](?=\s|$)")
_TRAILING_JUNK = re.compile(r"^[\s\-–—>]+|[\s]+$")


def normalize_cues(cues: list[Cue], *, min_duration: float = 0.25) -> list[Cue]:
    """排序、去重、修复时间轴重叠与过短条目。"""
    cleaned: list[Cue] = []
    for cue in sorted(cues, key=lambda c: c.start):
        text = _TRAILING_JUNK.sub("", clean_caption_text(cue.text))
        if not text:
            continue
        start = max(0.0, cue.start)
        end = max(cue.end, start + min_duration)
        if cleaned and start < cleaned[-1].end:
            # 与上一条重叠：取中点切开，保持单调递增
            prev = cleaned[-1]
            mid = (prev.end + start) / 2
            if mid - prev.start >= min_duration:
                prev.end = mid
                start = mid
            else:
                start = prev.end
            end = max(end, start + min_duration)
        cleaned.append(Cue(start=start, end=end, text=text, source_indexes=list(cue.source_indexes)))
    return cleaned


def merge_into_sentences(
    cues: list[Cue],
    *,
    max_duration: float = 8.0,
    max_chars: int = 120,
    min_duration: float = 0.6,
) -> list[Cue]:
    """把碎片化字幕合并成完整句子（按句末标点或长度上限切分）。"""
    sentences: list[Cue] = []
    buf: list[str] = []
    buf_start: float | None = None
    buf_end = 0.0
    buf_sources: list[int] = []

    def flush() -> None:
        nonlocal buf, buf_start, buf_end, buf_sources
        text = " ".join(part.strip() for part in buf if part.strip()).strip()
        if text and buf_start is not None:
            sentences.append(
                Cue(
                    start=buf_start,
                    end=max(buf_end, buf_start + 0.05),
                    text=text,
                    source_indexes=list(buf_sources),
                )
            )
        buf, buf_start, buf_end, buf_sources = [], None, 0.0, []

    for idx, cue in enumerate(cues):
        text = cue.text.strip()
        if not text:
            continue
        if buf_start is None:
            buf_start = cue.start
        buf.append(text)
        buf_end = cue.end
        buf_sources.append(idx)

        joined = " ".join(buf)
        ends_sentence = bool(_SENTENCE_END.search(text))
        too_long = len(joined) >= max_chars or (buf_end - buf_start) >= max_duration
        if ends_sentence or too_long:
            flush()

    flush()

    # 合并过短且相邻的句子，避免配音碎片化
    merged: list[Cue] = []
    for sentence in sentences:
        if (
            merged
            and sentence.duration < min_duration
            and (sentence.end - merged[-1].start) < max_duration
            and len(merged[-1].text) + len(sentence.text) + 1 <= max_chars
        ):
            prev = merged[-1]
            prev.text = f"{prev.text} {sentence.text}".strip()
            prev.end = sentence.end
            prev.source_indexes += sentence.source_indexes
        else:
            merged.append(sentence)
    return merged


def reindex(cues: list[Cue]) -> list[Cue]:
    return [replace(cue, index=i) for i, cue in enumerate(cues, start=1)]

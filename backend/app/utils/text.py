"""通用文本工具：TTS 文本切分、标题清洗、话题抽取。"""

from __future__ import annotations

import re
import unicodedata

# 中文/英文句末标点
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；!?;：:，,、\.])\s*")


def strip_all_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_punct(text: str) -> str:
    """把英文标点转换为中文全角标点，提升 TTS 自然度。"""
    table = {
        ",": "，", ".": "。", "?": "？", "!": "！", ";": "；", ":": "：",
        "(": "（", ")": "）", "“": "「", "”": "」",
    }
    out = []
    for ch in text:
        out.append(table.get(ch, ch))
    result = "".join(out)
    # 折叠重复的句末标点
    result = re.sub(r"([。！？…])\1+", r"\1", result)
    result = re.sub(r"，{2,}", "，", result)
    # 去掉小数点被误判的情况：1。5 -> 1.5
    result = re.sub(r"(?<=\d)。(?=\d)", ".", result)
    return result


def split_for_tts(text: str, max_chars: int = 280) -> list[str]:
    """把长文本切成 <= max_chars 的片段，优先在标点处断开（阿里云单请求上限 300 字符）。"""
    text = strip_all_whitespace(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    current = ""
    for token in _SENTENCE_BOUNDARY.split(text):
        if not token:
            continue
        if len(current) + len(token) <= max_chars:
            current += token
            continue
        if current:
            pieces.append(current)
            current = ""
        # 单个 token 本身超长：硬切
        while len(token) > max_chars:
            pieces.append(token[:max_chars])
            token = token[max_chars:]
        current = token
    if current:
        pieces.append(current)
    return [p for p in pieces if p.strip()]


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = strip_all_whitespace(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def sanitize_filename(name: str, *, max_length: int = 80, fallback: str = "untitled") -> str:
    """生成安全的文件名：去掉路径分隔符与控制字符，限制长度。"""
    name = unicodedata.normalize("NFKC", name or "")
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" ._")
    return name or fallback


_TAG_STOPWORDS = {"video", "youtube", "shorts", "vlog", "official", "channel"}


def extract_tags(text: str, *, limit: int = 5, min_length: int = 2) -> list[str]:
    """从标题/描述里猜话题标签：# 开头的显式标签优先，其次取高频长词。"""
    if not text:
        return []
    explicit = re.findall(rf"#([^\s#]{{{min_length},20}})", text)
    tags: list[str] = []
    for tag in explicit:
        tag = strip_all_whitespace(tag)
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) >= limit:
        return tags[:limit]

    words = re.findall(r"[A-Za-z\u4e00-\u9fff]{2,12}", text)
    freq: dict[str, int] = {}
    for word in words:
        low = word.lower()
        if low in _TAG_STOPWORDS:
            continue
        freq[low] = freq.get(low, 0) + 1
    for word, _count in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])):
        if len(tags) >= limit:
            break
        if word not in tags:
            tags.append(word)
    return tags[:limit]


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数，用于成本统计（中文按 1 字≈1 token，英文按 4 字符≈1 token）。"""
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - chinese
    other_tokens = -(-other // 4) if other > 0 else 0  # 向上取整
    return chinese + other_tokens

"""送给 TTS 之前的文本归一化（主要针对 ChatTTS）。

为什么需要它
------------
ChatTTS 是对话式中文模型，对**混在中文里的英文缩写、阿拉伯数字、特殊符号**很不友好：
「AI创业」「2024年」「15.5%」「GPT-4」这类写法经常被按中文逐字乱读、或直接吞掉。
LongAudio 那类项目就是靠一层文本归一化（缩写拆字母、数字转中文读法、标点规整）
把这类读错问题压下去的。

**只作用于送给 TTS 的文本**：字幕、标题、话题都保持原样——
观众看到的应当是「AI创业」，而不是「A I 创业」。

设计取舍
--------
- 纯标准库实现（不引入 pypinyin / num2words）：本项目要能离线跑通打包与测试，
  为几个读音规则增加依赖不划算。因此数字按「阿拉伯数字 + 中文单位」处理
  （"2024" → "2024"→「两千零二十四」这种完全口语化转换容易出错，
  这里选择保留数字并交给模型读，只在**明确会读错**的地方动手：
  百分比、小数、单位、以及单个数字的读法）。
- 规则表可配置：把常见专有名词写进「术语读法」即可覆盖品牌名，
  不必为每个词写代码。
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------------------
# 术语读法：把「会被读错的写法」换成「明确好读的写法」
#
# 注意顺序：长词优先，否则 "Chat" 会先把 "ChatTTS" 拆开。
# --------------------------------------------------------------------------------------
DEFAULT_TERM_RULES: tuple[tuple[str, str], ...] = (
    # 本项目的品牌名，中文里直接读字母
    ("EdxSpark", "Edx Spark"),
    ("ChatTTS", "Chat T T S"),
    ("ChatTts", "Chat T T S"),
    ("IndexTTS", "Index T T S"),
    ("GPT-4", "G P T 4"),
    ("GPT-4o", "G P T 4 o"),
    ("GPT", "G P T"),
    ("OpenAI", "Open A I"),
    ("WebUI", "Web U I"),
    ("API", "A P I"),
    ("URL", "U R L"),
)


def parse_term_rules(raw: str) -> list[tuple[str, str]]:
    """解析用户配置的术语读法。

    支持两种写法（每行一条）：
      AI=A I
      AI：A I
    未提供替换值时，默认按「逐字母展开」处理。
    """
    rules: list[tuple[str, str]] = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            key, sep, value = line.partition("：")
        key = key.strip()
        value = value.strip() if sep else ""
        if not key:
            continue
        rules.append((key, value or _spell_out(key)))
    # 长词优先，避免短词先命中
    rules.sort(key=lambda item: len(item[0]), reverse=True)
    return rules


def _spell_out(text: str) -> str:
    """把英文/数字逐字符展开：AI → A I，GPT4 → G P T 4（非字母数字保留）。"""
    return " ".join(ch for ch in text if not ch.isspace())


# --------------------------------------------------------------------------------------
# 英文缩写：中文语境里的连续大写字母（2~5 位）逐字母读
# --------------------------------------------------------------------------------------
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5})(?![A-Za-z0-9])")
# 已经带空格的（A I / P P T）不再处理
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _expand_acronyms(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return " ".join(match.group(1))
    return _ACRONYM_RE.sub(repl, text)


# --------------------------------------------------------------------------------------
# 数字与符号
# --------------------------------------------------------------------------------------
# 小数：把小数点读成「点」，避免 3.5 被念成「三点五」以外的奇怪读法
_DECIMAL_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?!\d)")
# 百分比：15.5% → 百分之15.5（数字保持不变，只把符号变成中文词）
_PERCENT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")
# 常见单位符号 → 中文读法
_UNIT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("℃", "摄氏度"),
    ("°C", "摄氏度"),
    ("km/h", "公里每小时"),
    ("km", "公里"),
    ("kg", "公斤"),
    ("GB", "G B"),
    ("MB", "M B"),
    ("TB", "T B"),
    ("¥", "人民币"),
    ("$", "美元"),
    ("&", "和"),
    ("~", "到"),
    ("×", "乘"),
    ("÷", "除以"),
    ("+", "加"),
    ("=", "等于"),
)
# 中文全角标点规整：冒号在 ChatTTS 里容易被读出来，LongAudio 也是把它换成句号
_PUNCT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("：", "。"),
    ("；", "。"),
    ("——", "，"),
    ("…", "。"),
    ("“", ""),
    ("”", ""),
    ("‘", ""),
    ("’", ""),
    ("(", "，"),
    (")", "，"),
    ("（", "，"),
    ("）", "，"),
    ("【", "，"),
    ("】", "，"),
    ("《", ""),
    ("》", ""),
    ("·", " "),
    ("#", " "),
)
# 连续标点收敛（避免出现「。。」这种会让模型停顿异常的串）
_REPEAT_PUNCT_RE = re.compile(r"([。，！？、])\1+")
# 空白收敛
_SPACE_RE = re.compile(r"[ \t\u3000]+")


def normalize_for_tts(
    text: str,
    *,
    term_rules: list[tuple[str, str]] | None = None,
    expand_acronyms: bool = True,
    normalize_numbers: bool = True,
    normalize_punct: bool = True,
) -> str:
    """把待合成文本规整成 ChatTTS 更容易读对的形式。

    只做「确定是坑」的处理：术语替换、缩写拆字母、百分号/单位符号中文化、
    标点规整与空白收敛。原始数字保留（模型读数字通常没问题，
    强行转成中文反而容易出「两千零二十四」这类怪读法）。
    """
    result = str(text or "")
    if not result.strip():
        return ""

    # 1) 术语替换（长词优先）
    for find, replace in (term_rules if term_rules is not None else DEFAULT_TERM_RULES):
        if find and find in result:
            result = result.replace(find, replace)

    if normalize_numbers:
        # 百分号先处理（"15.5%" → "百分之15.5"），再处理单位，最后处理小数
        result = _PERCENT_RE.sub(lambda m: f"百分之{m.group(1)}", result)
        for symbol, word in _UNIT_REPLACEMENTS:
            if symbol in result:
                result = result.replace(symbol, word)
        # 3.5 → 3点5（小数点读音固定，避免"3.5"被拆成"3"和"5"）
        result = _DECIMAL_RE.sub(lambda m: f"{m.group(1)}点{m.group(2)}", result)

    if expand_acronyms:
        result = _expand_acronyms(result)

    if normalize_punct:
        for symbol, word in _PUNCT_REPLACEMENTS:
            if symbol in result:
                result = result.replace(symbol, word)
        result = _REPEAT_PUNCT_RE.sub(r"\1", result)

    result = _SPACE_RE.sub(" ", result)
    # 去掉行首行尾多余标点与空白
    return result.strip(" ，。、,.")

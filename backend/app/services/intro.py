"""统一开头语：在每支成片的开头插入一句固定文案（配音 + 中文字幕）。

设计要点
--------
1. **放在翻译之后**。开头语是中文文案，不经过翻译；插入点选在译文产出之后，
   正片字幕仍保持原有的英译中结果。
2. **整条时间轴后移**。开头语要先合成配音、拿到真实时长，再把正片字幕整体
   后移同样的位移——否则正片第一句会被开头语盖住，音画也不同步。
3. **可续跑**。位移量记在 `stats["intro"]` 里，同时开头语那条字幕用
   `source_indexes=["intro"]` 打标，因此：
   - 续跑时靠标记识别，不会重复插入；
   - 关掉「显示在字幕里」时，烧录阶段可以把它过滤掉，但配音与位移仍然生效。
"""

from __future__ import annotations

from app.services.subtitles import Cue

INTRO_MARKER = "intro"
"""写在 Cue.source_indexes 里的标记，用于识别开头语字幕。"""

DEFAULT_INTRO_TEXT = "欢迎来到Edx Spark，我们将为您提供高质量的AI创业、工作、创新、学习等资讯！"

# 中文播报语速（字/秒），仅用于「还没合成音频时」的时长预估，
# 真实时长以语音合成返回值为准（Mock 通道也按同一速率估算，两边一致）。
CHARS_PER_SECOND = 4.5


def intro_settings(cfg: dict) -> dict:
    """从配置里取开头语设置（缺项按默认值补齐）。"""
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "text": " ".join(str(cfg.get("text") or "").split()).strip(),
        "gap_seconds": max(0.0, float(cfg.get("gap_seconds") or 0.0)),
        "show_in_subtitle": bool(cfg.get("show_in_subtitle", True)),
    }


def card_mode(cfg: dict) -> str:
    """开头画面的模式：auto（自动生成）/ none（不显示）/ custom（自定义图片）。"""
    mode = str(cfg.get("card_mode") or "auto").strip().lower()
    return mode if mode in {"auto", "none", "custom"} else "auto"


def intro_config_key(cfg: dict) -> str:
    """开头语配置指纹：文案/停顿/开关任一变化，旧配音与旧字幕都要重做。

    标题卡也纳入指纹：换了卡片图或文案后重跑，成片必须重新渲染，
    否则会沿用上一次（旧卡片）的成片。
    """
    settings = intro_settings(cfg)
    card = card_mode(cfg)
    card_detail = (
        f"{cfg.get('card_image', '')}|{cfg.get('card_title', '')}"
        f"|{cfg.get('card_subtitle', '')}|{cfg.get('card_layout', 'hero')}"
    )
    return (
        f"{int(settings['enabled'])}:{settings['text']}"
        f":{settings['gap_seconds']}:{int(settings['show_in_subtitle'])}"
        f":{card}:{card_detail}"
    )


def is_intro_cue(cue: Cue) -> bool:
    return INTRO_MARKER in (cue.source_indexes or [])


def has_intro(cues: list[Cue]) -> bool:
    return any(is_intro_cue(cue) for cue in cues)


def strip_by_offset(cues: list[Cue], offset: float) -> list[Cue]:
    """去掉「上一轮插入的开头语」，并把正片字幕整体前移回去。

    用于配置变更后的重做（换文案 / 关掉开关）。这里按时间轴判断而不是靠标记：
    开头语标记只存在于内存中的 Cue，写进 SRT 再读回来就没了，
    因此持久化的事实是 stats 里记录的位移量 offset。
    """
    if offset <= 0 or not cues:
        return list(cues)
    first = cues[0]
    if first.start > 0.35 or abs(first.end - offset) > 0.35:
        return list(cues)
    return [cue.shifted(-offset) for cue in cues[1:]]


def estimate_duration(text: str) -> float:
    """按中文字数估算播报时长（仅在拿到真实配音时长之前使用）。"""
    return max(0.6, len(text.strip()) / CHARS_PER_SECOND)


def apply_intro(cues: list[Cue], settings: dict, *, duration: float) -> list[Cue]:
    """在字幕最前面插入开头语，并把原字幕整体后移。

    duration 是开头语配音的时长（真实值或预估值）。
    返回新列表，不修改入参。
    """
    offset = max(0.0, float(duration)) + float(settings.get("gap_seconds") or 0.0)
    intro = Cue(
        start=0.0,
        end=offset,
        text=str(settings.get("text") or "").strip(),
        source_indexes=[INTRO_MARKER],
    )
    return [intro, *[cue.shifted(offset) for cue in cues]]


def strip_intro(cues: list[Cue]) -> list[Cue]:
    return [cue for cue in cues if not is_intro_cue(cue)]


def subtitle_cues(cues: list[Cue], settings: dict) -> list[Cue]:
    """烧录字幕用的列表：按配置决定是否保留开头语那一条。"""
    if settings.get("show_in_subtitle", True):
        return list(cues)
    return strip_intro(cues)

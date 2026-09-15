"""业务配置：schema 定义、持久化、密钥加密与脱敏。

设计：
- 按 section（general/translator/tts/download/publish/video）存为一条 JSON 记录。
- 声明为 secret 的字段在落库前用 Fernet 加密，读取时解密。
- 对外接口（GET /api/settings）返回脱敏值；提交时若字段值等于掩码或为空串，则保留原值。
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt, encrypt, mask
from app.models import Setting

# --------------------------------------------------------------------------------------
# 配置模型
# --------------------------------------------------------------------------------------


class TranslatorConfig(BaseModel):
    """英译中翻译（DeepSeek）。"""

    provider: Literal["deepseek", "mock"] = "mock"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = Field(default=1.3, ge=0.0, le=2.0)
    timeout: int = Field(default=120, ge=10, le=600)
    # 每批提交给模型的字幕条数
    batch_size: int = Field(default=20, ge=1, le=100)
    # 上下文条数：把相邻字幕一并送入，提升连贯性
    context_size: int = Field(default=4, ge=0, le=20)
    retry: int = Field(default=3, ge=0, le=10)
    # 翻译风格提示词，直接影响口语化程度
    style_prompt: str = (
        "你是专业的视频本地化翻译。把英文字幕翻译成自然、口语化的简体中文，"
        "符合中文表达习惯，不要逐字直译，不要出现英文残留。"
        "保持每句长度与原句相近，便于配音对轴。只输出译文。"
    )
    # 专有名词对照表：{"OpenAI": "OpenAI", "Python": "Python"}
    glossary: dict[str, str] = Field(default_factory=dict)


class TTSConfig(BaseModel):
    """阿里云智能语音交互（ISI）语音合成。"""

    provider: Literal["aliyun", "mock"] = "mock"
    access_key_id: str = ""
    access_key_secret: str = ""
    # 项目 AppKey（阿里云控制台「创建项目」后获得）
    app_key: str = ""
    # 可选手工 Token；留空则由 AK/SK 自动签发（有效期 24h，自动续期）
    token: str = ""
    region: str = "cn-shanghai"
    voice: str = "xiaoxian"
    format: Literal["mp3", "wav", "pcm"] = "mp3"
    sample_rate: int = 48000
    volume: int = Field(default=50, ge=0, le=100)
    # 语速 / 语调，范围 -500~500
    speech_rate: int = Field(default=0, ge=-500, le=500)
    pitch_rate: int = Field(default=0, ge=-500, le=500)
    # 合成后增益（dB），用于与背景音平衡
    gain_db: float = Field(default=0.0, ge=-20.0, le=20.0)
    # 单条字幕超过该长度时先在标点处切分（阿里云单请求上限 300 字符）
    max_chars_per_request: int = Field(default=280, ge=50, le=300)
    concurrency: int = Field(default=4, ge=1, le=16)


class DownloadConfig(BaseModel):
    """yt-dlp 下载配置。"""

    # 竖屏平台建议 1080p 以内；也可填 bestvideo+bestaudio
    format: str = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    max_height: int = Field(default=1080, ge=240, le=4320)
    subtitle_langs: list[str] = Field(default_factory=lambda: ["en", "en-US", "en-GB", "en-orig"])
    # 优先人工字幕，没有则回退自动字幕
    prefer_manual_subtitle: bool = True
    write_thumbnail: bool = True
    cookies_file: str = ""
    proxy: str = ""
    # JavaScript 运行时（YouTube 提取需要）。留空自动探测：deno > node > bun > quickjs
    js_runtime: str = ""
    retries: int = Field(default=5, ge=0, le=20)
    sleep_interval: float = Field(default=0.0, ge=0.0, le=60.0)
    rate_limit: str = ""

    @field_validator("subtitle_langs", mode="before")
    @classmethod
    def _split_langs(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


class PublishConfig(BaseModel):
    """抖音发布配置。"""

    provider: Literal["douyin", "mock"] = "mock"
    # 可见浏览器：发布时会打开真实浏览器窗口，便于观察与人工处理验证码。
    # 想在后台静默运行可改为 True（无头更容易被平台识别，也看不到中间状态）。
    headless: bool = False
    # 失败时保留浏览器窗口一段时间，供人工查看当前页面状态（无头模式下无效）
    keep_browser_on_failure: bool = True
    failure_hold_seconds: int = Field(default=180, ge=0, le=3600)
    # 无头发布遇到验证码/身份验证时，是否自动改为有头重试一次让人工接手
    headless_fallback_to_visible: bool = True
    # 是否在流水线末尾自动发布。默认关闭：流水线只产出成片与文案，
    # 条目停在「待发布」，由人在任务详情页确认后手动发布；
    # 想全自动发布，需要在「系统配置 → 发布」里显式打开。
    auto_publish: bool = False
    # 立即发布时下拉框文案
    title_max_len: int = Field(default=30, ge=1, le=200)
    default_tags: list[str] = Field(default_factory=lambda: ["视频搬运", "涨知识", "科普"])
    # 定时发布：距当前时间的分钟数；0 表示立即发布
    schedule_offset_minutes: int = Field(default=0, ge=0, le=14 * 24 * 60)
    # 发布后是否自动跳转并回填作品链接
    collect_url: bool = True
    # 是否允许并发发布（同一账号建议串行）
    concurrency: int = Field(default=1, ge=1, le=3)
    # 上传超时（秒）
    timeout: int = Field(default=600, ge=60, le=7200)

    @field_validator("default_tags", mode="before")
    @classmethod
    def _split_tags(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [s.strip().lstrip("#") for s in v.replace("，", ",").split(",") if s.strip()]
        return v


class VideoConfig(BaseModel):
    """成片合成配置。"""

    # 画面：original 保留原比例；9:16 竖屏（上下加模糊背景）；16:9 横屏
    target_aspect: Literal["original", "9:16", "16:9"] = "original"
    # 烧录字幕（抖音更吃字幕）
    burn_subtitles: bool = True
    # 字幕内容：双语（中英两行）/ 仅中文 / 仅英文
    subtitle_mode: Literal["bilingual", "zh", "en"] = "bilingual"
    # 单条字幕的显示上限。行业惯例：最多 2 行、单条停留 1~7 秒（常见 2~4 秒）。
    # 超过上限的长句会被按自然边界切分并按字数比例分配时间。
    # 设得过大会出现「一句话占屏十几秒」，观感就是字幕与话音对不上。
    subtitle_max_duration: float = Field(default=5.0, ge=1.0, le=30.0)
    subtitle_max_chars: int = Field(default=84, ge=20, le=300)
    # 字号与边距都以 1080p 为基准，按成片分辨率等比缩放，
    # 因此这里的数字就是「1080p 画面上的真实像素」，换分辨率观感一致。
    subtitle_font_size: int = Field(default=40, ge=6, le=120)
    # 留空则自动选择系统中真实可用、libass 能加载的中文字体
    subtitle_font_name: str = ""
    subtitle_margin_v: int = Field(default=40, ge=0, le=600)
    subtitle_alignment: Literal["bottom", "middle", "top"] = "bottom"
    subtitle_outline: int = Field(default=1, ge=0, le=6)
    # 封面来源：
    #   generated —— 用视频帧做底图，叠加深色渐变并排版标题与标签（推荐）
    #   frame     —— 直接抽一帧，不做任何设计
    # 封面来源：
    #   thumbnail —— 使用视频原始缩略图（默认。创作者为吸引点击专门设计，通常最好看）
    #   generated —— 程序生成的科技风设计稿
    #   frame     —— 从成片抽一帧
    cover_source: Literal["thumbnail", "generated", "frame"] = "thumbnail"
    cover_mode: Literal["generated", "frame"] = "generated"
    cover_theme: Literal["tech_blue", "tech_dark", "minimal"] = "tech_blue"
    # 封面底图：generated 为程序化生成的科技背景（推荐，不受视频内容影响）；
    # frame 为视频截图——开头是黑/白帧时会得到黑底或白底封面，所以不作默认
    cover_background: Literal["generated", "frame"] = "generated"
    # 封面左上角的品牌标识，留空则不显示
    cover_brand: str = "AI 译制"
    cover_max_tags: int = Field(default=4, ge=0, le=8)
    # 原视频音轨的处理方式：
    #   remove —— 完全丢弃原音轨（人声与背景音乐都不要），成片只有配音
    #   keep   —— 保留原音轨并压低音量，与配音混合
    original_audio: Literal["remove", "keep"] = "remove"
    # 保留原音轨时的音量
    bgm_volume: float = Field(default=0.12, ge=0.0, le=2.0)
    voice_volume: float = Field(default=1.0, ge=0.0, le=4.0)
    # 中文字幕变长导致超时，允许的最大加速比；超出则截断该句
    max_speedup: float = Field(default=1.35, ge=1.0, le=3.0)
    # 视频编码质量
    crf: int = Field(default=20, ge=0, le=51)
    preset: str = "medium"


class GeneralConfig(BaseModel):
    default_voice: str = "xiaoxian"
    max_concurrent_tasks: int = Field(default=1, ge=1, le=8)
    keep_source_files: bool = True
    log_retention_days: int = Field(default=30, ge=1, le=365)
    auto_clean_failed: bool = False


class ASRConfig(BaseModel):
    """语音识别（无字幕视频的兜底）。凭证与「语音合成」共用同一个阿里云项目。"""

    # whisper = 本地模型（默认，准确率最高且自带词级时间戳，无按量费用）
    # aliyun  = 阿里云一句话识别（无需本地模型，但实测英文准确率明显较低）
    # mock    = 离线占位
    provider: Literal["whisper", "aliyun", "mock"] = "whisper"
    # 无字幕时是否自动语音识别；关闭则保留原声不配音
    enabled: bool = True
    # 本地模型档位：越大越准也越慢。英文视频建议带 .en 后缀的档位
    whisper_model: str = "small"
    whisper_device: Literal["cpu", "cuda", "auto"] = "cpu"
    # int8 在 CPU 上最快；有 NVIDIA 显卡可改 float16
    whisper_compute_type: str = "int8"
    # 单条字幕的字数与时长上限（词级时间戳的合并依据）
    whisper_max_cue_chars: int = Field(default=84, ge=20, le=300)
    whisper_max_cue_duration: float = Field(default=5.0, ge=1.0, le=30.0)
    # 识别语言提示（仅用于日志与人为判断，实际语种由阿里云控制台的项目模型决定）
    language: str = "en"
    # 单块上限（秒）。阿里云单次请求音频不超过 60 秒，留出余量
    max_chunk_seconds: int = Field(default=40, ge=5, le=55)
    concurrency: int = Field(default=2, ge=1, le=8)
    # 静音检测：低于该响度视为静音
    silence_threshold_db: int = Field(default=-35, ge=-60, le=-10)
    min_silence_seconds: float = Field(default=0.45, ge=0.1, le=3.0)
    # 去掉「嗯/呃」等填充词
    remove_fillers: bool = True
    # 时长上限（分钟）。0 表示不限制。长视频识别按小时计费，设上限可避免意外开销
    max_duration_minutes: int = Field(default=0, ge=0, le=600)


CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "general": GeneralConfig,
    "translator": TranslatorConfig,
    "tts": TTSConfig,
    "asr": ASRConfig,
    "download": DownloadConfig,
    "publish": PublishConfig,
    "video": VideoConfig,
}

# 需要加密存储的字段
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "translator": ("api_key",),
    "tts": ("access_key_id", "access_key_secret", "app_key", "token"),
}

SECTION_LABELS = {
    "general": "通用",
    "translator": "翻译（DeepSeek）",
    "tts": "语音合成（阿里云 ISI）",
    "asr": "语音识别（无字幕兜底）",
    "download": "下载（yt-dlp）",
    "publish": "发布（抖音）",
    "video": "成片合成",
}

# 前端「恢复默认」用的全量默认值
DEFAULT_CONFIG: dict[str, dict[str, Any]] = {
    name: model().model_dump() for name, model in CONFIG_MODELS.items()
}


def _mask_section(section: str, data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for field in SECRET_FIELDS.get(section, ()):
        raw = out.get(field) or ""
        out[field] = mask(raw) if raw else ""
        out[f"{field}__set"] = bool(raw)
    return out


def _normalize(section: str, data: dict[str, Any]) -> dict[str, Any]:
    model = CONFIG_MODELS[section]
    return model.model_validate({**DEFAULT_CONFIG[section], **(data or {})}).model_dump()


class SettingsStore:
    """带缓存的配置读写。"""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}

    async def _load_section(self, session: AsyncSession, section: str) -> dict[str, Any]:
        row = await session.get(Setting, section)
        raw: dict[str, Any] = {}
        if row and isinstance(row.value, dict):
            raw = dict(row.value)
        for field in SECRET_FIELDS.get(section, ()):
            if field in raw:
                raw[field] = decrypt(str(raw[field] or ""))
        return _normalize(section, raw)

    async def load_all(self, session: AsyncSession, use_cache: bool = False) -> dict[str, dict[str, Any]]:
        if use_cache and self._cache:
            return copy.deepcopy(self._cache)
        result: dict[str, dict[str, Any]] = {}
        for section in CONFIG_MODELS:
            result[section] = await self._load_section(session, section)
        self._cache = copy.deepcopy(result)
        return result

    async def get_section(self, session: AsyncSession, section: str) -> dict[str, Any]:
        if section not in CONFIG_MODELS:
            raise KeyError(f"未知配置分组: {section}")
        return await self._load_section(session, section)

    async def public(self, session: AsyncSession) -> dict[str, Any]:
        """脱敏后的完整配置，用于前端渲染。"""
        full = await self.load_all(session, use_cache=True)
        return {
            section: _mask_section(section, data) for section, data in full.items()
        }

    async def describe(self) -> dict[str, Any]:
        """配置项元信息：分组、标签、默认值、密钥字段，供前端动态渲染表单。"""
        return {
            "sections": [
                {
                    "key": section,
                    "label": SECTION_LABELS[section],
                    "secrets": list(SECRET_FIELDS.get(section, ())),
                    "defaults": DEFAULT_CONFIG[section],
                }
                for section in CONFIG_MODELS
            ]
        }

    async def update(self, session: AsyncSession, patch: dict[str, Any]) -> dict[str, Any]:
        """按分组增量更新。密钥字段传空串或掩码表示「保持不变」。"""
        for section, values in (patch or {}).items():
            if section not in CONFIG_MODELS or not isinstance(values, dict):
                continue
            current = await self._load_section(session, section)
            secrets = SECRET_FIELDS.get(section, ())
            merged: dict[str, Any] = {**current}
            for key, value in values.items():
                if key.endswith("__set") or key not in CONFIG_MODELS[section].model_fields:
                    continue
                if key in secrets:
                    text = "" if value is None else str(value)
                    # 掩码回传 / 空串 => 保留原值
                    if text == "" or (set(text) <= {"*"} or "*" * 4 in text):
                        continue
                merged[key] = value
            normalized = _normalize(section, merged)
            stored = dict(normalized)
            for field in secrets:
                stored[field] = encrypt(str(normalized.get(field) or ""))
            row = await session.get(Setting, section)
            if row is None:
                row = Setting(key=section, value=stored, is_secret=bool(secrets))
                session.add(row)
            else:
                row.value = stored
                row.is_secret = bool(secrets)
        await session.commit()
        self._cache = {}
        return await self.public(session)

    async def reset(self, session: AsyncSession, section: str | None = None) -> dict[str, Any]:
        targets = [section] if section else list(CONFIG_MODELS)
        for name in targets:
            if name not in CONFIG_MODELS:
                continue
            row = await session.get(Setting, name)
            if row is not None:
                await session.delete(row)
        await session.commit()
        self._cache = {}
        return await self.public(session)


settings_store = SettingsStore()

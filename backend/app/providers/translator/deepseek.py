"""DeepSeek 翻译提供者（英文 → 中文）。

要点：
- 用 JSON 输出模式保证「序号 ↔ 译文」严格对齐，避免模型合并/漏句。
- 带上下文：把相邻若干句一起送入，让代词、术语翻译更连贯。
- 对齐校验失败时自动降级为逐条翻译，保证流水线不中断。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app.providers.base import BaseTranslator, ProviderError, TranslateResult
from app.services.settings_store import TranslatorConfig
from app.utils.text import estimate_tokens

logger = logging.getLogger(__name__)

_SYSTEM_TEMPLATE = """{style}

输出要求：
- 输入是带序号的英文字幕，逐条翻译成简体中文。
- 必须返回 JSON 对象，格式为 {{"translations": [{{"i": 序号, "zh": "译文"}}, ...]}}。
- 序号必须与输入一一对应，数量必须完全一致，不得合并、拆分或遗漏。
- 不要输出任何解释、拼音或英文原文。"""


def _extract_json(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if match:
            return json.loads(match.group(0))
        raise


class DeepSeekTranslator(BaseTranslator):
    name = "deepseek"

    def __init__(self, config: TranslatorConfig) -> None:
        self.config = config
        if not config.api_key:
            raise ProviderError("未配置 DeepSeek API Key，请在「系统配置 → 翻译」中填写")

    @property
    def _endpoint(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _build_prompt(self, texts: list[str], hint: str) -> str:
        lines = [f"{i + 1}. {t}" for i, t in enumerate(texts)]
        parts = ["请翻译以下字幕：", "\n".join(lines)]
        if hint:
            parts.append(f"\n上下文（仅供理解，不要翻译）：\n{hint}")
        if self.config.glossary:
            glossary = "；".join(f"{k} → {v}" for k, v in self.config.glossary.items())
            parts.append(f"\n术语对照（必须遵守）：\n{glossary}")
        return "\n".join(parts)

    async def _call(self, texts: list[str], hint: str) -> TranslateResult:
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": _SYSTEM_TEMPLATE.format(style=self.config.style_prompt.strip()),
                },
                {"role": "user", "content": self._build_prompt(texts, hint)},
            ],
            "temperature": self.config.temperature,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(self.config.retry + 1):
            try:
                async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                    response = await client.post(self._endpoint, headers=self._headers, json=payload)
                if response.status_code == 401:
                    raise ProviderError("DeepSeek 鉴权失败（401），请检查 API Key")
                if response.status_code == 429:
                    raise ProviderError("DeepSeek 触发限流（429）")
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = _extract_json(content)

                mapping: dict[int, str] = {}
                for entry in parsed.get("translations", []):
                    try:
                        mapping[int(entry["i"])] = str(entry.get("zh") or "").strip()
                    except (KeyError, TypeError, ValueError):
                        continue
                if len(mapping) != len(texts):
                    raise ValueError(f"译文条数不匹配：期望 {len(texts)}，实际 {len(mapping)}")

                usage = data.get("usage") or {}
                return TranslateResult(
                    texts=[mapping[i + 1] or texts[i] for i in range(len(texts))],
                    usage={
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 需要兜住网络/解析/对齐各类异常
                last_error = exc
                wait = min(2 ** attempt, 8)
                logger.warning("DeepSeek 翻译失败（第 %s 次）：%s，%ss 后重试", attempt + 1, exc, wait)
                if attempt < self.config.retry:
                    await asyncio.sleep(wait)
        raise ProviderError(f"DeepSeek 翻译失败：{last_error}")

    async def translate(self, texts: list[str], *, hint: str = "") -> TranslateResult:
        texts = [t or "" for t in texts]
        if not texts:
            return TranslateResult(texts=[])
        batch_size = max(1, self.config.batch_size)
        out: list[str] = []
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        estimate = 0

        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            context_before = texts[max(0, start - self.config.context_size) : start]
            context_after = texts[start + len(batch) : start + len(batch) + self.config.context_size]
            context = " ".join(context_before + context_after).strip()
            try:
                result = await self._call(batch, context)
            except ProviderError as exc:
                logger.warning("批次翻译失败，降级为逐条翻译：%s", exc)
                result = TranslateResult(texts=[])
                for text in batch:
                    try:
                        single = await self._call([text], context)
                        result.texts.append(single.texts[0])
                        for key, value in single.usage.items():
                            usage_total[key] = usage_total.get(key, 0) + int(value or 0)
                    except ProviderError:
                        # 单条仍失败：保留原文，交由人工/后续处理，不阻断整条流水线
                        result.texts.append(text)
            for key, value in result.usage.items():
                usage_total[key] = usage_total.get(key, 0) + int(value or 0)
            estimate += estimate_tokens(" ".join(batch)) + estimate_tokens(" ".join(result.texts))
            out.extend(result.texts)

        usage_total["estimated_tokens"] = estimate
        return TranslateResult(texts=out, usage=usage_total)

    # -- 发布文案生成 -----------------------------------------------------------

    async def _chat_json(self, system: str, user: str) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": min(1.5, self.config.temperature + 0.2),
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(self._endpoint, headers=self._headers, json=payload)
        response.raise_for_status()
        return _extract_json(response.json()["choices"][0]["message"]["content"])

    async def generate_metadata(
        self,
        *,
        title: str,
        description: str = "",
        translated_body: str = "",
        max_title_len: int = 30,
        fallback_tags: list[str] | None = None,
        tag_limit: int = 5,
    ) -> tuple[str, list[str]]:
        """用小模型生成更有传播力的中文标题与话题标签。"""
        from app.utils.text import extract_tags, truncate

        system = (
            "你是短视频运营专家，负责把海外视频包装成中文短视频的发布文案。"
            f"输出 JSON：{{\"title\": \"中文标题\", \"tags\": [\"话题1\", \"话题2\"]}}。"
            f"title 必须是简体中文，不超过 {max_title_len} 个字符，有吸引力但不夸张失实、不做标题党式虚假承诺。"
            f"tags 为 {tag_limit} 个中文话题词，每个 2-6 个字，不带 # 号，贴合内容与目标受众。"
        )
        user_parts = [f"原标题：{title}"]
        if translated_body:
            user_parts.append(f"中文字幕节选：{translated_body[:400]}")
        elif description:
            user_parts.append(f"原视频简介：{description[:600]}")
        if fallback_tags:
            user_parts.append(f"可参考的默认话题：{'、'.join(fallback_tags)}")

        try:
            data = await self._chat_json(system, "\n".join(user_parts))
        except Exception as exc:  # noqa: BLE001 - 失败时退回规则生成
            logger.warning("标题/话题生成调用失败，退回规则生成：%s", exc)
            return await super().generate_metadata(
                title=title,
                description=description,
                translated_body=translated_body,
                max_title_len=max_title_len,
                fallback_tags=fallback_tags,
                tag_limit=tag_limit,
            )

        title_zh = truncate(str(data.get("title") or "").strip() or title, max_title_len)
        raw_tags = data.get("tags") or []
        tags: list[str] = []
        for tag in raw_tags:
            clean = str(tag).strip().lstrip("#").strip()
            if clean and clean not in tags:
                tags.append(clean)
        if not tags:
            tags = extract_tags(f"{title} {description}", limit=tag_limit) or list(fallback_tags or [])
        return title_zh, tags[:tag_limit]


class MockTranslator(BaseTranslator):
    """离线演示用：不调用任何外部服务，用确定性规则产出「中文」字幕。

    真实译文质量取决于 DeepSeek；Mock 只保证流程可端到端跑通。
    """

    name = "mock"

    _DICT = {
        "hello": "你好",
        "hi": "嗨",
        "welcome": "欢迎",
        "today": "今天",
        "thank you": "谢谢",
        "thanks": "谢谢",
        "video": "视频",
        "subscribe": "订阅",
        "channel": "频道",
        "let's": "让我们",
        "start": "开始",
        "learn": "学习",
        "how": "如何",
        "why": "为什么",
        "what": "什么",
        "money": "金钱",
        "money.": "金钱。",
        "life": "生活",
        "people": "人们",
        "world": "世界",
        "time": "时间",
        "good": "好",
        "great": "很棒",
        "first": "首先",
        "second": "其次",
        "finally": "最后",
        "example": "例子",
        "important": "重要的",
    }

    def __init__(self, config: TranslatorConfig | None = None) -> None:
        self.config = config

    async def translate(self, texts: list[str], *, hint: str = "") -> TranslateResult:
        out: list[str] = []
        for text in texts:
            out.append(self._fake(text))
        # 模拟一点网络延迟，让前端进度条有真实观感
        await asyncio.sleep(min(0.05 * len(texts), 0.6))
        return TranslateResult(
            texts=out,
            usage={"total_tokens": estimate_tokens(" ".join(texts)) * 2, "mock": True},
        )

    def _fake(self, text: str) -> str:
        lowered = text.lower()
        for key, value in sorted(self._DICT.items(), key=lambda kv: -len(kv[0])):
            if key in lowered:
                return f"【示例译文】{value}（原文：{text[:40]}）"
        return f"【示例译文】{text[:60]}"

"""音色库：给「按说话人性别配音」提供带性别标注的可选音色。

两个通道的取法不同：

- **阿里云**：官方发音人自带性别（小仙/若兮…为女声，小刚/艾凯为男声），
  直接读内置清单，不需要探测。
- **ChatTTS**：音色没有标签，只能**实测**——用不同随机种子采样说话人，
  合成一句固定的探测语句，再用基频判定该音色是男是女，结果缓存到数据目录。
  探测一次约 10~40 秒（取决于机器），之后按缓存选音色，几乎零开销。

缓存理由是「音色清单是环境的属性」：换台机器/换模型都可能变，但同一环境内稳定。
探测失败（服务没起）不抛错，返回空清单，由调用方决定回退（例如沿用原音色）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.services import voice_profile

logger = logging.getLogger(__name__)

# 探测用语句：要求浊音多、音节清晰，便于基频判定
PROBE_TEXT = "大家好，今天我们来聊一个很有意思的话题。"
# 默认采样多少个说话人做性别标注
DEFAULT_POOL_SIZE = 10
CACHE_VERSION = 1


@dataclass
class SpeakerInfo:
    """一个已标注性别的音色。"""

    seed: int                      # 说话人种子（即 ChatTTS 的 voice 参数）
    gender: str                    # male / female
    f0: float = 0.0                # 实测基频，便于人工核对
    confidence: float = 0.0


@dataclass
class VoiceCatalog:
    """已标注的音色清单，按性别分组。"""

    provider: str = ""
    speakers: list[SpeakerInfo] = field(default_factory=list)
    source: str = ""               # cache / probed / builtin

    def pick(self, gender: str, seed: int = 0) -> SpeakerInfo | None:
        """按性别挑一个音色；同一 seed 稳定挑到同一个（保证全片音色一致）。"""
        pool = [s for s in self.speakers if s.gender == gender]
        if not pool:
            return None
        pool.sort(key=lambda s: (-s.confidence, s.seed))
        return pool[abs(seed) % len(pool)]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for speaker in self.speakers:
            out[speaker.gender] = out.get(speaker.gender, 0) + 1
        return out


def cache_path(cache_file: Path) -> Path:
    return cache_file


def load_cache(cache_file: Path) -> VoiceCatalog | None:
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("version") != CACHE_VERSION:
        return None
    speakers = [
        SpeakerInfo(
            seed=int(item["seed"]),
            gender=str(item["gender"]),
            f0=float(item.get("f0") or 0.0),
            confidence=float(item.get("confidence") or 0.0),
        )
        for item in data.get("speakers", [])
        if item.get("gender") in {"male", "female"}
    ]
    if not speakers:
        return None
    return VoiceCatalog(provider=data.get("provider", ""), speakers=speakers, source="cache")


def save_cache(cache_file: Path, catalog: VoiceCatalog) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "provider": catalog.provider,
        "probed_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "speakers": [
            {"seed": s.seed, "gender": s.gender, "f0": round(s.f0, 1), "confidence": s.confidence}
            for s in catalog.speakers
        ],
    }
    cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def probe_chattts(
    synth,
    work_dir: Path,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    start_seed: int = 1000,
    threshold: float = voice_profile.DEFAULT_THRESHOLD,
) -> VoiceCatalog:
    """实测 ChatTTS 音色的性别。

    synth 需实现 `synthesize(text, out_path, *, voice=None)`：这里用 voice 参数传种子，
    与 ChatTTS 上游「种子决定音色」的语义一致（本仓库的提供者已如此实现）。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    speakers: list[SpeakerInfo] = []
    for index in range(pool_size):
        seed = start_seed + index * 7  # 间隔取样，避免相邻种子得到相近音色
        out = work_dir / f"probe_{seed}.wav"
        try:
            result = await synth.synthesize(PROBE_TEXT, out, voice=str(seed))
        except Exception as exc:  # noqa: BLE001 - 探测失败不应影响任务
            logger.warning("ChatTTS 音色探测中断（seed=%s）：%s", seed, exc)
            break
        profile = await voice_profile.analyze_file(result.path, threshold=threshold)
        if profile.gender in {"male", "female"}:
            speakers.append(
                SpeakerInfo(seed=seed, gender=profile.gender, f0=profile.f0, confidence=profile.confidence)
            )
        else:
            logger.info("音色 seed=%s 性别不确定（f0=%.1f，%s）", seed, profile.f0, profile.reason)
        out.unlink(missing_ok=True)

    catalog = VoiceCatalog(provider="chattts", speakers=speakers, source="probed")
    logger.info("ChatTTS 音色探测完成：%s（共试 %s 个种子）", catalog.counts(), pool_size)
    return catalog

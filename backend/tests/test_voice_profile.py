"""说话人性别判定与音色选择的测试。

这套逻辑的失效模式很隐蔽：判错性别不会报错，只会「配出不符的声音」。
因此这里既测真值已知的合成浊音，也测容易误判的干扰信号（噪声/静音）。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services import voice_catalog, voice_profile


@pytest.mark.parametrize(
    ("f0", "expected"),
    [
        (95, "male"),
        (120, "male"),
        (150, "male"),
        (190, "female"),
        (220, "female"),
        (240, "female"),
    ],
)
async def test_tone_gender_detection(tmp_path, f0, expected):
    path = voice_profile.write_wav_mono(tmp_path / f"{f0}.wav", voice_profile.make_tone(f0, 3.0), 16000)
    profile = await voice_profile.analyze_file(path)
    assert profile.gender == expected, f"{f0}Hz 被判成 {profile.gender}（f0={profile.f0:.1f}）"
    assert profile.f0 == pytest.approx(f0, rel=0.05)


async def test_ambiguous_pitch_is_unknown(tmp_path):
    """落在男女重叠区（切分点附近）时宁可不判，避免配错性别。"""
    path = voice_profile.write_wav_mono(
        tmp_path / "mid.wav", voice_profile.make_tone(voice_profile.DEFAULT_THRESHOLD, 3.0), 16000
    )
    profile = await voice_profile.analyze_file(path)
    assert profile.gender == "unknown"
    assert profile.reason


async def test_noise_is_not_male(tmp_path):
    """白噪/粉噪的自相关峰值低，不能当成低音男声。"""
    rng = np.random.default_rng(7)
    white = (rng.random(16000 * 3).astype(np.float32) - 0.5) * 0.4
    path = voice_profile.write_wav_mono(tmp_path / "white.wav", white, 16000)
    profile = await voice_profile.analyze_file(path)
    assert profile.gender == "unknown"
    assert profile.reason


async def test_silence_is_unknown(tmp_path):
    path = voice_profile.write_wav_mono(tmp_path / "silence.wav", np.zeros(16000 * 2, dtype=np.float32), 16000)
    profile = await voice_profile.analyze_file(path)
    assert profile.gender == "unknown"
    assert profile.voiced_ratio == 0.0


def test_stats_expose_evidence():
    """判定结果要带依据，方便用户核对「为什么配了这个声音」。"""
    profile = voice_profile.classify(110.0, 0.5, periodicity=1.2, f0_iqr=10.0)
    stats = profile.as_stats()
    assert stats["gender"] == "male"
    assert stats["f0_hz"] == 110.0
    assert stats["periodicity"] == 1.2


class TestCatalog:
    """音色库的选择逻辑：必须按性别分组，且同一 seed 稳定命中同一个音色。"""

    def _catalog(self):
        return voice_catalog.VoiceCatalog(
            provider="chattts",
            speakers=[
                voice_catalog.SpeakerInfo(seed=1000, gender="male", f0=110, confidence=0.96),
                voice_catalog.SpeakerInfo(seed=1007, gender="female", f0=192, confidence=0.72),
                voice_catalog.SpeakerInfo(seed=1042, gender="male", f0=107, confidence=0.99),
                voice_catalog.SpeakerInfo(seed=1035, gender="female", f0=240, confidence=1.0),
            ],
        )

    def test_pick_by_gender(self):
        catalog = self._catalog()
        assert catalog.pick("male", seed=0).gender == "male"
        assert catalog.pick("female", seed=0).gender == "female"

    def test_pick_is_stable_for_same_seed(self):
        catalog = self._catalog()
        first = catalog.pick("female", seed=2222)
        assert all(catalog.pick("female", seed=2222).seed == first.seed for _ in range(5))

    def test_pick_prefers_confident_speaker(self):
        catalog = self._catalog()
        # seed=0 时按置信度降序取第一个
        assert catalog.pick("female", seed=0).seed == 1035
        assert catalog.pick("male", seed=0).seed == 1042

    def test_missing_gender_returns_none(self):
        catalog = voice_catalog.VoiceCatalog(speakers=[voice_catalog.SpeakerInfo(seed=1, gender="male")])
        assert catalog.pick("female") is None

    def test_cache_roundtrip(self, tmp_path):
        cache = tmp_path / "catalog.json"
        voice_catalog.save_cache(cache, self._catalog())
        loaded = voice_catalog.load_cache(cache)
        assert loaded is not None
        assert loaded.counts() == {"male": 2, "female": 2}
        assert loaded.pick("female", seed=0).f0 == 240.0

    def test_cache_ignores_broken_or_old_file(self, tmp_path):
        cache = tmp_path / "catalog.json"
        cache.write_text("{ not json", encoding="utf-8")
        assert voice_catalog.load_cache(cache) is None
        cache.write_text('{"version": 0, "speakers": [{"seed": 1, "gender": "male"}]}', encoding="utf-8")
        assert voice_catalog.load_cache(cache) is None

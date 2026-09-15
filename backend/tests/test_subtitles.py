"""字幕解析、清洗与断句的单元测试。"""

from __future__ import annotations

import json

from app.services.subtitles import (
    Cue,
    format_timestamp,
    merge_into_sentences,
    normalize_cues,
    parse_json3,
    parse_srt,
    parse_subtitle_file,
    parse_timestamp,
    parse_vtt,
    reindex,
    to_srt,
)


def test_parse_timestamp_formats():
    assert parse_timestamp("00:00:01,500") == 1.5
    assert parse_timestamp("00:01:02.250") == 62.25
    assert parse_timestamp("01:02:03,004") == 3723.004
    assert format_timestamp(3723.004) == "01:02:03,004"
    assert format_timestamp(0) == "00:00:00,000"


def test_format_timestamp_rounds_millis():
    # 1.9996 秒四舍五入到 2.000，不能出现 00:00:01,1000
    assert format_timestamp(1.9996) == "00:00:02,000"


def test_parse_srt_basic():
    content = """1
00:00:01,000 --> 00:00:03,000
Hello world

2
00:00:03,500 --> 00:00:05,000
Second line
"""
    cues = parse_srt(content)
    assert len(cues) == 2
    assert cues[0].text == "Hello world"
    assert cues[0].start == 1.0
    assert cues[1].text == "Second line"


def test_parse_srt_strips_tags_and_dedupes_rolling_lines():
    content = """1
00:00:01,000 --> 00:00:03,000
<c.colorE5E5E5>Hello world</c>

2
00:00:03,000 --> 00:00:05,000
Hello world
this is rolling
"""
    cues = parse_srt(content)
    assert cues[0].text == "Hello world"
    # 第二块里的重复行 "Hello world" 被去掉，只保留新内容
    assert cues[1].text == "this is rolling"


def test_parse_vtt_with_cue_settings():
    content = """WEBVTT

00:00:01.000 --> 00:00:03.000 align:start position:10%
Hello from vtt
"""
    cues = parse_vtt(content)
    assert len(cues) == 1
    assert cues[0].end == 3.0
    assert cues[0].text == "Hello from vtt"


def test_parse_json3():
    payload = {
        "events": [
            {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
            {"tStartMs": 3000, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 4000, "dDurationMs": 1500, "segs": [{"utf8": "Second"}]},
        ]
    }
    cues = parse_json3(json.dumps(payload))
    assert len(cues) == 2
    assert cues[0].text == "Hello world"
    assert cues[0].start == 1.0 and cues[0].end == 3.0


def test_normalize_fixes_overlap_and_empty():
    cues = [
        Cue(start=1.0, end=4.0, text="  first  "),
        Cue(start=3.0, end=5.0, text="second"),
        Cue(start=5.5, end=6.0, text="   "),
    ]
    cleaned = normalize_cues(cues)
    assert len(cleaned) == 2
    assert cleaned[0].text == "first"
    # 重叠被切开，时间轴单调不重叠
    assert cleaned[0].end <= cleaned[1].start


def test_merge_into_sentences_joins_fragments():
    cues = [
        Cue(start=0.0, end=1.0, text="Today we are"),
        Cue(start=1.0, end=2.0, text="going to learn"),
        Cue(start=2.0, end=3.0, text="something useful."),
        Cue(start=3.2, end=4.0, text="This is a new sentence."),
    ]
    merged = merge_into_sentences(cues)
    assert len(merged) == 2
    assert merged[0].text == "Today we are going to learn something useful."
    assert merged[0].start == 0.0 and merged[0].end == 3.0
    assert merged[1].text == "This is a new sentence."


def test_merge_into_sentences_respects_max_chars():
    cues = [Cue(start=i, end=i + 0.9, text=f"word{i} no punctuation here") for i in range(10)]
    merged = merge_into_sentences(cues, max_chars=40)
    assert len(merged) > 1
    assert all(len(cue.text) <= 60 for cue in merged)


def test_to_srt_roundtrip():
    cues = reindex([Cue(start=0.5, end=2.5, text="你好"), Cue(start=3.0, end=4.0, text="世界")])
    text = to_srt(cues)
    parsed = parse_srt(text)
    assert [c.text for c in parsed] == ["你好", "世界"]
    assert parsed[1].start == 3.0


def test_parse_subtitle_file_dispatch(tmp_path):
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")
    assert len(parse_subtitle_file(srt)) == 1

    vtt = tmp_path / "b.vtt"
    vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n", encoding="utf-8")
    assert len(parse_subtitle_file(vtt)) == 1

    json3 = tmp_path / "c.json3"
    json3.write_text(
        json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hi"}]}]}),
        encoding="utf-8",
    )
    assert len(parse_subtitle_file(json3)) == 1

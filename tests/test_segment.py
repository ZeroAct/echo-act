"""F-81 segmentation and the F-27 invariant every later layer stands on.

The single most important test in this file is
``test_segment_ranges_tile_the_source``: if segment ranges ever stop tiling
the source text, the highlight, the segment time table, and every external
projection of a job are wrong together, and no interface can hide it (A.2).
"""

from __future__ import annotations

import random

import pytest

from echoact import paths
from echoact.domain import Gender, Language, SpeakingStyle, VoiceSettings
from echoact.policy import (
    FIRST_SEGMENT_MAX_SECONDS,
    PARAGRAPH_EXTRA_GAP_MS,
    SEGMENT_MAX_CODEPOINTS,
    SEGMENT_MAX_SECONDS,
    SEGMENT_MIN_CODEPOINTS,
    STYLE_SEGMENT_GAP_MS,
    TEMPO_MAX,
)
from echoact.text.language import EN, KO
from echoact.text.normalize import grapheme_boundaries
from echoact.text.segment import (
    effective_tempo,
    ends_paragraph,
    estimate_seconds,
    segment_text,
    split_sentences,
    trailing_silence_ms,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """No test may read or write the real user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    yield
    paths.data_dir.cache_clear()


def settings(
    language: Language = Language.AUTO,
    style: SpeakingStyle = SpeakingStyle.NATURAL,
    tempo: float = 1.0,
) -> VoiceSettings:
    return VoiceSettings(
        model_id="supertonic3",
        language=language,
        gender=Gender.FEMALE,
        voice_id="F1",
        style=style,
        tempo=tempo,
    )


def boundaries(text: str) -> list[tuple[int, int]]:
    return [(s.source.start, s.source.end) for s in segment_text(text, settings())]


# ======================================================================
# Sentence boundaries
# ======================================================================


def test_sentences_split_on_the_three_terminators():
    text = "One. Two! Three?"
    assert [r.slice(text) for r in split_sentences(text)] == ["One. ", "Two! ", "Three?"]


def test_fullwidth_terminators_split_too():
    text = "안녕하세요。반갑습니다！그렇죠？"
    assert len(split_sentences(text)) == 3


def test_a_decimal_point_does_not_end_a_sentence():
    text = "The value is 3.14 exactly."
    assert [r.slice(text) for r in split_sentences(text)] == [text]


@pytest.mark.parametrize(
    "text",
    ["Dr. Kim arrived.", "Mr. and Mrs. Park left.", "Fruit, e.g. apples, is fine.",
     "That is, i.e. this one.", "Us vs. them today.", "Meet at 9 a.m. sharp.",
     "J. R. R. Tolkien wrote it.", "Visit www.example.com now."],
)
def test_an_abbreviation_does_not_end_a_sentence(text):
    assert [r.slice(text) for r in split_sentences(text)] == [text]


def test_etc_ends_a_sentence_only_when_the_next_one_starts():
    joined = "Apples, pears, etc. are fruit."
    assert [r.slice(joined) for r in split_sentences(joined)] == [joined]
    split = "Apples, pears, etc. They are fruit."
    assert [r.slice(split) for r in split_sentences(split)] == [
        "Apples, pears, etc. ",
        "They are fruit.",
    ]


def test_an_ellipsis_ends_a_sentence():
    text = "He paused... Then spoke."
    assert [r.slice(text) for r in split_sentences(text)] == ["He paused... ", "Then spoke."]


def test_a_closing_quote_or_bracket_stays_with_the_sentence_it_closes():
    text = '"Stop!" she said. (Then she left.) Next.'
    assert [r.slice(text) for r in split_sentences(text)] == [
        '"Stop!" ',
        "she said. ",
        "(Then she left.) ",
        "Next.",
    ]


def test_a_korean_sentence_ending_splits_even_without_a_following_space():
    text = "먼저 왔습니다.다음에 갑니다."
    assert [r.slice(text) for r in split_sentences(text)] == ["먼저 왔습니다.", "다음에 갑니다."]


def test_a_line_without_a_terminator_is_still_a_sentence():
    text = "제목\n본문입니다."
    assert [r.slice(text) for r in split_sentences(text)] == ["제목\n", "본문입니다."]


def test_trailing_whitespace_belongs_to_the_sentence_it_follows():
    text = "One.   Two."
    first = split_sentences(text)[0]
    assert first.slice(text) == "One.   "


def test_sentence_ranges_tile_the_text():
    text = "One. Two!\r\n\r\nThree? 네.\n"
    cursor = 0
    for span in split_sentences(text):
        assert span.start == cursor
        cursor = span.end
    assert cursor == len(text)


# ======================================================================
# Clause splitting
# ======================================================================


def test_a_long_english_sentence_splits_at_a_clause_boundary():
    text = (
        "The quick brown fox jumps over the lazy dog every single morning, "
        "and the dog barks loudly at the fox every single evening, "
        "but the fox always runs away before anything at all happens."
    )
    segs = segment_text(text, settings())
    assert len(segs) > 1
    for segment in segs[:-1]:
        tail = text[segment.source.start : segment.source.end].rstrip()
        assert tail.endswith((",", ";", ":")) or text[segment.source.end :].split(" ")[0] in (
            "and",
            "but",
            "or",
            "so",
        )


def test_a_long_korean_sentence_splits_at_a_clause_boundary():
    text = (
        "오늘은 날씨가 아주 좋아서 공원에 갔고, 사람들이 많았지만 조용했으며, "
        "나무 아래에서 책을 읽었습니다."
    )
    segs = segment_text(text, settings())
    assert len(segs) > 1
    assert all(s.source.length >= SEGMENT_MIN_CODEPOINTS for s in segs)


def test_no_segment_exceeds_the_code_point_cap_when_a_boundary_exists():
    sentence = "clause number one, " * 40 + "and the end."
    for segment in segment_text(sentence, settings()):
        assert segment.source.length <= SEGMENT_MAX_CODEPOINTS


def test_no_segment_exceeds_the_estimated_second_cap_when_a_boundary_exists():
    sentence = "clause number one, " * 40 + "and the end."
    tempo = effective_tempo(settings())
    for index, segment in enumerate(segment_text(sentence, settings())):
        cap = FIRST_SEGMENT_MAX_SECONDS if index == 0 else SEGMENT_MAX_SECONDS
        assert estimate_seconds(segment.spoken_text, segment.language, tempo) <= cap


def test_the_first_segment_is_capped_so_playback_starts_sooner():
    sentence = (
        "오늘은 날씨가 아주 좋아서 공원에 갔고, 사람들이 많았지만 조용했으며, "
        "나무 아래에서 책을 읽었습니다."
    )
    tempo = effective_tempo(settings())
    segs = segment_text(f"{sentence} {sentence}", settings())
    seconds = [estimate_seconds(s.spoken_text, s.language, tempo) for s in segs]

    assert seconds[0] <= FIRST_SEGMENT_MAX_SECONDS
    # The same sentence later in the job is left whole: the eight-second cap
    # buys a shorter wait before playback starts, and costs a split, so it is
    # spent once.
    assert seconds[-1] > FIRST_SEGMENT_MAX_SECONDS
    assert seconds[-1] <= SEGMENT_MAX_SECONDS
    assert len(segs) == 3


def test_a_short_sentence_is_never_split():
    text = "짧다."
    assert len(segment_text(text, settings())) == 1


def test_a_split_never_produces_a_segment_shorter_than_the_minimum():
    text = "네. " + "긴 문장이 여기에 있습니다, 그리고 또 다른 절이 있습니다. " * 5
    sentences = {(span.start, span.end) for span in split_sentences(text)}
    short = 0
    for segment in segment_text(text, settings()):
        span = (segment.source.start, segment.source.end)
        if segment.source.length < SEGMENT_MIN_CODEPOINTS:
            short += 1
            assert span in sentences  # the source sentence was itself shorter
    assert short == 1  # "네. ", and nothing the splitter produced


def test_a_word_that_cannot_be_split_stays_whole_and_over_long():
    # F-81 forbids cutting inside a word; latency loses that argument.
    text = "가" * 400
    segs = segment_text(text, settings())
    assert len(segs) == 1
    assert segs[0].source.length == 400


def test_a_cut_never_lands_inside_a_grapheme_cluster():
    text = ("사진 \U0001F468‍\U0001F469‍\U0001F467 그리고 설명이 이어집니다, "
            "그리고 두 번째 절이 있습니다. ") * 6
    legal = set(grapheme_boundaries(text))
    for segment in segment_text(text, settings()):
        assert segment.source.start in legal
        assert segment.source.end in legal


def test_a_number_is_never_split_across_segments():
    text = ("숫자 1,234,567원이 여기 있고, 그리고 또 다른 절이 이어집니다, "
            "그리고 세 번째 절도 있습니다. ") * 4
    number = "1,234,567"
    spans = [(s.source.start, s.source.end) for s in segment_text(text, settings())]
    at = text.find(number)
    seen = 0
    while at != -1:
        seen += 1
        assert any(start <= at and at + len(number) <= end for start, end in spans)
        at = text.find(number, at + 1)
    assert seen == 4


# ======================================================================
# The tiling invariant (F-27)
# ======================================================================

PROPERTY_INPUTS = [
    "",
    " ",
    "\n",
    "\r\n\r\n",
    "Hello.",
    "안녕하세요.",
    "Mixed 한글 and Latin in one sentence, repeated twice.",
    "같은 문장. 같은 문장. 같은 문장.",
    "Same sentence. Same sentence. Same sentence.",
    "\U0001F389\U0001F389\U0001F389",
    "\U0001F389\n\U0001F600\n\U0001F44D",
    "라인 하나\r\n라인 둘\r\n\r\n라인 셋",
    "line one\nline two\n\nline three",
    "   \t　   ",
    "숫자 1,234와 날짜 2026년 9월 8일, 그리고 3.14.",
    "Dr. Kim paid $12.50 at 9 a.m. on September 8, 2026.",
    "44.1kHz, 16 GB, 10-20, 12.5%, 1,234원",
    "한 문장" + "\U0001F44D\U0001F3FD" * 5 + "끝.",
    "\U0001F1F0\U0001F1F7 \U0001F1FA\U0001F1F8 flags and é combining marks.",
    "가" * 400,
    "clause one, clause two, clause three, " * 30,
    "문장 하나입니다. " * 60,
]


def _random_documents(count: int = 40) -> list[str]:
    """Deterministic pseudo-random documents over the F-27 edge cases."""
    pool = [
        "안녕하세요", " ", "  ", "\n", "\r\n", "\n\n", ".", "!", "?", "…",
        "Hello", "world", "1,234", "3.14", "2026-09-08", "$12.50", "12.5%",
        "\U0001F389", "\U0001F468‍\U0001F469‍\U0001F467", "\U0001F1F0\U0001F1F7",
        "Dr.", "e.g.", "3개", "그리고", ", ", "; ", "　", "é", "한",
    ]
    rng = random.Random(20260908)
    return ["".join(rng.choice(pool) for _ in range(rng.randint(0, 120))) for _ in range(count)]


ALL_INPUTS = PROPERTY_INPUTS + _random_documents()


@pytest.mark.parametrize("text", ALL_INPUTS)
def test_segment_ranges_tile_the_source(text):
    cursor = 0
    for segment in segment_text(text, settings()):
        assert segment.source.start == cursor, text
        assert segment.source.end > segment.source.start
        cursor = segment.source.end
    assert cursor == len(text)


@pytest.mark.parametrize("text", ALL_INPUTS)
def test_no_character_of_the_source_is_dropped_or_duplicated(text):
    rebuilt = "".join(
        text[s.source.start : s.source.end] for s in segment_text(text, settings())
    )
    assert rebuilt == text


@pytest.mark.parametrize("text", ALL_INPUTS)
def test_segment_indices_are_dense_and_ordered(text):
    segs = segment_text(text, settings())
    assert [s.index for s in segs] == list(range(len(segs)))


@pytest.mark.parametrize("text", ALL_INPUTS)
def test_segmentation_is_deterministic(text):
    first = segment_text(text, settings())
    second = segment_text(text, settings())
    assert [(s.source.start, s.source.end, s.spoken_text, s.language) for s in first] == [
        (s.source.start, s.source.end, s.spoken_text, s.language) for s in second
    ]


def test_a_fifty_thousand_character_document_tiles_and_is_segmented_once():
    paragraph = (
        "오늘은 날씨가 좋아서 공원에 갔고, 사람들이 많았지만 조용했습니다. "
        "The meeting is at 9 a.m. on September 8, 2026, and it costs $12.50. "
        "숫자 1,234와 3.14, 그리고 \U0001F389 이모지.\n\n"
    )
    text = (paragraph * (50_000 // len(paragraph) + 1))[:50_000]
    segs = segment_text(text, settings())
    cursor = 0
    for segment in segs:
        assert segment.source.start == cursor
        cursor = segment.source.end
    assert cursor == len(text) == 50_000
    assert segs == segment_text(text, settings())


# ======================================================================
# Unspoken spans (F-27, A.3)
# ======================================================================


def test_an_emoji_only_line_attaches_to_the_preceding_segment():
    text = "첫 문장입니다.\n\U0001F389\U0001F389\n마지막 문장입니다."
    segs = segment_text(text, settings())
    assert all(s.is_spoken for s in segs)
    assert "\U0001F389" in text[segs[0].source.start : segs[0].source.end]


def test_a_document_with_nothing_to_say_still_produces_one_segment():
    text = "\U0001F389 \U0001F600\n"
    segs = segment_text(text, settings())
    assert len(segs) == 1
    assert segs[0].spoken_text == ""
    assert (segs[0].source.start, segs[0].source.end) == (0, len(text))


def test_leading_whitespace_attaches_forward_when_there_is_no_previous_segment():
    text = "\n\n   Hello there."
    segs = segment_text(text, settings())
    assert len(segs) == 1
    assert segs[0].source.start == 0
    assert segs[0].spoken_text == "Hello there."


def test_an_emoji_inside_a_sentence_is_not_read_but_stays_in_the_range():
    text = "축하합니다 \U0001F389 오늘."
    segment = segment_text(text, settings())[0]
    assert "\U0001F389" not in segment.spoken_text
    assert segment.source.slice(text) == text


# ======================================================================
# Language, silence, and settings
# ======================================================================


def test_each_segment_carries_the_language_its_sentence_resolved_to():
    text = "안녕하세요. Hello there. 다시 한국어."
    assert [s.language for s in segment_text(text, settings())] == [KO, EN, KO]


def test_an_explicit_language_applies_to_every_segment():
    text = "안녕하세요. Hello there."
    segs = segment_text(text, settings(language=Language.EN))
    assert [s.language for s in segs] == [EN, EN]


@pytest.mark.parametrize("style", list(SpeakingStyle))
def test_the_gap_after_a_segment_is_the_style_preset(style):
    text = "One sentence. Two sentence."
    segs = segment_text(text, settings(style=style))
    assert segs[0].trailing_silence_ms == STYLE_SEGMENT_GAP_MS[style.value]


def test_a_paragraph_break_adds_the_extra_gap_to_the_preceding_segment():
    text = "First paragraph.\n\nSecond paragraph."
    segs = segment_text(text, settings())
    assert segs[0].trailing_silence_ms == (
        STYLE_SEGMENT_GAP_MS[SpeakingStyle.NATURAL.value] + PARAGRAPH_EXTRA_GAP_MS
    )


def test_the_last_segment_has_no_trailing_silence():
    text = "First.\n\nSecond.\n\n"
    assert segment_text(text, settings())[-1].trailing_silence_ms == 0


def test_ends_paragraph_needs_a_blank_line_not_merely_a_line_break():
    assert not ends_paragraph("one line\n")
    assert ends_paragraph("one line\n\n")
    assert ends_paragraph("one line\r\n\r\n")


def test_the_style_tempo_multiplier_is_clamped_into_the_allowed_range():
    assert effective_tempo(settings(style=SpeakingStyle.BRIGHT, tempo=TEMPO_MAX)) == TEMPO_MAX
    assert effective_tempo(settings(style=SpeakingStyle.NATURAL, tempo=1.0)) == 1.0
    assert effective_tempo(settings(style=SpeakingStyle.CALM, tempo=1.0)) == pytest.approx(0.92)


def test_a_faster_tempo_lets_more_text_fit_in_one_segment():
    text = "긴 문장이 계속 이어지고, 또 다른 절이 이어지고, 세 번째 절도 이어집니다. " * 3
    slow = segment_text(text, settings(tempo=0.7))
    fast = segment_text(text, settings(tempo=1.5))
    assert len(fast) < len(slow)


def test_the_estimate_uses_the_measured_rate_for_each_language():
    assert estimate_seconds("가" * 60, KO, 1.0) == pytest.approx(10.0)
    assert estimate_seconds("a" * 133, EN, 1.0) == pytest.approx(10.0)
    assert estimate_seconds("가" * 60, KO, 2.0) == pytest.approx(5.0)


def test_trailing_silence_is_the_apps_own_and_belongs_to_the_preceding_segment():
    # F-82 and A.5: the engine's silence parameter is inert once we chunk.
    plain = trailing_silence_ms(SpeakingStyle.CALM, paragraph_break=False)
    para = trailing_silence_ms(SpeakingStyle.CALM, paragraph_break=True)
    assert plain == STYLE_SEGMENT_GAP_MS["calm"]
    assert para == plain + PARAGRAPH_EXTRA_GAP_MS


def test_spoken_text_is_the_normalised_reading_of_the_segments_own_range():
    text = "가격은 1,234원입니다."
    segment = segment_text(text, settings())[0]
    assert segment.spoken_text == "가격은 천이백삼십사원입니다."
    assert segment.source.slice(text) == text


def test_an_empty_document_has_no_segments():
    assert segment_text("", settings()) == []

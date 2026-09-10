"""F-27: normalisation that can always point back at what the user typed.

Two kinds of test live here.  The alignment tests are about the invariant --
pieces tile the source, and every produced offset maps back inside the span
that produced it.  The rule tests are about readings, one per rule, including
the readings we deliberately refuse to invent.
"""

from __future__ import annotations

import pytest

from echoact import paths
from echoact.domain import TextRange
from echoact.text.language import EN, KO
from echoact.text.normalize import (
    Normalized,
    Piece,
    english_cardinal,
    english_ordinal,
    english_year,
    grapheme_boundaries,
    grapheme_clusters,
    is_decorative,
    is_grapheme_boundary,
    korean_native,
    korean_sino,
    normalize,
    spoken_number,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """No test may read or write the real user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    yield
    paths.data_dir.cache_clear()


SAMPLES = [
    ("", EN),
    ("Hello world.", EN),
    ("안녕하세요. 반갑습니다.", KO),
    ("가격은 1,234원이고 할인율은 12.5%입니다.", KO),
    ("Dr. Kim paid $12.50 on September 8, 2026.", EN),
    ("44.1kHz, 16 GB, 3.14, 10-20", EN),
    ("2026년 9월 8일 오후 3시 30분", KO),
    ("emoji \U0001F389\U0001F600 mixed 한글 and Latin", KO),
    ("line one\r\nline two\n\nline three", EN),
    ("   　\t  ", KO),
    ("\U0001F1F0\U0001F1F7\U0001F468‍\U0001F469‍\U0001F467", EN),
    ("v1.2.3 abc123 e.g. i.e. vs. etc.", EN),
]


# ======================================================================
# The alignment invariant
# ======================================================================


@pytest.mark.parametrize(("text", "lang"), SAMPLES)
def test_pieces_tile_the_source_with_no_gaps_and_no_overlaps(text, lang):
    result = normalize(text, lang)
    cursor = 0
    for piece in result.pieces:
        assert piece.source.start == cursor
        cursor = piece.source.end
    assert cursor == len(text)


@pytest.mark.parametrize(("text", "lang"), SAMPLES)
def test_the_pieces_concatenate_to_the_normalised_text(text, lang):
    result = normalize(text, lang)
    assert "".join(piece.text for piece in result.pieces) == result.text


@pytest.mark.parametrize(("text", "lang"), SAMPLES)
def test_every_produced_offset_maps_inside_the_span_that_produced_it(text, lang):
    result = normalize(text, lang)
    produced = 0
    for piece in result.pieces:
        for step in range(len(piece.text)):
            mapped = result.source_offset(produced + step)
            assert piece.source.start <= mapped <= max(piece.source.start, piece.source.end - 1)
        produced += len(piece.text)


@pytest.mark.parametrize(("text", "lang"), SAMPLES)
def test_every_source_offset_maps_to_a_valid_produced_offset(text, lang):
    result = normalize(text, lang)
    for offset in range(len(text) + 1):
        assert 0 <= result.produced_offset(offset) <= len(result.text)


def test_offsets_round_trip_exactly_where_nothing_was_rewritten():
    result = normalize("Plain unremarkable prose.", EN)
    assert result.text == "Plain unremarkable prose."
    for offset in range(len(result.source) + 1):
        assert result.source_offset(result.produced_offset(offset)) == offset


def test_a_range_over_an_expansion_points_at_the_characters_the_user_typed():
    source = "가격은 1,234원입니다."
    result = normalize(source, KO)
    start = result.text.index("천")
    end = start + len("천이백삼십사원")
    mapped = result.source_range_of(TextRange(start, end))
    assert mapped.slice(source) == "1,234원"


def test_spoken_between_returns_the_reading_of_a_source_range():
    source = "값은 1,234원."
    result = normalize(source, KO)
    assert result.spoken_between(0, 2) == "값은"
    assert "천이백삼십사원" in result.spoken_between(3, len(source))


def test_boundaries_are_offered_only_between_pieces():
    result = normalize("a 1,234 b", EN)
    assert result.boundaries[0] == 0
    assert result.boundaries[-1] == len(result.source)
    starts = {piece.source.start for piece in result.pieces}
    assert set(result.boundaries) == starts | {len(result.source)}


def test_a_gap_in_the_alignment_is_rejected_at_construction():
    with pytest.raises(AssertionError):
        Normalized(source="abcd", text="ac", pieces=(Piece(TextRange(0, 1), "a"),))


def test_an_overlap_in_the_alignment_is_rejected_at_construction():
    pieces = (Piece(TextRange(0, 2), "ab"), Piece(TextRange(1, 3), "bc"))
    with pytest.raises(AssertionError):
        Normalized(source="abc", text="abbc", pieces=pieces)


def test_pieces_whose_lengths_do_not_add_up_to_the_text_are_rejected():
    with pytest.raises(AssertionError):
        Normalized(source="ab", text="abc", pieces=(Piece(TextRange(0, 2), "ab"),))


# ======================================================================
# Spans that yield no audio (F-27, A.3)
# ======================================================================


def test_an_emoji_produces_an_empty_piece_whose_source_range_survives():
    result = normalize("\U0001F389", EN)
    assert result.text == ""
    assert [(p.source.start, p.source.end, p.text) for p in result.pieces] == [(0, 1, "")]


def test_a_zwj_family_emoji_is_suppressed_as_one_piece():
    family = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"
    result = normalize(f"가족 {family} 사진", KO)
    assert family not in result.text
    assert "가족" in result.text and "사진" in result.text
    assert sum(piece.source.length for piece in result.pieces) == len(result.source)


def test_an_emoji_between_two_words_leaves_a_separator_rather_than_joining_them():
    assert normalize("hello\U0001F389world", EN).text == "hello world"


def test_decorative_symbols_are_not_sent_for_synthesis():
    for symbol in "•✈♥→®":
        assert is_decorative(symbol)
        assert normalize(f" {symbol} ", EN).text.strip() == ""


def test_a_currency_sign_is_not_treated_as_decorative():
    assert not is_decorative("$")
    assert not is_decorative("₩")


def test_a_whitespace_run_collapses_to_one_space_and_keeps_its_range():
    result = normalize("a \t\n  b", EN)
    assert result.text == "a b"
    assert result.pieces[1].source.length == 5
    assert result.pieces[1].text == " "


def test_a_line_break_never_reaches_the_engine():
    assert "\n" not in normalize("one\ntwo\n\nthree", EN).text


# ======================================================================
# Grapheme clusters
# ======================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466", 1),  # ZWJ family
        ("\U0001F44D\U0001F3FD", 1),  # skin-tone modifier
        ("\U0001F1F0\U0001F1F7", 1),  # one flag
        ("\U0001F1F0\U0001F1F7\U0001F1FA\U0001F1F8", 2),  # two flags, not one
        ("é", 1),  # combining acute
        ("한", 1),  # Hangul jamo L V T
        ("한글", 2),
        ("\r\n", 1),
        ("a\r\nb", 3),
        ("1️⃣", 1),  # keycap
        ("한글", 2),
        ("", 0),
    ],
)
def test_grapheme_clusters_are_counted_the_way_a_reader_sees_them(text, expected):
    assert len(grapheme_clusters(text)) == expected
    assert "".join(grapheme_clusters(text)) == text


def test_grapheme_boundaries_start_at_zero_and_end_at_the_length():
    text = "a\U0001F44D\U0001F3FD한"
    bounds = grapheme_boundaries(text)
    assert bounds[0] == 0
    assert bounds[-1] == len(text)
    assert list(bounds) == sorted(bounds)


def test_is_grapheme_boundary_refuses_the_middle_of_a_cluster():
    text = "ab\U0001F468‍\U0001F469cd"
    inside = 3  # between the man and the joiner
    assert not is_grapheme_boundary(text, inside)
    assert is_grapheme_boundary(text, 2)
    assert is_grapheme_boundary(text, len(text) - 2)


def test_is_grapheme_boundary_agrees_with_a_full_scan():
    text = "가́b\U0001F1F0\U0001F1F7c\r\nd\U0001F44D\U0001F3FDe"
    bounds = set(grapheme_boundaries(text))
    for index in range(len(text) + 1):
        assert is_grapheme_boundary(text, index) == (index in bounds)


# ======================================================================
# Number readings
# ======================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "zero"),
        (7, "seven"),
        (13, "thirteen"),
        (21, "twenty-one"),
        (100, "one hundred"),
        (101, "one hundred one"),
        (1234, "one thousand two hundred thirty-four"),
        (1_000_000, "one million"),
        (1_234_567, "one million two hundred thirty-four thousand five hundred sixty-seven"),
    ],
)
def test_english_cardinals(value, expected):
    assert english_cardinal(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "first"), (2, "second"), (3, "third"), (8, "eighth"), (12, "twelfth"),
     (20, "twentieth"), (21, "twenty-first"), (30, "thirtieth"), (31, "thirty-first")],
)
def test_english_ordinals_for_every_day_of_a_month(value, expected):
    assert english_ordinal(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1984, "nineteen eighty-four"), (2026, "twenty twenty-six"),
     (2000, "two thousand"), (2005, "two thousand five"), (1900, "nineteen hundred")],
)
def test_years_are_read_the_way_they_are_said(value, expected):
    assert english_year(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "영"), (1, "일"), (10, "십"), (11, "십일"), (20, "이십"), (100, "백"),
     (1234, "천이백삼십사"), (10_000, "만"), (15_000, "만 오천"), (100_000, "십만"),
     (100_000_000, "일억")],
)
def test_sino_korean_numbers(value, expected):
    assert korean_sino(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "한"), (2, "두"), (3, "세"), (4, "네"), (10, "열"), (20, "스무"),
     (21, "스물한"), (99, "아흔아홉")],
)
def test_native_korean_numbers_use_the_form_a_counter_takes(value, expected):
    assert korean_native(value) == expected


def test_the_native_korean_series_stops_where_korean_stops_using_it():
    assert korean_native(100) is None
    assert korean_native(0) is None


def test_decimals_are_read_digit_by_digit_after_the_point():
    assert spoken_number("3.14", EN) == "three point one four"
    assert spoken_number("3.14", KO) == "삼 점 일 사"


# ======================================================================
# Expansion rules, one test per rule (F-27)
# ======================================================================


@pytest.mark.parametrize(
    ("source", "lang", "expected"),
    [
        ("1,234", EN, "one thousand two hundred thirty-four"),
        ("1,234", KO, "천이백삼십사"),
        ("1234", EN, "one thousand two hundred thirty-four"),
        ("1234", KO, "천이백삼십사"),
        ("3.14", EN, "three point one four"),
        ("3.14", KO, "삼 점 일 사"),
        ("44.1kHz", EN, "forty-four point one kilohertz"),
        ("44.1kHz", KO, "사십사 점 일 킬로헤르츠"),
        ("10-20", EN, "ten to twenty"),
        ("10-20", KO, "십에서 이십"),
        ("2026-09-08", KO, "이천이십육년 구월 팔일"),
        ("2026-09-08", EN, "September eighth, twenty twenty-six"),
        ("September 8, 2026", EN, "September eighth, twenty twenty-six"),
        ("2026년 9월 8일", KO, "이천이십육년 구월 팔일"),
        ("1,234원", KO, "천이백삼십사원"),
        ("$12.50", EN, "twelve dollars and fifty cents"),
        ("$12.50", KO, "십이 달러 오십 센트"),
        ("$1", EN, "one dollar"),
        ("$0.50", EN, "fifty cents"),
        ("12.5%", EN, "twelve point five percent"),
        ("12.5%", KO, "십이 점 오 퍼센트"),
        ("16 GB", EN, "sixteen gigabytes"),
        ("1 km", EN, "one kilometer"),
        ("5 kg", KO, "오 킬로그램"),
        ("36.5°C", EN, "thirty-six point five degrees Celsius"),
    ],
)
def test_the_readings_the_requirement_names(source, lang, expected):
    assert normalize(source, lang).text == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Dr. Kim", "Doctor Kim"),
        ("Mr. Lee", "Mister Lee"),
        ("Mrs. Park", "Missus Park"),
        ("Prof. Choi", "Professor Choi"),
        ("cats, dogs, etc.", "cats, dogs, et cetera"),
        ("fruit, e.g. apples", "fruit, for example apples"),
        ("that is, i.e. this", "that is, that is this"),
        ("us vs. them", "us versus them"),
        ("9 a.m.", "nine A M"),
        ("5 p.m.", "five P M"),
    ],
)
def test_english_abbreviations(source, expected):
    assert normalize(source, EN).text == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("3개", "세 개"),
        ("1개", "한 개"),
        ("20개", "스무 개"),
        ("21개", "스물한 개"),
        ("100개", "백 개"),
        ("2명", "두 명"),
        ("3시간", "세 시간"),
        ("5시", "다섯시"),
        ("30분", "삼십분"),
        ("3개월", "삼개월"),
        ("6월", "유월"),
        ("10월", "시월"),
        ("2026년", "이천이십육년"),
        ("1.5시간", "일 점 오 시간"),
    ],
)
def test_korean_counters_choose_the_right_number_series(source, expected):
    assert normalize(source, KO).text == expected


def test_a_korean_counter_is_read_in_korean_even_in_an_english_sentence():
    assert normalize("Buy 3개 today", EN).text == "Buy 세 개 today"


def test_a_bare_four_digit_number_is_a_quantity_and_not_a_year():
    # Only a date's year gets the paired reading; "1984 people" does not.
    assert normalize("1984", EN).text == "one thousand nine hundred eighty-four"


# ======================================================================
# What we refuse to expand (conservatism is the requirement, not caution)
# ======================================================================


@pytest.mark.parametrize(
    "source",
    ["v1.2.3", "abc123", "1,2,3", "1-2-3", "010-1234-5678", "ISBN 978-3-16-148410-0",
     "3rd", "St. Peter", "Ms. Kim", "-40", "2026-13-45"],
)
def test_text_we_cannot_read_with_confidence_is_left_exactly_as_typed(source):
    assert normalize(source, EN).text == source
    assert normalize(source, KO).text == source


def test_normalisation_never_changes_the_source_field():
    source = "가격 1,234원 \U0001F389"
    result = normalize(source, KO)
    assert result.source == source


def test_normalisation_is_deterministic():
    source = "Dr. Kim paid $12.50 for 3개 on 2026-09-08. \U0001F389"
    for lang in (EN, KO):
        assert normalize(source, lang).text == normalize(source, lang).text
        assert normalize(source, lang).pieces == normalize(source, lang).pieces


def test_a_long_document_still_tiles():
    source = ("문장 하나. Sentence two, 1,234원 and 3.14. \U0001F389\n\n") * 500
    result = normalize(source, KO)
    cursor = 0
    for piece in result.pieces:
        assert piece.source.start == cursor
        cursor = piece.source.end
    assert cursor == len(source)

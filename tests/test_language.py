"""F-05: which language a sentence is read in."""

from __future__ import annotations

import pytest

from echoact import paths
from echoact.domain import Language
from echoact.text.language import EN, KO, is_engine_language, resolve_language


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """No test may read or write the real user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    yield
    paths.data_dir.cache_clear()


def test_automatic_mode_reads_a_hangul_sentence_in_korean():
    assert resolve_language("안녕하세요.", Language.AUTO) == KO


def test_automatic_mode_reads_a_latin_sentence_in_english():
    assert resolve_language("Hello there.", Language.AUTO) == EN


def test_a_single_hangul_word_is_enough_to_make_a_sentence_korean():
    assert resolve_language("Please read this 문장 aloud.", Language.AUTO) == KO


def test_isolated_jamo_still_counts_as_hangul():
    assert resolve_language("한", Language.AUTO) == KO


def test_digits_and_symbols_alone_fall_back_to_english():
    assert resolve_language("1,234 (56%) !!", Language.AUTO) == EN
    assert resolve_language("\U0001F389\U0001F389", Language.AUTO) == EN


def test_an_explicit_choice_overrides_the_hangul_test_in_both_directions():
    assert resolve_language("안녕하세요.", Language.EN) == EN
    assert resolve_language("Hello there.", Language.KO) == KO


def test_the_empty_sentence_is_english_rather_than_undefined():
    assert resolve_language("", Language.AUTO) == EN


def test_resolution_is_pure():
    text = "혼합 sentence 123."
    assert resolve_language(text, Language.AUTO) == resolve_language(text, Language.AUTO)


def test_only_the_two_engine_codes_are_engine_languages():
    assert is_engine_language(KO)
    assert is_engine_language(EN)
    assert not is_engine_language("auto")
    assert not is_engine_language("ja")


def test_the_returned_code_is_what_the_engine_is_called_with():
    # A.5's spikes call synthesize(..., lang="ko" | "en"); the domain enum's
    # values happen to match, and this test is what keeps them matching.
    assert (KO, EN) == (Language.KO.value, Language.EN.value)

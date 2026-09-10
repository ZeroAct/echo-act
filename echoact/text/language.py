"""F-05's language resolution, per sentence.

The engine takes a two-letter code on every call, so the question "which
language is this sentence?" has to be answered somewhere; answering it here,
once, is what keeps the GUI's preview, the segmenter, and the duration
estimate from disagreeing.  The Hangul test itself lives in
``domain.contains_hangul`` for the same reason.
"""

from __future__ import annotations

from typing import Final

from ..domain import Language, contains_hangul

#: The codes the engine accepts (`lang=` in every ``synthesize`` call).
KO: Final = "ko"
EN: Final = "en"

ENGINE_LANGUAGES: Final = frozenset({KO, EN})


def resolve_language(text: str, selected: Language = Language.AUTO) -> str:
    """Return the engine language code for one sentence.

    F-05: an explicit Korean or English selection wins outright -- the user
    asked for a reading, not for a guess -- and automatic mode decides on
    whether the sentence contains Hangul.  The test is "contains", not
    "is mostly": a Korean sentence quoting an English phrase is still read
    by the Korean voice, and the alternative (a per-sentence majority vote)
    would flip the voice mid-paragraph on a single loan word.
    """
    if selected is Language.KO:
        return KO
    if selected is Language.EN:
        return EN
    return KO if contains_hangul(text) else EN


def is_engine_language(code: str) -> bool:
    return code in ENGINE_LANGUAGES


__all__ = ["EN", "ENGINE_LANGUAGES", "KO", "is_engine_language", "resolve_language"]

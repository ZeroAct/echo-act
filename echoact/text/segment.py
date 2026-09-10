"""F-81's segmentation: sentences first, then clauses, deterministically.

Every segment carries a source range in Unicode code points into the text the
user entered, and those ranges tile that text exactly -- no gaps, no overlaps,
first starting at 0 and last ending at ``len(text)``.  F-27 makes this a
correctness requirement rather than a convenience: the highlight, the segment
time table, and every external projection of a job are built on it, and a
range that is off by one is a defect no interface can hide (A.2, step 2).

Three properties are worth stating because the rest of the product relies on
them and none of them is obvious from the code:

* The function is pure.  Same text and same settings, same segmentation --
  F-81 requires it of every entry path, and it is also what lets a REST
  caller and the GUI agree on segment indices without sharing state.
* Cuts land only on boundaries of the F-27 alignment, which makes "never
  inside a word, a number, or a grapheme cluster" structural: a number and
  its expansion are one indivisible piece, so there is no offset inside one
  to cut at.
* Unspoken spans -- emoji, decorative symbols, a run of whitespace -- are
  never segments of their own where a neighbour exists.  F-27 attaches them
  to a neighbouring segment so the highlight still travels across them, and
  the preceding segment is that neighbour, matching the way F-82's silence
  is attributed backwards.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Final

from ..domain import Segment, SpeakingStyle, TextRange, VoiceSettings, clamp
from ..policy import (
    ESTIMATE_CODEPOINTS_PER_SECOND_EN,
    ESTIMATE_CODEPOINTS_PER_SECOND_KO,
    FIRST_SEGMENT_MAX_SECONDS,
    PARAGRAPH_EXTRA_GAP_MS,
    SEGMENT_MAX_CODEPOINTS,
    SEGMENT_MAX_SECONDS,
    SEGMENT_MIN_CODEPOINTS,
    STYLE_SEGMENT_GAP_MS,
    STYLE_TEMPO_MULTIPLIER,
    TEMPO_MAX,
    TEMPO_MIN,
)
from .language import KO, resolve_language
from .normalize import (
    AMBIGUOUS_ABBREVIATIONS,
    NON_TERMINAL_ABBREVIATIONS,
    Normalized,
    is_grapheme_boundary,
    normalize,
)

# ======================================================================
# Sentence boundaries
# ======================================================================

#: Fullwidth forms included: a Korean or Japanese keyboard produces them and
#: they are unambiguously terminal, unlike the ASCII period.
_TERMINATORS: Final = frozenset(".!?。！？．…‥⋯؟।")
_STRONG_TERMINATORS: Final = frozenset("!?。！？…‥⋯؟।")
#: A quote or bracket that closes after the terminator belongs to the
#: sentence it closes -- '"Stop!" she said' must not break before `she`.
_CLOSERS: Final = frozenset("\"'”’）)]}»›〉》」』〞")
_LINE_BREAKS: Final = frozenset("\n\r  \f\v")

#: Verb endings that make a Korean sentence's final period unmistakable, so
#: that a missing space after it ("...합니다.다음") still splits.  English
#: cannot use the same trick: its abbreviations end in letters too.
_KO_SENTENCE_ENDINGS: Final = frozenset("다요죠까네오음함임슴")

_WS_RUN: Final = re.compile(r"\s+")
_WORD: Final = re.compile(r"\S+")


def _absorb_whitespace(text: str, index: int) -> int:
    match = _WS_RUN.match(text, index)
    return match.end() if match else index


def _abbreviation_before(text: str, dot: int) -> str:
    """The token ending at ``text[dot]``, e.g. ``Dr`` or ``e.g``."""
    start = dot
    while start > 0:
        previous = text[start - 1]
        if not ((previous.isalpha() and previous.isascii()) or previous == "."):
            break
        start -= 1
    return text[start:dot].rstrip(".")


def _terminates(text: str, first: int, last: int, after_closers: int) -> bool:
    """Whether the terminator run ``text[first:last]`` ends a sentence."""
    run = text[first:last]
    if any(ch in _STRONG_TERMINATORS for ch in run):
        return True
    if last - first >= 3:
        return True  # "..." is an ellipsis, not three abbreviations
    before = text[first - 1] if first else ""
    following = text[after_closers] if after_closers < len(text) else ""
    if before.isdigit() and following.isdigit():
        return False
    if before in _KO_SENTENCE_ENDINGS:
        return True
    token = _abbreviation_before(text, first)
    if token in NON_TERMINAL_ABBREVIATIONS:
        return False
    if len(token) == 1 and token.isupper():
        return False  # an initial: "J. R. R. Tolkien"
    if token in AMBIGUOUS_ABBREVIATIONS:
        # "etc." ends a sentence only when what follows starts one.
        nxt = _absorb_whitespace(text, after_closers)
        return nxt >= len(text) or text[nxt].isupper() or not text[nxt].isascii()
    # A period glued to more text is inside something -- a host name, a
    # version, a file name -- not at the end of a sentence.
    return following == "" or following.isspace()


def split_sentences(text: str) -> list[TextRange]:
    """Sentence ranges that tile ``text``.

    Trailing whitespace, including the line break, belongs to the sentence it
    follows: F-27 attributes the pause after a sentence to that sentence, and
    keeping the two together is what makes a paragraph break detectable later
    without a second pass over the source.
    """
    spans: list[TextRange] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in _TERMINATORS:
            run_end = index
            while run_end < length and text[run_end] in _TERMINATORS:
                run_end += 1
            closed = run_end
            while closed < length and text[closed] in _CLOSERS:
                closed += 1
            if _terminates(text, index, run_end, closed):
                end = _absorb_whitespace(text, closed)
                spans.append(TextRange(start, end))
                start = index = end
            else:
                index = run_end
            continue
        if char in _LINE_BREAKS:
            # A line with no terminator is still a unit: a heading, a list
            # item, a line of dialogue.  Reading it into the next line would
            # be wrong however the punctuation falls.
            end = _absorb_whitespace(text, index)
            spans.append(TextRange(start, end))
            start = index = end
            continue
        index += 1
    if start < length:
        spans.append(TextRange(start, length))
    return spans


# ======================================================================
# Clause boundaries
# ======================================================================

_CLAUSE_PUNCTUATION: Final = frozenset(",;:，；：、")
_EN_CONJUNCTIONS: Final = frozenset({"and", "but", "or", "so"})
_KO_CONJUNCTIONS: Final = ("그리고", "하지만", "그래서", "또는", "그러나", "그런데")
#: Korean connective endings.  Two-syllable forms are unambiguous; the
#: single-syllable ones can also end a noun (사고, 참고), which costs a pause
#: in the wrong place at worst -- never a cut inside a word.
_KO_CONNECTIVE_LONG: Final = ("지만", "는데", "거나", "면서", "아서", "어서", "해서")
_KO_CONNECTIVE_SHORT: Final = frozenset("고며면서")

_RANK_PUNCTUATION: Final = 0
_RANK_CONJUNCTION: Final = 1
_RANK_CONNECTIVE: Final = 2
_RANK_WORD: Final = 3
#: Ranks F-81 calls clause boundaries.  A bare word boundary is not one, and
#: is only used where a sentence offers nothing better (see ``_cut``).
_CLAUSE_RANKS: Final = (_RANK_PUNCTUATION, _RANK_CONJUNCTION, _RANK_CONNECTIVE)


def _clause_candidates(text: str) -> dict[int, int]:
    """Offsets where ``text`` may be cut, each with its rank (lower better)."""
    found: dict[int, int] = {}

    def offer(position: int, rank: int) -> None:
        if 0 < position < len(text) and rank < found.get(position, _RANK_WORD + 1):
            found[position] = rank

    for index, char in enumerate(text):
        if char not in _CLAUSE_PUNCTUATION:
            continue
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if before.isdigit() and after.isdigit():
            continue  # a thousands separator or a decimal comma
        offer(_absorb_whitespace(text, index + 1), _RANK_PUNCTUATION)

    for match in _WS_RUN.finditer(text):
        word_start = match.end()
        offer(word_start, _RANK_WORD)
        word_end = match.start()
        if word_end >= 2 and (
            text[word_end - 2 : word_end] in _KO_CONNECTIVE_LONG
            or (text[word_end - 1] in _KO_CONNECTIVE_SHORT and _is_hangul(text[word_end - 2]))
        ):
            offer(word_start, _RANK_CONNECTIVE)
        following = _WORD.match(text, word_start)
        if following is not None:
            word = following.group()
            if word.strip(",.!?").lower() in _EN_CONJUNCTIONS or word.startswith(_KO_CONJUNCTIONS):
                offer(word_start, _RANK_CONJUNCTION)
    return found


def _is_hangul(char: str) -> bool:
    return "가" <= char <= "힣" or "ᄀ" <= char <= "ᇿ"


# ======================================================================
# Duration estimate
# ======================================================================


def codepoints_per_second(lang: str) -> float:
    return ESTIMATE_CODEPOINTS_PER_SECOND_KO if lang == KO else ESTIMATE_CODEPOINTS_PER_SECOND_EN


def estimate_seconds(spoken: str, lang: str, tempo: float) -> float:
    """Audio seconds for text already normalised, per policy's measurements.

    The estimate runs on the *spoken* text, not the source: "1,234" is five
    code points and "one thousand two hundred thirty-four" is thirty-six, and
    it is the second that the engine has to say.
    """
    return _seconds(len(spoken), lang, tempo)


def _seconds(codepoints: int, lang: str, tempo: float) -> float:
    return codepoints / codepoints_per_second(lang) / tempo


def effective_tempo(settings: VoiceSettings) -> float:
    """F-08's style multiplier over F-07's tempo, clamped as policy requires."""
    multiplier = STYLE_TEMPO_MULTIPLIER[settings.style.value]
    return clamp(settings.tempo * multiplier, TEMPO_MIN, TEMPO_MAX)


def trailing_silence_ms(style: SpeakingStyle, *, paragraph_break: bool) -> int:
    """The pause after a segment: F-08's style preset, plus F-27's paragraph.

    The app inserts this itself.  A.5 measured the engine's own silence
    parameter doing nothing once the text is chunked first, so F-82 makes the
    gap the application's and this is where its length is decided.
    """
    gap = STYLE_SEGMENT_GAP_MS[style.value]
    return gap + PARAGRAPH_EXTRA_GAP_MS if paragraph_break else gap


_PARAGRAPH_TAIL: Final = re.compile(r"\s*$")
_LINE_BREAK_RUN: Final = re.compile(r"\r\n|[\n\r  \f\v]")


def ends_paragraph(source_slice: str) -> bool:
    """Whether this span's own trailing whitespace contains a blank line."""
    tail = _PARAGRAPH_TAIL.search(source_slice)
    return tail is not None and len(_LINE_BREAK_RUN.findall(tail.group())) >= 2


# ======================================================================
# Splitting one sentence
# ======================================================================


@dataclass(frozen=True, slots=True)
class _Chunk:
    start: int
    end: int
    spoken: str
    lang: str


def _cut(
    text: str,
    norm: Normalized,
    candidates: dict[int, int],
    sorted_offsets: list[int],
    start: int,
    lang: str,
    tempo: float,
    max_seconds: float,
) -> int:
    """Where the segment starting at ``start`` should end.

    Returns ``len(text)`` when the rest fits, or when the sentence offers no
    boundary that may legally be cut -- F-81 forbids cutting inside a word,
    so an unbreakable run stays whole and over-long rather than being
    chopped.  Latency is a quality of service; a word cut in half is not.
    """
    length = len(text)
    produced_start = norm.produced_offset(start)
    remaining = len(norm.text) - produced_start
    if (
        length - start <= SEGMENT_MAX_CODEPOINTS
        and _seconds(remaining, lang, tempo) <= max_seconds
    ):
        return length

    budget = int(max_seconds * codepoints_per_second(lang) * tempo)
    seconds_end = norm.source_offset(produced_start + budget)
    hard_end = min(length, start + SEGMENT_MAX_CODEPOINTS, max(seconds_end, start + 1))
    hard_end = max(hard_end, min(length, start + SEGMENT_MIN_CODEPOINTS))

    for ranks in (_CLAUSE_RANKS, (_RANK_WORD,)):
        for require_tail in (True, False):
            chosen = _best_offset(
                candidates, sorted_offsets, start, hard_end, length, ranks, require_tail
            )
            if chosen is not None:
                return chosen
    return length


def _best_offset(
    candidates: dict[int, int],
    sorted_offsets: list[int],
    start: int,
    hard_end: int,
    length: int,
    ranks: tuple[int, ...],
    require_tail: bool,
) -> int | None:
    """The latest acceptable cut at or before ``hard_end``.

    Latest, not best-ranked: a comma five characters in is a clause boundary
    too, and preferring it would shred the sentence into segments far shorter
    than F-81 allows.
    """
    upper = bisect_right(sorted_offsets, hard_end)
    for index in range(upper - 1, -1, -1):
        offset = sorted_offsets[index]
        if offset - start < SEGMENT_MIN_CODEPOINTS:
            break
        if candidates[offset] not in ranks:
            continue
        if require_tail and 0 < length - offset < SEGMENT_MIN_CODEPOINTS:
            continue
        return offset
    return None


def _split_sentence(
    text: str, norm: Normalized, lang: str, tempo: float, first_max_seconds: float
) -> list[_Chunk]:
    cuttable = set(norm.boundaries)
    candidates = {
        offset: rank
        for offset, rank in _clause_candidates(text).items()
        if offset in cuttable and is_grapheme_boundary(text, offset)
    }
    sorted_offsets = sorted(candidates)
    chunks: list[_Chunk] = []
    start = 0
    max_seconds = first_max_seconds
    while start < len(text):
        end = _cut(text, norm, candidates, sorted_offsets, start, lang, tempo, max_seconds)
        chunks.append(_Chunk(start, end, norm.spoken_between(start, end), lang))
        start = end
        max_seconds = SEGMENT_MAX_SECONDS
    return chunks


# ======================================================================
# The public entry point
# ======================================================================


def segment_text(source: str, settings: VoiceSettings) -> list[Segment]:
    """Split ``source`` into the segments one job will generate (F-81, F-27).

    ``settings`` supplies F-05's language selection, F-07's tempo, and F-08's
    style, all three of which change where the cuts fall: the language picks
    the reading and the estimate, and tempo and style set how much audio a
    given number of code points becomes.
    """
    if not source:
        return []
    tempo = effective_tempo(settings)
    chunks: list[_Chunk] = []
    # The 8-second cap belongs to the first segment that actually produces
    # audio.  Leading blank lines or an emoji-only first line are attached to
    # a neighbour later, so counting them would spend the cap on silence.
    spoken_seen = False
    for span in split_sentences(source):
        sentence = span.slice(source)
        lang = resolve_language(sentence, settings.language)
        norm = normalize(sentence, lang)
        first_cap = SEGMENT_MAX_SECONDS if spoken_seen else FIRST_SEGMENT_MAX_SECONDS
        for chunk in _split_sentence(sentence, norm, lang, tempo, first_cap):
            spoken_seen = spoken_seen or bool(chunk.spoken.strip())
            chunks.append(
                _Chunk(span.start + chunk.start, span.start + chunk.end, chunk.spoken, chunk.lang)
            )
    chunks = _attach_unspoken(chunks)
    segments = _build(chunks, source, settings.style)
    _assert_tiles(segments, len(source))
    return segments


def _attach_unspoken(chunks: list[_Chunk]) -> list[_Chunk]:
    """F-27: a span that yields no audio joins a neighbour, keeping its range.

    Backwards by preference, so that the emoji or the blank line after a
    sentence is highlighted while that sentence's own trailing silence plays.
    A document with nothing to say keeps one segment covering all of it --
    the range still has to exist, or the source text stops being complete.
    """
    merged: list[_Chunk] = []
    for chunk in chunks:
        if chunk.spoken.strip() or not merged:
            merged.append(chunk)
            continue
        previous = merged[-1]
        merged[-1] = _Chunk(
            previous.start, chunk.end, previous.spoken + chunk.spoken, previous.lang
        )
    if len(merged) > 1 and not merged[0].spoken.strip():
        head, second = merged[0], merged[1]
        merged[1] = _Chunk(head.start, second.end, head.spoken + second.spoken, second.lang)
        del merged[0]
    return merged


def _build(chunks: list[_Chunk], source: str, style: SpeakingStyle) -> list[Segment]:
    segments: list[Segment] = []
    for index, chunk in enumerate(chunks):
        spoken = chunk.spoken.strip()
        last = index == len(chunks) - 1
        silence = (
            0
            if last
            else trailing_silence_ms(
                style, paragraph_break=ends_paragraph(source[chunk.start : chunk.end])
            )
        )
        segments.append(
            Segment(
                index=index,
                source=TextRange(chunk.start, chunk.end),
                spoken_text=spoken,
                language=chunk.lang,
                trailing_silence_ms=silence,
            )
        )
    return segments


def _assert_tiles(segments: list[Segment], length: int) -> None:
    """F-27's invariant, checked rather than trusted."""
    cursor = 0
    for segment in segments:
        if segment.source.start != cursor:
            raise AssertionError(
                f"segment {segment.index} starts at {segment.source.start}, expected {cursor}"
            )
        cursor = segment.source.end
    if cursor != length:
        raise AssertionError(f"segments cover {cursor} of {length} code points")


def estimate_job_seconds(segments: list[Segment], tempo: float) -> float:
    """Total audio a job will produce, silence included (F-88's input)."""
    total = 0.0
    for segment in segments:
        total += estimate_seconds(segment.spoken_text, segment.language, tempo)
        total += segment.trailing_silence_ms / 1000
    return total


__all__ = [
    "codepoints_per_second",
    "effective_tempo",
    "ends_paragraph",
    "estimate_job_seconds",
    "estimate_seconds",
    "segment_text",
    "split_sentences",
    "trailing_silence_ms",
]

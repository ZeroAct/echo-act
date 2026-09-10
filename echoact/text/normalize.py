"""F-27's normalisation, carrying an alignment back to what the user typed.

The engine is given an expanded reading -- "1,234" becomes "천이백삼십사" or
"one thousand two hundred thirty-four" -- but every segment range the rest of
the product uses must still point at the original characters.  So the
expansion is recorded as an *edit script*: an ordered list of
``Piece(source_range, produced_text)`` that tiles the source exactly.  A
per-character index array was the obvious alternative and is worse in the two
ways that matter here: it cannot represent a span that produces nothing (an
emoji, a run of whitespace), and an off-by-one in it is invisible, whereas a
gap or an overlap between pieces is an assertion failure at construction.

Two consequences the callers depend on:

* Pieces are atomic.  A number, a date, or an abbreviation is one piece, so a
  segmenter that only ever cuts at a piece boundary cannot cut inside one --
  which is half of F-81's "never inside a word, a number, or a grapheme
  cluster".
* A piece whose produced text is empty still occupies its source range.  A.3
  decided emoji and decorative symbols are not sent for synthesis, because
  A.5 measured the engine vocalising three emoji as 1.32 s of audio; F-27
  requires their characters to stay in the source and the highlight to travel
  across them.  An empty piece is exactly that.

Expansion is deliberately conservative.  A reading we are not sure of is
worse than no reading at all, because a wrong expansion changes what is
spoken while leaving the source text -- and therefore the user's ability to
notice -- untouched.  So "1.2.3", "St.", and "3rd" are left exactly as typed.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from ..domain import TextRange
from .language import EN, KO

# ======================================================================
# Grapheme clusters (UAX #29, the subset this product can encounter)
# ======================================================================
#
# The `regex` package is not a dependency, and `str` indexing counts code
# points, so the cluster rules are implemented here.  They are needed twice:
# a split must never fall inside a cluster (F-81), and an emoji sequence must
# be suppressed whole rather than leaving a stray joiner or skin-tone
# modifier behind for the engine to read.

_CR: Final = 1
_LF: Final = 2
_CONTROL: Final = 3
_EXTEND: Final = 4
_ZWJ: Final = 5
_RI: Final = 6
_PREPEND: Final = 7
_SPACINGMARK: Final = 8
_L: Final = 9
_V: Final = 10
_T: Final = 11
_LV: Final = 12
_LVT: Final = 13
_EXTPICT: Final = 14
_OTHER: Final = 0

#: Extended_Pictographic, approximated by block ranges.  Unicode data files
#: are not shipped with the app, and the exact property is only needed to
#: decide "emoji-ish", so the blocks are enumerated instead.
_EXTPICT_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2190, 0x21FF),
    (0x2300, 0x23FF),
    (0x24C2, 0x24C2),
    (0x25A0, 0x25FF),
    (0x2600, 0x27BF),
    (0x2900, 0x297F),
    (0x2934, 0x2935),
    (0x2B00, 0x2BFF),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
    (0x1F000, 0x1FAFF),
    (0x1FC00, 0x1FFFD),
)

#: Characters that are decorative in the F-27 sense: pictographs, dingbats,
#: the symbol blocks, and the bullets a pasted list carries.  Currency signs
#: and mathematical operators are deliberately absent -- they carry meaning a
#: listener needs, and several of them are expanded by the rules below.
_DECORATIVE_RANGES: Final[tuple[tuple[int, int], ...]] = tuple(
    sorted(
        _EXTPICT_RANGES
        + (
            (0x2022, 0x2023),
            (0x2043, 0x2043),
            (0x20D0, 0x20FF),
            (0xFE00, 0xFE0F),
            (0xE0100, 0xE01EF),
        )
    )
)

#: Attach to a preceding pictograph without starting a new cluster.
_EMOJI_MODIFIERS: Final[tuple[tuple[int, int], ...]] = (
    (0x1F3FB, 0x1F3FF),
    (0xFE00, 0xFE0F),
    (0x20E3, 0x20E3),
)

_PREPEND_CODEPOINTS: Final[frozenset[int]] = frozenset(
    {0x0600, 0x0601, 0x0602, 0x0603, 0x0604, 0x0605, 0x06DD, 0x070F, 0x0890,
     0x0891, 0x08E2, 0x0D4E, 0x110BD, 0x110CD}
)


def _in_ranges(cp: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    """Membership in an ascending, non-overlapping range table."""
    for lo, hi in ranges:
        if lo <= cp <= hi:
            return True
        if cp < lo:
            break
    return False


_CLASS_CACHE: dict[int, int] = {}


def _gb_class(ch: str) -> int:
    cp = ord(ch)
    cached = _CLASS_CACHE.get(cp)
    if cached is not None:
        return cached
    _CLASS_CACHE[cp] = value = _compute_gb_class(cp, ch)
    return value


def _compute_gb_class(cp: int, ch: str) -> int:
    if cp == 0x0D:
        return _CR
    if cp == 0x0A:
        return _LF
    if cp == 0x200D:
        return _ZWJ
    if 0x1F1E6 <= cp <= 0x1F1FF:
        return _RI
    if 0x1100 <= cp <= 0x115F:
        return _L
    if 0x1160 <= cp <= 0x11A7:
        return _V
    if 0x11A8 <= cp <= 0x11FF:
        return _T
    if 0xAC00 <= cp <= 0xD7A3:
        return _LV if (cp - 0xAC00) % 28 == 0 else _LVT
    if _in_ranges(cp, _EMOJI_MODIFIERS):
        return _EXTEND
    if cp in _PREPEND_CODEPOINTS:
        return _PREPEND
    category = unicodedata.category(ch)
    if category in ("Mn", "Me"):
        return _EXTEND
    if category == "Mc":
        return _SPACINGMARK
    if category in ("Cc", "Cf", "Zl", "Zp"):
        return _CONTROL
    if _in_ranges(cp, _EXTPICT_RANGES):
        return _EXTPICT
    return _OTHER


def grapheme_boundaries(text: str) -> tuple[int, ...]:
    """Every index at which ``text`` may be cut, including 0 and ``len``."""
    if not text:
        return (0,)
    out = [0]
    chain = False  # an ExtPict Extend* run is open
    zwj_after_pict = False
    ri_run = 0
    prev = _gb_class(text[0])
    _, chain, zwj_after_pict, ri_run = _advance(prev, chain, zwj_after_pict, ri_run)
    for i in range(1, len(text)):
        cur = _gb_class(text[i])
        if _breaks(prev, cur, zwj_after_pict, ri_run):
            out.append(i)
        prev, chain, zwj_after_pict, ri_run = _advance(cur, chain, zwj_after_pict, ri_run)
    out.append(len(text))
    return tuple(out)


def _advance(
    cur: int, chain: bool, zwj_after_pict: bool, ri_run: int
) -> tuple[int, bool, bool, int]:
    if cur == _EXTPICT:
        chain, zwj_after_pict = True, False
    elif cur == _EXTEND:
        zwj_after_pict = False
    elif cur == _ZWJ:
        zwj_after_pict = chain
    else:
        chain, zwj_after_pict = False, False
    ri_run = ri_run + 1 if cur == _RI else 0
    return cur, chain, zwj_after_pict, ri_run


def _breaks(prev: int, cur: int, zwj_after_pict: bool, ri_run: int) -> bool:
    if prev == _CR and cur == _LF:
        return False
    if prev in (_CR, _LF, _CONTROL) or cur in (_CR, _LF, _CONTROL):
        return True
    if cur in (_EXTEND, _ZWJ, _SPACINGMARK):
        return False
    if prev == _PREPEND:
        return False
    if prev == _L and cur in (_L, _V, _LV, _LVT):
        return False
    if prev in (_LV, _V) and cur in (_V, _T):
        return False
    if prev in (_LVT, _T) and cur == _T:
        return False
    if prev == _ZWJ and cur == _EXTPICT and zwj_after_pict:
        return False
    if prev == _RI and cur == _RI and ri_run % 2 == 1:
        return False
    return True


def grapheme_clusters(text: str) -> list[str]:
    bounds = grapheme_boundaries(text)
    return [text[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]


#: How far back ``is_grapheme_boundary`` re-derives the machine's state.  A
#: cluster longer than this does not occur in text a person typed; the flag
#: sequences and ZWJ families that motivate the rules are well under it.
_RESTART_WINDOW: Final = 64


def is_grapheme_boundary(text: str, index: int) -> bool:
    """Whether ``index`` splits ``text`` without cutting a cluster.

    Scanning the whole string for one question costs O(n) per candidate split
    and F-81 asks that question often on a 50,000-character document, so the
    state machine is restarted from a nearby character that cannot be inside
    a cluster instead.
    """
    if index <= 0 or index >= len(text):
        return True
    start = max(0, index - _RESTART_WINDOW)
    while start > 0 and _gb_class(text[start]) not in (_OTHER, _CONTROL, _CR, _LF):
        start -= 1
    return (index - start) in {b for b in grapheme_boundaries(text[start:index + 1])}


# ======================================================================
# Number readings
# ======================================================================

_EN_ONES: Final = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_EN_TENS: Final = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
)
_EN_SCALES: Final = ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand"))
_EN_ORDINALS: Final = {
    "one": "first", "two": "second", "three": "third", "five": "fifth",
    "eight": "eighth", "nine": "ninth", "twelve": "twelfth",
    "twenty": "twentieth", "thirty": "thirtieth", "forty": "fortieth",
    "fifty": "fiftieth", "sixty": "sixtieth", "seventy": "seventieth",
    "eighty": "eightieth", "ninety": "ninetieth", "hundred": "hundredth",
    "thousand": "thousandth",
}

_KO_SINO_DIGITS: Final = "영일이삼사오육칠팔구"
_KO_SMALL_UNITS: Final = ("", "십", "백", "천")
_KO_BIG_UNITS: Final = ("", "만", "억", "조", "경")
_KO_NATIVE_ONES: Final = (
    "", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉",
)
_KO_NATIVE_ONES_ATTR: Final = (
    "", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉",
)
_KO_NATIVE_TENS: Final = (
    "", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔",
)
_KO_NATIVE_TENS_ATTR: Final = (
    "", "열", "스무", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔",
)


def english_cardinal(n: int) -> str:
    if n < 0:
        return "minus " + english_cardinal(-n)
    if n < 20:
        return _EN_ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _EN_TENS[tens] + (f"-{_EN_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = f"{_EN_ONES[hundreds]} hundred"
        return f"{head} {english_cardinal(rest)}" if rest else head
    for value, name in _EN_SCALES:
        if n >= value:
            count, rest = divmod(n, value)
            head = f"{english_cardinal(count)} {name}"
            return f"{head} {english_cardinal(rest)}" if rest else head
    return str(n)


def english_ordinal(n: int) -> str:
    words = english_cardinal(n)
    head, sep, last = words.rpartition("-") if "-" in words.rsplit(" ", 1)[-1] else words.rpartition(" ")
    ordinal = _EN_ORDINALS.get(last, last + "th")
    return head + sep + ordinal


def english_year(year: int) -> str:
    """The reading a listener expects for a year, not the plain cardinal.

    1984 is "nineteen eighty-four" and 2026 is "twenty twenty-six"; only the
    2000s are read as a cardinal, because "twenty oh five" is a style choice
    and "two thousand five" is not.
    """
    if 1100 <= year <= 1999 or 2010 <= year <= 2099:
        high, low = divmod(year, 100)
        if low == 0:
            return f"{english_cardinal(high)} hundred"
        if low < 10:
            return f"{english_cardinal(high)} oh {english_cardinal(low)}"
        return f"{english_cardinal(high)} {english_cardinal(low)}"
    return english_cardinal(year)


def korean_sino(n: int) -> str:
    """Sino-Korean reading: 1234 -> 천이백삼십사."""
    if n < 0:
        return "마이너스 " + korean_sino(-n)
    if n == 0:
        return "영"
    groups: list[int] = []
    rest = n
    while rest:
        rest, group = divmod(rest, 10_000)
        groups.append(group)
    if len(groups) > len(_KO_BIG_UNITS):
        return str(n)
    parts: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            continue
        body = _korean_sino_group(group)
        if index == 1 and group == 1:
            body = ""  # 10,000 is 만, never 일만
        parts.append(body + _KO_BIG_UNITS[index])
    return " ".join(parts)


def _korean_sino_group(group: int) -> str:
    out: list[str] = []
    for power in range(3, -1, -1):
        digit = (group // 10**power) % 10
        if not digit:
            continue
        if digit == 1 and power > 0:
            out.append(_KO_SMALL_UNITS[power])  # 십, not 일십
        else:
            out.append(_KO_SINO_DIGITS[digit] + _KO_SMALL_UNITS[power])
    return "".join(out)


def korean_native(n: int, *, attributive: bool = True) -> str | None:
    """Native-Korean reading, or ``None`` where the series does not reach.

    Counters like 개 and 시 take 하나/둘/셋, and before a counter those become
    한/두/세 -- "3개" is "세 개", never "삼 개".  The series is only used up to
    99 because beyond that Korean itself switches to the Sino reading.
    """
    if not 1 <= n <= 99:
        return None
    tens, ones = divmod(n, 10)
    tens_table = _KO_NATIVE_TENS_ATTR if attributive and ones == 0 else _KO_NATIVE_TENS
    ones_table = _KO_NATIVE_ONES_ATTR if attributive else _KO_NATIVE_ONES
    return tens_table[tens] + ones_table[ones]


def spoken_number(literal: str, lang: str) -> str:
    """Read a bare numeric literal such as ``1,234`` or ``3.14``."""
    digits = literal.replace(",", "")
    whole, _, fraction = digits.partition(".")
    value = int(whole) if whole else 0
    head = english_cardinal(value) if lang == EN else korean_sino(value)
    if not fraction:
        return head
    if lang == EN:
        tail = " ".join(_EN_ONES[int(d)] for d in fraction)
        return f"{head} point {tail}"
    tail = " ".join(_KO_SINO_DIGITS[int(d)] for d in fraction)
    return f"{head} 점 {tail}"


# ======================================================================
# Rule tables
# ======================================================================

_MONTHS: Final = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_NAMES: Final = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
#: 6월 is 유월 and 10월 is 시월, not 육월 and 십월.  The irregularity is the
#: whole reason months are not just "Sino number + 월".
_KO_MONTHS: Final = (
    "", "일월", "이월", "삼월", "사월", "오월", "유월",
    "칠월", "팔월", "구월", "시월", "십일월", "십이월",
)

# token -> (english singular, english plural, korean)
_UNITS: Final[dict[str, tuple[str, str, str]]] = {
    "kHz": ("kilohertz", "kilohertz", "킬로헤르츠"),
    "MHz": ("megahertz", "megahertz", "메가헤르츠"),
    "GHz": ("gigahertz", "gigahertz", "기가헤르츠"),
    "Hz": ("hertz", "hertz", "헤르츠"),
    "km": ("kilometer", "kilometers", "킬로미터"),
    "cm": ("centimeter", "centimeters", "센티미터"),
    "mm": ("millimeter", "millimeters", "밀리미터"),
    "kg": ("kilogram", "kilograms", "킬로그램"),
    "mg": ("milligram", "milligrams", "밀리그램"),
    "KB": ("kilobyte", "kilobytes", "킬로바이트"),
    "MB": ("megabyte", "megabytes", "메가바이트"),
    "GB": ("gigabyte", "gigabytes", "기가바이트"),
    "TB": ("terabyte", "terabytes", "테라바이트"),
    "ms": ("millisecond", "milliseconds", "밀리초"),
    "°C": ("degree Celsius", "degrees Celsius", "도"),
    "°F": ("degree Fahrenheit", "degrees Fahrenheit", "도"),
}

#: counter -> (native reading?, a space between number and counter?)
#: 번 is absent on purpose: "3번" is 삼번 for a bus and 세 번 for three times,
#: and nothing in the surrounding text settles which.
_KO_COUNTERS: Final[dict[str, tuple[bool, bool]]] = {
    "시간": (True, True),
    "개월": (False, False),
    "주일": (False, False),
    "학년": (False, False),
    "인분": (False, True),
    "개": (True, True),
    "명": (True, True),
    "사람": (True, True),
    "살": (True, True),
    "마리": (True, True),
    "권": (True, True),
    "대": (True, True),
    "병": (True, True),
    "잔": (True, True),
    "그루": (True, True),
    "가지": (True, True),
    "켤레": (True, True),
    "벌": (True, True),
    "채": (True, True),
    "시": (True, False),
    "년": (False, False),
    "월": (False, False),
    "일": (False, False),
    "원": (False, False),
    "분": (False, False),
    "초": (False, False),
    "층": (False, False),
    "주": (False, False),
    "호": (False, False),
    "도": (False, False),
    "회": (False, True),
    "세": (False, False),
    "미터": (False, True),
}

# abbreviation -> (english reading, korean reading)
#
# "Ms.", "St.", and "No." are deliberately absent: their expansions are
# ambiguous (Saint or Street; Number or the Spanish negative), and F-27's
# risk is asymmetric -- leaving text alone is recoverable, speaking the wrong
# word is not.
_ABBREVIATIONS: Final[dict[str, tuple[str, str]]] = {
    "Dr.": ("Doctor", "Doctor"),
    "Mr.": ("Mister", "Mister"),
    "Mrs.": ("Missus", "Missus"),
    "Prof.": ("Professor", "Professor"),
    "etc.": ("et cetera", "et cetera"),
    "e.g.": ("for example", "for example"),
    "i.e.": ("that is", "that is"),
    "vs.": ("versus", "versus"),
    # Spelled as letters rather than "in the morning": p.m. spans both
    # afternoon and evening, so the wordy reading is wrong half the time.
    "a.m.": ("A M", "오전"),
    "p.m.": ("P M", "오후"),
}

#: Tokens whose trailing period never ends a sentence (F-81).  Used by the
#: segmenter, which must agree with this table or "Dr. Kim" splits in two.
NON_TERMINAL_ABBREVIATIONS: Final[frozenset[str]] = frozenset(
    {"Dr", "Mr", "Mrs", "Ms", "Prof", "St", "Jr", "Sr", "Fig", "No", "Inc",
     "Ltd", "Co", "Corp", "Rev", "Gen", "Sgt", "Capt", "vs", "cf", "al",
     "e.g", "i.e", "a.m", "p.m", "A.M", "P.M", "U.S", "Ph.D", "approx", "est"}
)
#: Tokens that may or may not end a sentence; the segmenter looks at what
#: follows.
AMBIGUOUS_ABBREVIATIONS: Final[frozenset[str]] = frozenset({"etc", "Ave", "Rd"})


def _char_class(ranges: tuple[tuple[int, int], ...]) -> str:
    return "".join(
        f"\\U{lo:08x}" if lo == hi else f"\\U{lo:08x}-\\U{hi:08x}" for lo, hi in ranges
    )


_DECORATIVE_CLASS: Final = _char_class(_DECORATIVE_RANGES) + _char_class(
    ((0x1F1E6, 0x1F1FF), (0x1F3FB, 0x1F3FF), (0x20E3, 0x20E3), (0x200D, 0x200D))
)

_NUM: Final = r"\d{1,3}(?:,\d{3})+|\d+"
_DEC: Final = rf"(?:{_NUM})(?:\.\d+)?"
#: Nothing that would make the digits part of an identifier or a version.
#: Nothing may start a numeric match immediately after one of these.  The
#: dashes are what keeps "010-1234-5678" a phone number: with them absent, a
#: rule could start on the second group and read half of it as a range.
_LEFT: Final = r"(?<![-–~\d.,A-Za-z_])"


def _alternation(tokens: Iterable[str]) -> str:
    """Longest token first, so 시간 wins over 시 and 개월 over 개."""
    return "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))


#: Month names are matched capitalised only.  Lower-case "may" is a common
#: English verb, and "I may 3 times" is not a date.
_MONTH_TOKENS: Final = tuple(sorted({n.capitalize() for n in _MONTHS}, key=len, reverse=True))

_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("iso_date", r"(?<![\d-])\d{4}-\d{2}-\d{2}(?![\d-])"),
    (
        "en_date",
        r"(?<![A-Za-z])(?:"
        + "|".join(_MONTH_TOKENS)
        + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,?\s*\d{4})?(?![A-Za-z\d])",
    ),
    ("currency", _LEFT + rf"[$₩]\s?(?:{_NUM})(?:\.\d{{1,2}})?(?![\d])"),
    ("percent", _LEFT + rf"(?:{_DEC})\s?%"),
    ("unit", _LEFT + rf"(?:{_DEC})\s?(?:{_alternation(_UNITS)})(?![A-Za-z])"),
    ("ko_counter", _LEFT + rf"(?:{_DEC})\s?(?:{_alternation(_KO_COUNTERS)})"),
    (
        "number_range",
        # ``(?!\d)`` stops the second number backtracking to a shorter one to
        # satisfy the "no third group" lookahead that follows it.
        _LEFT + rf"(?:{_NUM})\s?[-–~]\s?(?:{_NUM})(?!\d)(?!\s?[-–~]\s?\d)(?![A-Za-z])",
    ),
    ("abbrev", r"(?<![A-Za-z.])(?:" + _alternation(_ABBREVIATIONS) + r")"),
    # ``(?!,\d)`` keeps "1,2,3" whole: the group is not a thousands group, so
    # reading only the "1" would speak a different list from the one written.
    # ``(?![-–~]\d)`` does the same for "010-1234-5678", which the range rule
    # above has already declined: a part of a phone number is not a number.
    ("decimal", _LEFT + rf"(?:{_NUM})\.\d+(?![\d.])(?!,\d)(?![-–~]\d)(?![A-Za-z])"),
    ("integer", _LEFT + rf"(?:{_NUM})(?![\d.]*\d)(?!,\d)(?![-–~]\d)(?![A-Za-z])"),
    ("decorative", rf"[{_DECORATIVE_CLASS}]+"),
    ("space", r"\s+"),
)

#: One alternation, scanned once.  Rule order is priority order: at any
#: position the first rule that can match wins, which is why the specific
#: patterns (a date, a currency amount) precede the general ones (a decimal,
#: an integer).
_MASTER: Final = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _RULES))

_RX_ISO: Final = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_RX_EN_DATE: Final = re.compile(
    r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?", re.ASCII
)
_RX_CURRENCY: Final = re.compile(r"([$₩])\s?([\d,]+)(?:\.(\d{1,2}))?")
_RX_VALUE_TAIL: Final = re.compile(r"([\d,.]+)\s?(.+)", re.DOTALL)
_RX_RANGE: Final = re.compile(r"([\d,]+)\s?[-–~]\s?([\d,]+)")


# ======================================================================
# Rule handlers
# ======================================================================


def _h_iso_date(match: re.Match[str], lang: str) -> str:
    parsed = _RX_ISO.fullmatch(match.group())
    if parsed is None:
        return match.group()
    year, month, day = (int(g) for g in parsed.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return match.group()  # a part number or a range, not a date
    return _spoken_date(year, month, day, lang)


def _h_en_date(match: re.Match[str], lang: str) -> str:
    parsed = _RX_EN_DATE.fullmatch(match.group())
    if parsed is None:
        return match.group()
    name, day_text, year_text = parsed.groups()
    month = _MONTHS.get(name.lower())
    day = int(day_text)
    if month is None or not 1 <= day <= 31:
        return match.group()
    year = int(year_text) if year_text else None
    if lang == KO:
        head = "" if year is None else f"{korean_sino(year)}년 "
        return f"{head}{_KO_MONTHS[month]} {korean_sino(day)}일"
    tail = "" if year is None else f", {english_year(year)}"
    return f"{_MONTH_NAMES[month]} {english_ordinal(day)}{tail}"


def _spoken_date(year: int, month: int, day: int, lang: str) -> str:
    if lang == KO:
        return f"{korean_sino(year)}년 {_KO_MONTHS[month]} {korean_sino(day)}일"
    return f"{_MONTH_NAMES[month]} {english_ordinal(day)}, {english_year(year)}"


def _h_currency(match: re.Match[str], lang: str) -> str:
    parsed = _RX_CURRENCY.fullmatch(match.group())
    if parsed is None:
        return match.group()
    sign, whole_text, cents_text = parsed.groups()
    whole = int(whole_text.replace(",", ""))
    cents = int(cents_text.ljust(2, "0")) if cents_text else 0
    if sign == "₩":
        # The won has no everyday subunit, so a decimal here is a plain
        # fractional amount rather than 100ths of a unit.
        literal = whole_text if not cents_text else f"{whole_text}.{cents_text}"
        reading = spoken_number(literal, lang)
        return f"{reading} 원" if lang == KO else f"{reading} won"
    say_whole = bool(whole) or not cents
    if lang == KO:
        head = f"{korean_sino(whole)} 달러" if say_whole else ""
        tail = f"{korean_sino(cents)} 센트" if cents else ""
        return " ".join(part for part in (head, tail) if part)
    head = f"{english_cardinal(whole)} dollar{'' if whole == 1 else 's'}" if say_whole else ""
    tail = f"{english_cardinal(cents)} cent{'' if cents == 1 else 's'}" if cents else ""
    if head and tail:
        return f"{head} and {tail}"
    return head or tail


def _h_percent(match: re.Match[str], lang: str) -> str:
    literal = match.group().rstrip("%").strip()
    reading = spoken_number(literal, lang)
    return f"{reading} 퍼센트" if lang == KO else f"{reading} percent"


def _h_unit(match: re.Match[str], lang: str) -> str:
    parsed = _RX_VALUE_TAIL.fullmatch(match.group())
    if parsed is None:
        return match.group()
    literal, token = parsed.group(1), parsed.group(2).strip()
    singular, plural, korean = _UNITS[token]
    reading = spoken_number(literal, lang)
    if lang == KO:
        return f"{reading} {korean}"
    exact_one = literal.replace(",", "") in ("1", "1.0")
    return f"{reading} {singular if exact_one else plural}"


def _h_ko_counter(match: re.Match[str], lang: str) -> str:
    """Korean counters are read in Korean whatever the sentence's language.

    The counter itself is a Korean word, so an English reading of the number
    in front of it ("three 개") is not a reading anyone would want; F-05's
    per-sentence language decides the voice, not the arithmetic.
    """
    parsed = _RX_VALUE_TAIL.fullmatch(match.group())
    if parsed is None:
        return match.group()
    literal, counter = parsed.group(1), parsed.group(2).strip()
    native, spaced = _KO_COUNTERS[counter]
    digits = literal.replace(",", "")
    if counter == "월" and "." not in digits and 1 <= int(digits) <= 12:
        return _KO_MONTHS[int(digits)]
    reading: str | None = None
    if native and "." not in digits:
        reading = korean_native(int(digits))
    if reading is None:
        reading = spoken_number(literal, KO)
    return f"{reading} {counter}" if spaced else f"{reading}{counter}"


def _h_number_range(match: re.Match[str], lang: str) -> str:
    parsed = _RX_RANGE.fullmatch(match.group())
    if parsed is None:
        return match.group()
    low, high = (spoken_number(g, lang) for g in parsed.groups())
    return f"{low}에서 {high}" if lang == KO else f"{low} to {high}"


def _h_abbrev(match: re.Match[str], lang: str) -> str:
    english, korean = _ABBREVIATIONS[match.group()]
    return korean if lang == KO else english


def _h_number(match: re.Match[str], lang: str) -> str:
    return spoken_number(match.group(), lang)


def _h_decorative(match: re.Match[str], lang: str) -> str:
    """A.3: not sent for synthesis, but the source range survives.

    The run collapses to nothing, except between two non-space characters,
    where it collapses to a single space -- otherwise "hello🎉world" would
    reach the engine as one invented word.
    """
    text = match.string
    before = text[match.start() - 1] if match.start() else ""
    after = text[match.end()] if match.end() < len(text) else ""
    if before and after and not before.isspace() and not after.isspace():
        return " "
    return ""


def _h_space(match: re.Match[str], lang: str) -> str:
    """One space, whatever the run contained.

    Line breaks must not reach the engine, and a paragraph break is expressed
    as silence between segments (F-82, F-08) rather than as characters.
    """
    return " "


_HANDLERS: Final[dict[str, Callable[[re.Match[str], str], str]]] = {
    "iso_date": _h_iso_date,
    "en_date": _h_en_date,
    "currency": _h_currency,
    "percent": _h_percent,
    "unit": _h_unit,
    "ko_counter": _h_ko_counter,
    "number_range": _h_number_range,
    "abbrev": _h_abbrev,
    "decimal": _h_number,
    "integer": _h_number,
    "decorative": _h_decorative,
    "space": _h_space,
}


# ======================================================================
# The alignment
# ======================================================================


@dataclass(frozen=True, slots=True)
class Piece:
    """One entry of the edit script: a source span and what it is read as."""

    source: TextRange
    text: str

    @property
    def is_identity(self) -> bool:
        return len(self.text) == self.source.length


@dataclass(frozen=True, slots=True)
class Normalized:
    """Normalised text plus the alignment F-27 requires.

    ``pieces`` tiles ``source`` exactly: consecutive, no gaps, no overlaps,
    covering ``[0, len(source))``.  ``text`` is their concatenation.  All
    offsets are Unicode code points into the string that was normalised, and
    a caller normalising a slice of a document adds that slice's own offset.
    """

    source: str
    text: str
    pieces: tuple[Piece, ...]
    # Index arrays for the two mappings, derived in __post_init__ (which is
    # also where the tiling is verified: a gap or an overlap is a defect in a
    # rule, and F-27 makes it one worth crashing on rather than shipping).
    _source_starts: tuple[int, ...] = field(default=(), init=False, repr=False, compare=False)
    _produced_starts: tuple[int, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        cursor = 0
        produced = 0
        source_starts: list[int] = []
        produced_starts: list[int] = []
        for piece in self.pieces:
            if piece.source.start != cursor:
                raise AssertionError(
                    f"alignment is not a tiling at {piece.source.start} (expected {cursor})"
                )
            source_starts.append(cursor)
            produced_starts.append(produced)
            cursor = piece.source.end
            produced += len(piece.text)
        if cursor != len(self.source):
            raise AssertionError(f"alignment covers {cursor} of {len(self.source)} code points")
        if produced != len(self.text):
            # Lengths, not contents: the offsets are all the mappings use,
            # and rebuilding the joined string here would double the cost of
            # normalising a 50,000-character document to catch nothing that
            # this module can produce.
            raise AssertionError("alignment pieces do not span the normalised text")
        object.__setattr__(self, "_source_starts", tuple(source_starts))
        object.__setattr__(self, "_produced_starts", tuple(produced_starts))

    # -- mapping ------------------------------------------------------

    def source_offset(self, produced_offset: int) -> int:
        """Where in the *source* a produced offset came from.

        Inside a piece that changed length there is no character-level truth
        to report, so the piece's own start is returned: a highlight may cover
        one character too many, but it can never point outside the span that
        produced the audio.
        """
        if produced_offset <= 0:
            return 0
        if produced_offset >= len(self.text):
            return len(self.source)
        index = bisect_right(self._produced_starts, produced_offset) - 1
        piece = self.pieces[index]
        within = produced_offset - self._produced_starts[index]
        if piece.is_identity:
            return piece.source.start + within
        return piece.source.start

    def produced_offset(self, source_offset: int) -> int:
        """The inverse.  Offsets inside a rewritten piece floor to its start."""
        if source_offset <= 0:
            return 0
        if source_offset >= len(self.source):
            return len(self.text)
        index = bisect_right(self._source_starts, source_offset) - 1
        piece = self.pieces[index]
        within = source_offset - piece.source.start
        if piece.is_identity:
            return self._produced_starts[index] + within
        return self._produced_starts[index]

    def spoken_between(self, start: int, end: int) -> str:
        """The normalised text produced by source range ``[start, end)``."""
        return self.text[self.produced_offset(start) : self.produced_offset(end)]

    def source_range_of(self, produced: TextRange) -> TextRange:
        return TextRange(self.source_offset(produced.start), self.source_offset(produced.end))

    @property
    def boundaries(self) -> tuple[int, ...]:
        """Source offsets a caller may cut at without splitting a piece."""
        return self._source_starts + (len(self.source),)


def normalize(text: str, lang: str = EN) -> Normalized:
    """Expand numbers, dates, currency, and abbreviations for one language.

    F-27.  ``lang`` is the engine language F-05 resolved for this sentence;
    it selects the reading, never which characters exist.
    """
    pieces: list[Piece] = []
    out: list[str] = []
    cursor = 0
    for match in _MASTER.finditer(text):
        start, end = match.span()
        if start > cursor:
            _append(pieces, out, cursor, start, text[cursor:start])
        # Exactly one rule group can participate in a match, so the last
        # named group that matched is the rule that fired.
        name = match.lastgroup or ""
        _append(pieces, out, start, end, _HANDLERS[name](match, lang))
        cursor = end
    if cursor < len(text):
        _append(pieces, out, cursor, len(text), text[cursor:])
    return Normalized(source=text, text="".join(out), pieces=tuple(pieces))


def _append(pieces: list[Piece], out: list[str], start: int, end: int, produced: str) -> None:
    pieces.append(Piece(TextRange(start, end), produced))
    out.append(produced)


def is_decorative(ch: str) -> bool:
    """Whether one code point is emoji or decorative, per A.3."""
    cp = ord(ch)
    return (
        _in_ranges(cp, _DECORATIVE_RANGES)
        or 0x1F1E6 <= cp <= 0x1F1FF
        or 0x1F3FB <= cp <= 0x1F3FF
        or cp in (0x200D, 0x20E3)
    )


__all__ = [
    "AMBIGUOUS_ABBREVIATIONS",
    "NON_TERMINAL_ABBREVIATIONS",
    "Normalized",
    "Piece",
    "english_cardinal",
    "english_ordinal",
    "english_year",
    "grapheme_boundaries",
    "grapheme_clusters",
    "is_decorative",
    "is_grapheme_boundary",
    "korean_native",
    "korean_sino",
    "normalize",
    "spoken_number",
]

"""Display language, per F-86.

Korean or English, defaulting to the operating system's language and falling
back to English.  It is independent of the narration language in F-05 and of
the source text, and A-22 requires that switching it leaves documents, job
snapshots, and API responses unchanged -- which is why nothing outside
``echoact.ui`` imports this module.  Error text on the wire comes from
``echoact.errors``, in English, always.

Strings are keyed by their English text rather than by an identifier.  That
keeps a call site readable, makes a missing translation degrade to correct
English instead of to a symbol, and means no registry to keep in step.  Where
two English strings coincide but their Korean does not, pass a ``context``.

Each UI module registers its own strings with ``add_korean`` from its own
file, so the catalogue never becomes one contended table.
"""

from __future__ import annotations

import locale
import os
from collections.abc import Callable, Mapping
from enum import StrEnum


class Lang(StrEnum):
    EN = "en"
    KO = "ko"
    SYSTEM = "system"


_KO: dict[str, str] = {}
_CURRENT: Lang = Lang.EN
_LISTENERS: list[Callable[[Lang], None]] = []


def detect_system_language() -> Lang:
    """Korean if the OS says so, English otherwise.

    Checked in the order a user would expect to win: an explicit override,
    then the process locale, then the OS default.  Anything unrecognised is
    English, because F-86 names English as the fallback rather than the
    nearest match.
    """
    for value in (
        os.environ.get("ECHOACT_LANG"),
        os.environ.get("LANG"),
        os.environ.get("LC_ALL"),
    ):
        if value and value.lower().startswith("ko"):
            return Lang.KO
        if value and value.lower().startswith("en"):
            return Lang.EN
    try:
        code = locale.getlocale()[0] or ""
    except ValueError:
        code = ""
    if code.lower().startswith(("ko", "korean")):
        return Lang.KO
    return Lang.EN


def set_language(lang: Lang) -> Lang:
    """Switch the display language and notify listeners.  Returns the
    resolved language, so a caller storing ``SYSTEM`` still learns which
    one is in force."""
    global _CURRENT
    _CURRENT = detect_system_language() if lang is Lang.SYSTEM else lang
    for fn in list(_LISTENERS):
        fn(_CURRENT)
    return _CURRENT


def current() -> Lang:
    return _CURRENT


def on_change(fn: Callable[[Lang], None]) -> None:
    """Register a retranslate callback.  Widgets rebuild their text here
    rather than being recreated, so the switch keeps the user's place."""
    _LISTENERS.append(fn)


def add_korean(entries: Mapping[str, str], context: str | None = None) -> None:
    """Contribute translations.  Called at import time by each UI module."""
    prefix = f"{context}\x00" if context else ""
    for source, korean in entries.items():
        _KO[prefix + source] = korean


def tr(text: str, context: str | None = None) -> str:
    if _CURRENT is not Lang.KO:
        return text
    if context:
        found = _KO.get(f"{context}\x00{text}")
        if found is not None:
            return found
    return _KO.get(text, text)


def untranslated() -> list[str]:
    """Strings the Korean catalogue does not cover.

    Not used at runtime; a test asserts this is empty for the strings the
    app actually shows, so a new label cannot ship English-only by accident.
    """
    return sorted(k for k, v in _KO.items() if not v)


def plural(n: int, singular: str, plural_form: str) -> str:
    """English pluralises, Korean does not.

    Written as one helper so no call site grows an ``if language ==`` of its
    own; Korean returns the singular because Hangul marks number on the
    noun only when it matters, and "1개의 문장" style counting reads wrong
    for a running status line.
    """
    if _CURRENT is Lang.KO:
        return singular
    return singular if n == 1 else plural_form


def duration(ms: int) -> str:
    """m:ss, or h:mm:ss past an hour.  Identical in both languages, because
    a timecode is not prose."""
    total = max(0, ms) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def approximate_duration(ms: int) -> str:
    """A rough length for an estimate, where a timecode would imply more
    precision than F-88 promises."""
    total = max(0, ms) // 1000
    if total < 60:
        return tr("about {n} seconds").format(n=total)
    minutes = round(total / 60)
    if minutes < 60:
        return tr("about {n} min").format(n=minutes)
    hours = total / 3600
    return tr("about {n} h").format(n=f"{hours:.1f}")


def count(n: int) -> str:
    """Thousands separators.  Korean uses the same grouping as English."""
    return f"{n:,}"


def bytes_size(n: int) -> str:
    """Storage in GB (10^9), per Section 4.1's units rule."""
    if n < 1000:
        return f"{n} B"
    for unit, div in (("GB", 10**9), ("MB", 10**6), ("kB", 10**3)):
        if n >= div:
            value = n / div
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{n} B"


def memory_size(n: int) -> str:
    """Memory in GiB (2^30), per Section 4.1's units rule.  Deliberately a
    different function from ``bytes_size``: the document uses different
    units for storage and memory and conflating them misreports both."""
    gib = n / (1 << 30)
    if gib >= 1:
        return f"{gib:.1f} GiB"
    return f"{n / (1 << 20):.0f} MiB"


# The strings shared across the app.  Module-specific text is registered by
# the module that shows it.
add_korean(
    {
        # identity and chrome
        "EchoAct": "에코액트",
        "Library": "라이브러리",
        "Models": "모델",
        "Settings": "설정",
        "Help": "도움말",
        "Close": "닫기",
        "Cancel": "취소",
        "Save": "저장",
        "Delete": "삭제",
        "Open": "열기",
        "Done": "완료",
        "Retry": "다시 시도",
        "Continue": "계속",
        "Back": "뒤로",
        "Copy": "복사",
        "Discard": "저장하지 않음",
        # reading and playback
        "Read aloud": "소리내어 읽기",
        "Play": "재생",
        "Pause": "일시정지",
        "Stop": "정지",
        "Stop reading": "읽기 중지",
        "Previous segment": "이전 문장",
        "Next segment": "다음 문장",
        "Return to the reading position": "읽는 위치로 이동",
        "Play as soon as ready": "준비되는 즉시 재생",
        "Follow the reading position": "읽는 위치 따라가기",
        "Highlighting unavailable": "강조 표시를 사용할 수 없음",
        "The text no longer matches the audio.": "본문이 오디오와 더 이상 일치하지 않습니다.",
        "Waiting for the next segment": "다음 문장을 기다리는 중",
        "Paused": "일시정지됨",
        "Playing": "재생 중",
        "Stopped": "정지됨",
        # generation
        "Preparing the model": "모델 준비 중",
        "Loading the model": "모델 불러오는 중",
        "Generating": "생성 중",
        "Complete": "완료됨",
        "Canceled": "취소됨",
        "Interrupted": "중단됨",
        "Failed": "실패",
        "Cancel generation": "생성 취소",
        # voice settings
        "Voice": "음성",
        "Model": "모델",
        "Language": "언어",
        "Automatic": "자동",
        "Korean": "한국어",
        "English": "영어",
        "Gender": "성별",
        "Female": "여성",
        "Male": "남성",
        "Style": "말하기 스타일",
        "Natural": "자연스럽게",
        "Calm": "차분하게",
        "Bright": "밝게",
        "Narration": "내레이션",
        "Tempo": "속도",
        "Presets": "사전 설정",
        # input
        "{n} / {max} characters": "{n} / {max}자",
        "Open a text file": "텍스트 파일 열기",
        "Unsaved text": "저장하지 않은 본문",
        "Replace the current text?": "현재 본문을 바꿀까요?",
        # resources and service
        "CPU": "CPU",
        "Memory": "메모리",
        "Resource budget": "자원 한도",
        "Local service": "로컬 서비스",
        "on": "켜짐",
        "off": "꺼짐",
        "Integrations unavailable": "연동을 사용할 수 없음",
        # measurements
        "about {n} seconds": "약 {n}초",
        "about {n} min": "약 {n}분",
        "about {n} h": "약 {n}시간",
        "{n} segments": "{n}개 문장",
        "1 segment": "문장 1개",
    }
)

_CURRENT = detect_system_language()

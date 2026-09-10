"""The vocabulary every layer shares: states, settings, and the four entities
Section 4.2 manages.

These types are deliberately free of I/O.  The database stores them, the REST
layer projects them, the GUI renders them, and the engine produces them, but
none of those concerns appear here.  That is what lets N-24 hold: one job
state machine, one segment shape, one definition of what a result is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .errors import Code, EchoActError
from .policy import TEMPO_MAX, TEMPO_MIN

# ======================================================================
# Enumerations
# ======================================================================


class JobKind(StrEnum):
    """F-54's discriminator.  Exactly one value is accepted in this release.

    It exists so that a second kind is an additive change rather than a
    breaking one; see A.3.  Nothing in the product hints at what the second
    value might be.
    """

    SPEECH = "speech"


class JobState(StrEnum):
    """Section 5.1.  ``Complete`` also implies any requested retention succeeded."""

    ACCEPTED = "accepted"
    PREPARING_MODEL = "preparing_model"
    GENERATING = "generating"
    CANCELING = "canceling"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        """True while the job may still be holding the generation slot."""
        return self in _ACTIVE_STATES


_TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETE, JobState.FAILED, JobState.CANCELED, JobState.INTERRUPTED}
)
_ACTIVE_STATES: frozenset[JobState] = frozenset(
    {
        JobState.ACCEPTED,
        JobState.PREPARING_MODEL,
        JobState.GENERATING,
        JobState.CANCELING,
    }
)

# Section 5.1's legal transitions.  Anything absent is a programming error,
# not a runtime condition, and ``check_transition`` says so loudly.
_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.ACCEPTED: frozenset(
        {JobState.PREPARING_MODEL, JobState.GENERATING, JobState.CANCELING,
         JobState.FAILED, JobState.INTERRUPTED}
    ),
    JobState.PREPARING_MODEL: frozenset(
        {JobState.GENERATING, JobState.CANCELING, JobState.FAILED, JobState.INTERRUPTED}
    ),
    JobState.GENERATING: frozenset(
        {JobState.COMPLETE, JobState.CANCELING, JobState.FAILED, JobState.INTERRUPTED}
    ),
    # "If cancellation and completion race, whichever terminal state is
    # confirmed first is kept.  Completion is never overwritten by Canceled."
    JobState.CANCELING: frozenset({JobState.CANCELED, JobState.COMPLETE, JobState.FAILED}),
    JobState.COMPLETE: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELED: frozenset(),
    JobState.INTERRUPTED: frozenset(),
}


def can_transition(current: JobState, nxt: JobState) -> bool:
    return nxt in _TRANSITIONS[current]


def check_transition(current: JobState, nxt: JobState) -> None:
    if not can_transition(current, nxt):
        raise AssertionError(f"illegal job transition {current.value} -> {nxt.value}")


class PlaybackState(StrEnum):
    """Section 5.2."""

    NOT_READY = "not_ready"
    PLAYING = "playing"
    PAUSED = "paused"
    WAITING_FOR_SEGMENT = "waiting_for_segment"
    STOPPED = "stopped"
    ENDED = "ended"


class RequestPath(StrEnum):
    """Which surface asked for the job.  F-69 shows it; F-50 uses it for
    ownership, though never for priority -- see A.3."""

    GUI = "gui"
    REST = "rest"
    MCP = "mcp"


class Language(StrEnum):
    """F-05.  ``AUTO`` resolves per sentence on whether it contains Hangul."""

    AUTO = "auto"
    KO = "ko"
    EN = "en"


class Gender(StrEnum):
    FEMALE = "female"
    MALE = "male"


class SpeakingStyle(StrEnum):
    """F-08.  A preset over tempo, inter-segment pause, and whatever
    expression controls the model exposes.  Never changes which characters
    are spoken."""

    NATURAL = "natural"
    CALM = "calm"
    BRIGHT = "bright"
    NARRATION = "narration"


class Capability(StrEnum):
    """F-61's separately grantable permissions.  A credential carries a set
    of these; the MCP server's effective permissions are exactly its REST
    credential's, so revocation is one mechanism."""

    GENERATE = "generate"
    READ_RESULTS = "read_results"
    READ_HISTORY = "read_history"
    OWNER = "owner"


class RetentionMode(StrEnum):
    """F-42.  A one-off job's body text and audio never enter permanent
    history; 4.2 fixes what each keeps."""

    ONE_OFF = "one_off"
    RETAINED = "retained"


# ======================================================================
# Voice settings
# ======================================================================


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    """One fixed set of settings.  A job freezes these at creation; F-10
    forbids changing them mid-job."""

    model_id: str
    language: Language
    gender: Gender
    voice_id: str
    style: SpeakingStyle
    tempo: float

    def validate(self) -> None:
        if not (TEMPO_MIN - 1e-9 <= self.tempo <= TEMPO_MAX + 1e-9):
            raise EchoActError(
                Code.TEMPO_OUT_OF_RANGE,
                detail={"tempo": self.tempo, "min": TEMPO_MIN, "max": TEMPO_MAX},
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "language": self.language.value,
            "gender": self.gender.value,
            "voice_id": self.voice_id,
            "style": self.style.value,
            "tempo": round(self.tempo, 3),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VoiceSettings:
        return cls(
            model_id=d["model_id"],
            language=Language(d["language"]),
            gender=Gender(d["gender"]),
            voice_id=d["voice_id"],
            style=SpeakingStyle(d["style"]),
            tempo=float(d["tempo"]),
        )

    def with_(self, **kw: Any) -> VoiceSettings:
        return replace(self, **kw)


@dataclass(frozen=True, slots=True)
class Budget:
    """The resource ceiling actually applied to a job.

    F-78 distinguishes the value on screen from the value in force, so a job
    records the one it ran under and the GUI shows both.
    """

    cpu_percent: int
    memory_bytes: int
    #: Worker threads derived from ``cpu_percent`` per F-87, never left to
    #: the runtime's own default.  A.5 records that capping is also faster.
    intra_op_threads: int
    inter_op_threads: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_percent": self.cpu_percent,
            "memory_bytes": self.memory_bytes,
            "intra_op_threads": self.intra_op_threads,
            "inter_op_threads": self.inter_op_threads,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Budget:
        return cls(
            cpu_percent=int(d["cpu_percent"]),
            memory_bytes=int(d["memory_bytes"]),
            intra_op_threads=int(d["intra_op_threads"]),
            inter_op_threads=int(d.get("inter_op_threads", 1)),
        )


# ======================================================================
# Text ranges
# ======================================================================


@dataclass(frozen=True, slots=True)
class TextRange:
    """A half-open span of the job's source text, in Unicode code points.

    4.2 is explicit that these are code-point offsets, start-inclusive and
    end-exclusive, and that they are never conflated with UI indices.  Qt
    counts UTF-16 units, so the reading surface converts at its boundary and
    nowhere else.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise AssertionError(f"degenerate text range [{self.start}, {self.end})")

    @property
    def length(self) -> int:
        return self.end - self.start

    def slice(self, text: str) -> str:
        return text[self.start : self.end]


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Milliseconds from the start of the job's audio (4.2)."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms < self.start_ms:
            raise AssertionError(f"degenerate time range [{self.start_ms}, {self.end_ms})")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def contains(self, ms: int) -> bool:
        return self.start_ms <= ms < self.end_ms


# ======================================================================
# Entities (Section 4.2)
# ======================================================================


@dataclass(slots=True)
class Segment:
    """The smallest unit linking audio to source text.

    ``source`` always refers to the *original* text the user entered, never to
    the normalised form sent for synthesis -- F-27 makes that a correctness
    requirement, not a nicety.  ``spoken_text`` is what the engine received;
    it may be empty for a segment that carries only unspoken characters.
    """

    index: int
    source: TextRange
    #: Normalised text handed to the engine.  Empty means nothing is spoken
    #: for this range; the range is still highlighted, per F-27.
    spoken_text: str
    language: str
    #: Filled once generated.  Times are relative to the job's full audio.
    time: TimeRange | None = None
    #: Silence the app appends after this segment, per F-82 and F-08.  It is
    #: attributed to the preceding segment, so it is inside ``time``.
    trailing_silence_ms: int = 0
    audio_path: str | None = None
    frame_count: int = 0
    ready: bool = False
    segment_id: str = ""

    @property
    def is_spoken(self) -> bool:
        return bool(self.spoken_text.strip())


@dataclass(slots=True)
class Result:
    """4.2.  Addressed by a permission-checkable identifier, never a path."""

    result_id: str
    job_id: str
    sample_rate: int
    channels: int
    sample_width_bits: int
    frame_count: int
    byte_size: int
    #: SHA-256 of the WAV bytes.  4.2 calls this integrity verification data.
    digest: str
    created_at: float
    expires_at: float | None
    #: Relative to the app's audio directory; never handed to a client.
    relative_path: str = ""

    @property
    def duration_ms(self) -> int:
        return int(round(self.frame_count * 1000 / self.sample_rate))


@dataclass(slots=True)
class Job:
    """One generation request against one fixed text and one fixed setting."""

    job_id: str
    kind: JobKind
    request_path: RequestPath
    owner_client_id: str
    state: JobState
    #: The frozen source text.  F-29 compares the live input against this;
    #: a one-off job clears it once it reaches a terminal state and its
    #: retention window closes.
    source_text: str
    settings: VoiceSettings
    budget: Budget | None
    retention: RetentionMode
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: Set only for a job created by an integration; F-70 names the client
    #: in the completion notice.
    client_label: str | None = None
    idempotency_key: str | None = None
    segments: list[Segment] = field(default_factory=list)
    result: Result | None = None
    #: 0.0 to 1.0 over segments generated, which is generation progress and
    #: not playback progress -- Section 1.2 keeps those separate.
    generated_segments: int = 0
    total_segments: int = 0

    @property
    def progress(self) -> float:
        if self.total_segments <= 0:
            return 0.0
        return min(1.0, self.generated_segments / self.total_segments)

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def ready_segments(self) -> list[Segment]:
        return [s for s in self.segments if s.ready]

    def playable_ms(self) -> int:
        """How much audio can be played right now.

        F-14 forbids seeking past this, and F-12's player waits here rather
        than at the end of the document.
        """
        ready = self.ready_segments()
        return ready[-1].time.end_ms if ready and ready[-1].time else 0


@dataclass(slots=True)
class Document:
    """4.2.  Editing a document never touches an existing job's snapshot."""

    document_id: str
    title: str
    body: str
    created_at: float
    modified_at: float
    version: int = 1


# ======================================================================
# Helpers used by more than one layer
# ======================================================================

_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")


def contains_hangul(text: str) -> bool:
    """F-05's automatic-mode test, in one place so the GUI, the segmenter,
    and the estimator cannot disagree about what a Korean sentence is."""
    return _HANGUL.search(text) is not None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


__all__ = [
    "Budget",
    "Capability",
    "Document",
    "Gender",
    "Job",
    "JobKind",
    "JobState",
    "Language",
    "PlaybackState",
    "RequestPath",
    "RetentionMode",
    "Result",
    "Segment",
    "SpeakingStyle",
    "TextRange",
    "TimeRange",
    "VoiceSettings",
    "can_transition",
    "check_transition",
    "clamp",
    "contains_hangul",
]

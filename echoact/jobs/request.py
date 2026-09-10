"""What a generation request is, and what makes one valid.

Every entry path validates here.  F-37 requires the GUI, REST, and MCP to
apply the same limits and the same support policy, and the cheapest way to
guarantee that is to give them one function rather than three that agree
today.

Nothing in this module touches the engine, the database, or the disk: it
answers "would this be accepted?", which is exactly what F-88's pre-flight
estimate needs and what the job engine needs before it takes the slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain import (
    Gender,
    JobKind,
    Language,
    RequestPath,
    RetentionMode,
    Segment,
    SpeakingStyle,
    VoiceSettings,
)
from ..errors import Code, EchoActError
from ..models.manifest import Manifest, ModelEntry
from ..policy import (
    BOUNDED_WAIT_CEILING_S,
    ESTIMATE_REAL_TIME_FACTOR,
    MAX_INPUT_CODEPOINTS,
)
from ..text.segment import estimate_seconds, segment_text
from ..util import ids


@dataclass(frozen=True, slots=True)
class JobRequest:
    """One accepted-or-refused generation request.

    ``idempotency_key`` is not optional.  A.3 records why: with a single
    generation slot, a client that retries after a slow or dropped response
    would otherwise queue a second rendering of the same text behind the
    first, and automated clients retry as a matter of course.  The GUI mints
    one per press for the same reason -- a double-click is a retry too.
    """

    text: str
    settings: VoiceSettings
    request_path: RequestPath
    owner_client_id: str
    idempotency_key: str
    kind: JobKind = JobKind.SPEECH
    retention: RetentionMode = RetentionMode.ONE_OFF
    client_label: str | None = None
    #: F-88.  ``None`` means answer as soon as the job is accepted.
    wait_s: float | None = None

    def digest(self) -> str:
        """F-49's request-match discriminator.

        Hashed rather than stored: 4.2 keeps "only the information needed
        for duplicate prevention ... with no source text", so the record
        must be able to tell "same content" from "different content"
        without holding the content.
        """
        from ..db.store import request_match_digest

        return request_match_digest(
            self.kind.value,
            self.text,
            repr(sorted(self.settings.to_dict().items())),
            self.retention.value,
        )


@dataclass(frozen=True, slots=True)
class Estimate:
    """F-88's pre-flight answer.

    Explicitly approximate, and derived from the throughput A.5 measured
    rather than from a trial synthesis: the requirement says an estimate
    must not create a job or load a model.
    """

    valid: bool
    codepoints: int
    segment_count: int
    audio_ms: int
    synthesis_ms: int
    first_audio_ms: int
    slot_free: bool
    model_ready: bool
    approximate: bool = True
    problems: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "codepoints": self.codepoints,
            "segment_count": self.segment_count,
            "audio_ms": self.audio_ms,
            "synthesis_ms": self.synthesis_ms,
            "first_audio_ms": self.first_audio_ms,
            "slot_free": self.slot_free,
            "model_ready": self.model_ready,
            "approximate": self.approximate,
            "problems": list(self.problems),
        }


def validate_text(text: str) -> str:
    """F-03.  Returns the text unchanged when it is acceptable.

    Whitespace-only input is refused rather than trimmed: A.5 found the
    engine raising on it, so this is the guard the requirement says it is,
    and trimming would quietly change the offsets every segment range is
    measured against.
    """
    if not text or not text.strip():
        raise EchoActError(Code.INPUT_EMPTY)
    if len(text) > MAX_INPUT_CODEPOINTS:
        raise EchoActError(
            Code.INPUT_TOO_LONG,
            detail={"codepoints": len(text), "limit": MAX_INPUT_CODEPOINTS},
        )
    return text


def validate_settings(settings: VoiceSettings, manifest: Manifest) -> ModelEntry:
    """F-04 through F-08.

    Nothing is substituted.  F-54 is explicit that unknown models and
    options are not arbitrarily replaced, so every mismatch is an error
    naming what was wrong rather than a silent correction.
    """
    settings.validate()  # tempo range, F-07

    if not manifest.has(settings.model_id):
        raise EchoActError(
            Code.MODEL_UNKNOWN,
            detail={"model_id": settings.model_id, "known": list(manifest.model_ids)},
        )
    entry = manifest.get(settings.model_id)

    if settings.language is not Language.AUTO and not entry.supports_language(settings.language):
        raise EchoActError(
            Code.LANGUAGE_UNKNOWN,
            detail={
                "language": settings.language.value,
                "supported": [lang.value for lang in entry.languages],
            },
        )
    if not isinstance(settings.style, SpeakingStyle):
        raise EchoActError(Code.STYLE_UNKNOWN, detail={"style": str(settings.style)})

    voice = next((v for v in entry.voices if v.voice_id == settings.voice_id), None)
    if voice is None:
        raise EchoActError(
            Code.VOICE_UNKNOWN,
            detail={
                "voice_id": settings.voice_id,
                "available": [v.voice_id for v in entry.voices],
            },
        )
    if voice.gender is not settings.gender:
        # F-06 selects a gender and then a voice within it; a request whose
        # two halves disagree is a mistake to report, not one to resolve by
        # picking a side.
        raise EchoActError(
            Code.VOICE_GENDER_MISMATCH,
            detail={"voice_id": voice.voice_id, "voice_gender": voice.gender.value},
        )
    return entry


def validate_request(request: JobRequest, manifest: Manifest) -> ModelEntry:
    """Everything a request must satisfy before it can take the slot."""
    if request.kind is None:
        raise EchoActError(Code.JOB_KIND_MISSING)
    if not isinstance(request.kind, JobKind):
        raise EchoActError(Code.JOB_KIND_UNKNOWN, detail={"kind": str(request.kind)})
    if not request.idempotency_key or not request.idempotency_key.strip():
        raise EchoActError(Code.IDEMPOTENCY_KEY_MISSING)
    if request.wait_s is not None and request.wait_s < 0:
        raise EchoActError(
            Code.INTERNAL, "A wait cannot be negative.", detail={"wait_s": request.wait_s}
        )
    validate_text(request.text)
    return validate_settings(request.settings, manifest)


def clamp_wait(wait_s: float | None, *, ceiling_s: float = BOUNDED_WAIT_CEILING_S) -> float:
    """4.1's bounded wait.

    The service never waits longer than the caller asked *and* never longer
    than the owner's ceiling, so this takes the smaller of the two rather
    than substituting the default for an out-of-range value.
    """
    if wait_s is None:
        return 0.0
    return max(0.0, min(float(wait_s), ceiling_s))


def plan_segments(text: str, settings: VoiceSettings) -> list[Segment]:
    """F-81, with fresh identifiers so the plan can be stored as-is."""
    segments = segment_text(text, settings)
    for seg in segments:
        if not seg.segment_id:
            seg.segment_id = ids.segment_id()
    return segments


def estimate(
    text: str,
    settings: VoiceSettings,
    manifest: Manifest,
    *,
    slot_free: bool,
    model_ready: bool,
    real_time_factor: float = ESTIMATE_REAL_TIME_FACTOR,
) -> Estimate:
    """F-88's pre-flight estimate.

    Answers even when the request is invalid: a caller asking "would this
    work?" is best served by the reasons it would not, all of them, rather
    than by the first exception.  That is also why validation problems are
    collected instead of raised.
    """
    problems: list[dict[str, Any]] = []
    for check in (
        lambda: validate_text(text),
        lambda: validate_settings(settings, manifest),
    ):
        try:
            check()
        except EchoActError as exc:
            problems.append({"code": exc.code.value, "message": exc.message, **exc.detail})

    if problems:
        return Estimate(
            valid=False,
            codepoints=len(text),
            segment_count=0,
            audio_ms=0,
            synthesis_ms=0,
            first_audio_ms=0,
            slot_free=slot_free,
            model_ready=model_ready,
            problems=tuple(problems),
        )

    segments = segment_text(text, settings)
    tempo = settings.tempo
    audio_s = 0.0
    first_s = 0.0
    for i, seg in enumerate(segments):
        seconds = estimate_seconds(seg.spoken_text, seg.language, tempo)
        seconds += seg.trailing_silence_ms / 1000.0
        audio_s += seconds
        if i == 0:
            first_s = seconds

    synthesis_s = audio_s * real_time_factor
    # Time to the first sound, which is what F-12 makes the interesting
    # number: the first segment has to be synthesised before anything plays,
    # and a model that is not resident has to be loaded first.  The load is
    # not included here -- F-88 says an estimate never waits on model
    # preparation, so quoting it would describe a different operation.
    first_audio_s = first_s * real_time_factor

    return Estimate(
        valid=True,
        codepoints=len(text),
        segment_count=len(segments),
        audio_ms=int(round(audio_s * 1000)),
        synthesis_ms=int(round(synthesis_s * 1000)),
        first_audio_ms=int(round(first_audio_s * 1000)),
        slot_free=slot_free,
        model_ready=model_ready,
    )


def resolve_gender_voice(entry: ModelEntry, gender: Gender, voice_id: str | None) -> str:
    """The first voice of a gender, for a caller that named only a gender.

    Used by the GUI when the user switches gender, never to paper over an
    invalid request: F-54 forbids substituting an unknown option, and this
    is only reached when no voice was named at all.
    """
    voices = entry.voices_for(gender)
    if not voices:
        raise EchoActError(Code.VOICE_UNKNOWN, detail={"gender": gender.value})
    if voice_id:
        for v in voices:
            if v.voice_id == voice_id:
                return v.voice_id
    return voices[0].voice_id

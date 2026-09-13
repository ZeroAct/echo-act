"""The wire shapes of Section 2.10's operations.

These models exist for two reasons at once.  They validate what arrives, and
they are what FastAPI turns into the machine-readable specification F-57
requires -- so a field that is absent here is absent from the contract, and
the response models below are the whole of what a client may depend on.

Two rules run through all of them.

* Nothing carries a filesystem path.  4.2 says a result is "queried by a
  permission-checkable identifier rather than an arbitrary path", and F-57
  keeps internal paths out of responses, so audio is addressed by job and
  segment identifier and the bytes arrive from a separate streaming route.
* Enumerated values are the domain's own ``StrEnum`` members.  N-24 makes a
  behaviour difference between the GUI, REST, and MCP a defect, and the
  cheapest way to keep the vocabularies identical is to have one of them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..domain import (
    Gender,
    JobKind,
    JobState,
    Language,
    RequestPath,
    RetentionMode,
    SpeakingStyle,
)
from ..policy import TEMPO_DEFAULT, TEMPO_MAX, TEMPO_MIN

#: Longest duplicate-prevention key accepted.  4.1 scopes the key per client
#: and keeps it for an hour; it is an identifier the caller mints, so the only
#: bound that matters is that it cannot itself become a payload.
MAX_IDEMPOTENCY_KEY_CHARS = 200

def _strict(**extra: Any) -> ConfigDict:
    """The configuration every model in this module shares.

    ``extra="forbid"`` is the request half of F-54's "unknown models or
    options are not arbitrarily substituted".  Dropping an option this
    version does not define substitutes its default just as surely as
    replacing it would: a caller that wrote ``retainn`` gets a one-off job
    whose text and audio 4.1 removes an hour after it ends, a caller that
    wrote ``speed`` gets 1.0x, and neither is told.  Section 2.10 is the
    surface an automated client depends on, so an unrecognised word costs one
    round trip and is named, rather than being silently reinterpreted.
    """
    return ConfigDict(extra="forbid", protected_namespaces=(), **extra)


_STRICT = _strict()


# ======================================================================
# Requests
# ======================================================================


class VoiceIn(BaseModel):
    """F-04 to F-08's settings.

    Every field may be omitted, and what fills the gap is the *manifest's*
    documented default -- never the owner's current GUI selection, which
    would make an automated caller's output depend on what the person at the
    keyboard last clicked.  F-54 still forbids substituting an *unknown*
    model or option: an absent value takes a default, a wrong one is refused.
    An omitted gender is read off the named voice rather than defaulted,
    because a caller that named ``M1`` has already said which gender it
    wanted and a 422 would be pedantry.
    """

    model_config = _STRICT

    model_id: str | None = Field(
        default=None, description="Model identifier from GET /api/v1/models."
    )
    voice_id: str | None = Field(
        default=None, description="Voice identifier belonging to that model."
    )
    gender: Gender | None = Field(
        default=None, description="Must match the named voice's own gender (F-06)."
    )
    language: Language = Field(
        default=Language.AUTO,
        description="'auto' resolves per sentence on whether it contains Hangul.",
    )
    style: SpeakingStyle = Field(default=SpeakingStyle.NATURAL)
    tempo: float = Field(default=TEMPO_DEFAULT, ge=TEMPO_MIN, le=TEMPO_MAX)


_EXAMPLE_VOICE = {
    "model_id": "supertonic-3",
    "voice_id": "F1",
    "gender": "female",
    "language": "auto",
    "style": "narration",
    "tempo": 1.0,
}


class EstimateRequest(BaseModel):
    """F-88's pre-flight body.  Creates no job and loads no model."""

    model_config = _strict(
        json_schema_extra={
            "examples": [
                {"kind": "speech", "text": "안녕하세요. 반갑습니다.", "voice": _EXAMPLE_VOICE}
            ]
        }
    )

    text: str = Field(description="The text that would be spoken.")
    voice: VoiceIn | None = None
    kind: JobKind = Field(default=JobKind.SPEECH)


class CreateJobRequest(BaseModel):
    """F-54's generation request.

    ``idempotency_key`` is required rather than optional: F-49 refuses a
    request without one, because with a single generation slot a client that
    retries after a slow response would otherwise queue a second rendering of
    the same text behind the first.

    ``text`` is absent when the request arrived as a multipart upload; F-54
    makes the two alternatives and forbids both at once.
    """

    model_config = _strict(
        json_schema_extra={
            "examples": [
                {
                    "kind": "speech",
                    "idempotency_key": "2f8c1d5e-report-2026-09-11",
                    "text": "EchoAct reads a document aloud.",
                    "voice": _EXAMPLE_VOICE,
                    "retain": False,
                    "wait_s": 10,
                }
            ]
        },
    )

    kind: JobKind = Field(description="Exactly 'speech' in this release (F-54).")
    idempotency_key: str = Field(
        min_length=1,
        max_length=MAX_IDEMPOTENCY_KEY_CHARS,
        description="Duplicate-prevention key, scoped to this client (F-49).",
    )
    voice: VoiceIn | None = Field(
        default=None,
        description="Voice settings. Omit any part of it to take the manifest default (F-54).",
    )
    text: str | None = Field(
        default=None, description="Inline text. Omit when uploading a file instead."
    )
    retain: bool = Field(
        default=False,
        description="Keep the job, its snapshot, and its audio in history (F-42).",
    )
    wait_s: float | None = Field(
        default=None,
        description=(
            "Seconds to wait for a terminal state before answering (F-88). "
            "Clamped to the service's ceiling; the job is unaffected either way."
        ),
    )
    play: bool = Field(
        default=False,
        description=(
            "Ask for the audio to be played on the host output device as it is "
            "generated (F-89). The job is created either way; whether playback "
            "was accepted is reported in the response's 'playback'."
        ),
    )
    encoding: str | None = Field(
        default=None,
        description="F-34's confirmed encoding, honoured only for an upload whose own is unclear.",
    )
    confirm_unknown: bool = Field(
        default=False,
        description="F-35's confirmation that an upload with no known extension is text.",
    )


# ======================================================================
# Shared projections
# ======================================================================


class ErrorBody(BaseModel):
    """F-57's error shape.  Carries no path, no credential, and no body text."""

    model_config = _STRICT

    code: str
    message: str
    retryable: bool
    request_id: str
    detail: dict[str, Any] | None = None
    retry_after_s: float | None = None


class VoiceOut(BaseModel):
    model_config = _STRICT

    model_id: str
    voice_id: str
    gender: Gender
    language: Language
    style: SpeakingStyle
    tempo: float


class BudgetOut(BaseModel):
    """F-78's "value actually applied", never the value on the owner's screen."""

    model_config = _STRICT

    cpu_percent: int
    memory_bytes: int
    intra_op_threads: int
    inter_op_threads: int


class JobError(BaseModel):
    model_config = _STRICT

    code: str
    message: str | None = None
    retryable: bool


class JobProgress(BaseModel):
    model_config = _STRICT

    generated_segments: int
    total_segments: int
    fraction: float


class PlaybackOut(BaseModel):
    """What became of a playback request (F-89).

    A refusal here is not the job's failure: the job was created, its audio
    is retrievable, and only the speaker was unavailable -- so it is reported
    as a field rather than as the request's status.  ``code`` is the same
    F-57 code the dedicated play endpoint would have answered with, so a
    caller branches on one vocabulary either way.
    """

    model_config = _STRICT

    accepted: bool
    code: str | None = None
    message: str | None = None
    retry_after_s: float | None = None


class JobOut(BaseModel):
    """One job as F-54 and F-56 present it.  Never the source text: F-56 puts
    the snapshot behind its own separately authorised request."""

    model_config = _STRICT

    job_id: str
    kind: JobKind
    state: JobState
    terminal: bool
    request_path: RequestPath
    owner_client_id: str
    client_label: str | None
    retention: RetentionMode
    voice: VoiceOut
    budget: BudgetOut | None
    progress: JobProgress
    created_at: float
    started_at: float | None
    ended_at: float | None
    result_ready: bool
    result_expired: bool
    audio_duration_ms: int
    error: JobError | None = None
    #: Present only on a response to POST /api/v1/jobs.  F-88 says waiting is
    #: an optimisation, so a caller has to be able to tell that it happened
    #: without inferring it from the elapsed time.
    waited_s: float | None = None
    #: True when this response returned an existing job for a repeated
    #: duplicate-prevention key rather than creating one (F-49).
    duplicate: bool | None = None
    #: Present only when playback was asked for (F-89).
    playback: PlaybackOut | None = None


class JobPage(BaseModel):
    """4.1's list page: 20 items by default, 100 at most."""

    model_config = _STRICT

    items: list[JobOut]
    total: int
    limit: int
    offset: int
    has_more: bool


class JobTextOut(BaseModel):
    """F-56's separately authorised snapshot."""

    model_config = _STRICT

    job_id: str
    text: str
    codepoints: int
    retention: RetentionMode


class SegmentOut(BaseModel):
    """F-55's per-segment mapping.

    ``source_start``/``source_end`` are Unicode code points into the job's
    source text, start-inclusive and end-exclusive (4.2), and are never UI
    indices.  Times are milliseconds from the start of the job's audio.
    """

    model_config = _STRICT

    segment_id: str
    index: int
    source_start: int
    source_end: int
    start_ms: int
    end_ms: int
    duration_ms: int
    language: str
    spoken: bool
    has_audio: bool


class SegmentsOut(BaseModel):
    model_config = _STRICT

    job_id: str
    segments: list[SegmentOut]
    #: The index of the last ready segment, or null when none is ready.  A
    #: caller polls with this rather than re-reading the whole list.
    last_sequence: int | None
    ready_count: int
    total_segments: int
    complete: bool


class IntegrityOut(BaseModel):
    """4.2's "integrity verification data", named rather than bare."""

    model_config = _STRICT

    algorithm: str
    value: str
    state: str


class ResultOut(BaseModel):
    """4.2's Result entity, as F-55 hands it to a client."""

    model_config = _STRICT

    job_id: str
    result_id: str
    #: ``echoact.audio.wav.FORMAT_NAME``.  F-82 fixes one output format and
    #: this is its name, rather than a second vocabulary for the same thing.
    format: str
    media_type: str
    sample_rate: int
    channels: int
    sample_width_bits: int
    frame_count: int
    duration_ms: int
    byte_size: int
    integrity: IntegrityOut
    created_at: float
    expires_at: float | None
    expired: bool
    available: bool


class EstimateOut(BaseModel):
    """F-88's answer.  ``approximate`` is always true and says so on the wire."""

    model_config = _STRICT

    valid: bool
    codepoints: int
    segment_count: int
    audio_ms: int
    synthesis_ms: int
    first_audio_ms: int
    slot_free: bool
    model_ready: bool
    approximate: bool
    problems: list[dict[str, Any]]


class VoiceChoice(BaseModel):
    """F-53: a bare identifier gives a caller that is not a person no basis
    for choosing, so the manifest's description travels with it."""

    model_config = _STRICT

    voice_id: str
    display_name: str
    gender: Gender
    description: str


class LicenseOut(BaseModel):
    model_config = _STRICT

    name: str
    acceptance_required: bool
    accepted: bool


class MinimumBudgetOut(BaseModel):
    model_config = _STRICT

    memory_bytes: int
    cpu_percent: int


class ModelOut(BaseModel):
    """F-53's model entry and F-63's row.

    ``state`` distinguishes an undownloaded model from a server fault, which
    F-53 requires outright, and ``unavailable_reason`` carries F-04's reason
    rather than hiding a model that cannot run.
    """

    model_config = _STRICT

    model_id: str
    display_name: str
    state: str
    ready: bool
    runnable: bool
    unavailable_reason: str | None
    sample_rate: int
    languages: list[Language]
    styles: list[SpeakingStyle]
    voices: list[VoiceChoice]
    minimum_budget: MinimumBudgetOut
    bytes_total: int
    bytes_present: int
    license: LicenseOut
    download_authorised: bool


class ModelsOut(BaseModel):
    model_config = _STRICT

    models: list[ModelOut]
    languages: list[Language]
    styles: list[SpeakingStyle]
    genders: list[Gender]
    input_formats: list[str]
    encodings: list[str]
    tempo_min: float
    tempo_max: float
    tempo_default: float


class ResourcePolicyOut(BaseModel):
    """Section 4.1's limits, as the actual numbers in force.

    F-53 asks for "the actual resource policy", which is why the applied
    budget is here beside the fixed limits: the ceilings are constants, but
    what a job may use depends on the machine at this moment (F-21, N-04).
    """

    model_config = _STRICT

    max_input_codepoints: int
    max_upload_bytes: int
    max_request_body_bytes: int
    concurrent_generation: int
    generation_requests_per_minute: int
    other_requests_per_minute: int
    list_page_default: int
    list_page_max: int
    bounded_wait_default_s: float
    bounded_wait_max_s: float
    one_off_result_ttl_s: float
    idempotency_ttl_s: float
    tempo_min: float
    tempo_max: float
    applied_budget: BudgetOut | None
    budget_unavailable_reason: str | None


class CapabilitiesOut(BaseModel):
    model_config = _STRICT

    granted: list[str]
    job_kinds: list[JobKind]
    languages: list[Language]
    styles: list[SpeakingStyle]
    input_formats: list[str]
    bounded_wait: bool
    estimate: bool
    upload: bool
    history: bool
    results: bool
    generation: bool
    #: F-89: whether the owner allows a client to play on the host speakers.
    external_play: bool


class GenerationStateOut(BaseModel):
    """F-47's single slot, as a caller needs to see it before asking."""

    model_config = _STRICT

    busy: bool
    #: Set only when the running job belongs to this caller; N-19 keeps
    #: another owner's job identifier out of a status response.
    current_job_id: str | None
    current_job_is_mine: bool


class StatusOut(BaseModel):
    model_config = _STRICT

    service: str
    version: str
    api_version: str
    state: str
    started_at: float
    uptime_s: float
    bind_host: str
    bind_port: int
    mcp_enabled: bool
    client_id: str
    client_name: str
    credential_expires_at: float
    capabilities: CapabilitiesOut
    resource_policy: ResourcePolicyOut
    generation: GenerationStateOut

"""Section 2.10's twelve operations.  Twelve, and no thirteenth.

The contract table is the public functional surface, so this module registers
exactly the paths it lists.  Anything a client might also find useful --
deleting a job, editing a document, querying the database -- is absent on
purpose: F-56 says external document editing, deletion, and full-database
queries are not supported, and F-61 says MCP adds no capability REST does not
already expose, which only means anything if REST's own surface is closed.

Two constraints shape how the handlers are written.

* **N-22.** Acceptance, query, cancellation, and estimate answer within one
  second at p95 and independently of model loading and generation.  So no
  handler blocks on the engine.  ``submit`` returns as soon as the job is
  accepted, ``cancel`` hands the kill to another thread and answers 202, and
  the only wait in the file is F-88's, which the caller asked for and which
  N-22 excludes from its figure.
* **N-21.** No list query and no audio retrieval loads an unbounded amount
  into memory.  Pages are bounded by 4.1, segment and result rows carry
  counts rather than audio, and both audio routes stream from disk.

Capability mapping, from F-61's three separately granted permissions:

===========================================  =====================
Operation                                    Capability
===========================================  =====================
status, models                               (authentication only)
estimate, create, job state, cancel          ``generate``
segments, segment audio, audio, result       ``read_results``
history list, source-text snapshot           ``read_history``
===========================================  =====================
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from python_multipart.exceptions import FormParserError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.responses import StreamingResponse

from ..audio import wav
from ..config.budget import resolve_budget_from_system
from ..db.store import JobSummary
from ..domain import (
    Budget,
    Capability,
    Gender,
    Job,
    JobKind,
    JobState,
    Language,
    RequestPath,
    RetentionMode,
    Segment,
    SpeakingStyle,
    VoiceSettings,
)
from ..errors import Code, EchoActError, is_retryable
from ..jobs.engine import BUSY_RETRY_AFTER_S
from ..jobs.request import JobRequest, clamp_wait, estimate
from ..models.registry import ModelState
from ..policy import (
    API_VERSION,
    BOUNDED_WAIT_DEFAULT_S,
    IDEMPOTENCY_TTL_S,
    LIST_PAGE_DEFAULT,
    LIST_PAGE_MAX,
    MAX_CONCURRENT_GENERATION,
    MAX_IMPORT_FILE_BYTES,
    MAX_INPUT_CODEPOINTS,
    MAX_REQUEST_BODY_BYTES,
    ONEOFF_RESULT_TTL_S,
    RATE_GENERATION_PER_MIN,
    RATE_OTHER_PER_MIN,
    TEMPO_DEFAULT,
    TEMPO_MAX,
    TEMPO_MIN,
)
from ..text.loader import load_bytes
from ..text.sniff import SELECTABLE_ENCODINGS, SUPPORTED_FORMATS
from ..util import ids
from .deps import (
    CANCEL_ACK_GRACE_S,
    ServiceContext,
    credential_of,
    iter_file,
    owner_scope,
    require_capability,
    require_job,
    resolve_managed_audio,
)
from .schemas import (
    BudgetOut,
    CapabilitiesOut,
    CreateJobRequest,
    EstimateOut,
    EstimateRequest,
    GenerationStateOut,
    IntegrityOut,
    JobError,
    JobOut,
    JobPage,
    JobProgress,
    JobTextOut,
    LicenseOut,
    MinimumBudgetOut,
    ModelOut,
    ModelsOut,
    ResourcePolicyOut,
    ResultOut,
    SegmentOut,
    SegmentsOut,
    StatusOut,
    VoiceChoice,
    VoiceOut,
)

#: What a caller is told to wait for something that is merely not finished
#: yet.  ``echoact.policy`` fixes no number for it, so the job engine's own
#: busy hint is reused rather than a second one invented: both answer "the one
#: generation slot is working, come back shortly".
NOT_READY_RETRY_AFTER_S = BUSY_RETRY_AFTER_S

AUDIO_MEDIA_TYPE = "audio/wav"

#: N-19 and F-60: a result belongs to one owner, so no shared intermediary may
#: keep a copy and no browser may replay one from its cache.
_AUDIO_HEADERS = {"Cache-Control": "private, no-store", "Accept-Ranges": "none"}


def build_router(context: ServiceContext) -> APIRouter:
    """Register the twelve operations against one service context."""
    router = APIRouter()

    # ==================================================================
    # F-53 -- service and capability query
    # ==================================================================

    @router.get(
        "/status",
        response_model=StatusOut,
        summary="Service, version, supported capabilities, resource policy",
    )
    def get_status(request: Request) -> StatusOut:
        """F-53's uptime, version, capabilities, and *actual* resource policy.

        The budget reported is the one a job would run under right now, which
        F-21 and N-04 make a function of the machine at this instant rather
        than of the owner's setting alone.  A machine that cannot currently
        grant F-23's floor is reported as such instead of failing the query:
        a caller asking whether the service is usable is exactly the caller
        that needs to be told it is not.
        """
        credential = credential_of(request)
        settings = context.settings
        budget, budget_reason = _current_budget(context)
        engine = context.engine
        current = engine.current()
        mine = bool(
            current is not None
            and (credential.is_owner or current.owner_client_id == credential.client_id)
        )
        granted = sorted(c.value for c in credential.effective_capabilities)
        return StatusOut(
            service="echoact",
            version=_app_version(),
            api_version=API_VERSION,
            state="draining" if context.draining else "running",
            started_at=context.started_at,
            uptime_s=max(0.0, ids.now() - context.started_at),
            bind_host=context.host,
            bind_port=context.port,
            mcp_enabled=bool(settings.mcp_enabled),
            client_id=credential.client_id,
            client_name=credential.name,
            credential_expires_at=credential.expires_at,
            capabilities=CapabilitiesOut(
                granted=granted,
                job_kinds=list(JobKind),
                languages=list(Language),
                styles=list(SpeakingStyle),
                input_formats=list(SUPPORTED_FORMATS),
                bounded_wait=True,
                estimate=True,
                upload=True,
                history=credential.has_capability(Capability.READ_HISTORY),
                results=credential.has_capability(Capability.READ_RESULTS),
                generation=credential.has_capability(Capability.GENERATE),
            ),
            resource_policy=ResourcePolicyOut(
                max_input_codepoints=MAX_INPUT_CODEPOINTS,
                max_upload_bytes=MAX_IMPORT_FILE_BYTES,
                max_request_body_bytes=MAX_REQUEST_BODY_BYTES,
                concurrent_generation=MAX_CONCURRENT_GENERATION,
                generation_requests_per_minute=RATE_GENERATION_PER_MIN,
                other_requests_per_minute=RATE_OTHER_PER_MIN,
                list_page_default=LIST_PAGE_DEFAULT,
                list_page_max=LIST_PAGE_MAX,
                bounded_wait_default_s=BOUNDED_WAIT_DEFAULT_S,
                bounded_wait_max_s=context.bounded_wait_ceiling_s,
                one_off_result_ttl_s=ONEOFF_RESULT_TTL_S,
                idempotency_ttl_s=IDEMPOTENCY_TTL_S,
                tempo_min=TEMPO_MIN,
                tempo_max=TEMPO_MAX,
                applied_budget=_budget_out(budget),
                budget_unavailable_reason=budget_reason,
            ),
            generation=GenerationStateOut(
                busy=bool(engine.busy),
                current_job_id=current.job_id if (current is not None and mine) else None,
                current_job_is_mine=mine,
            ),
        )

    # ==================================================================
    # F-53 -- models and voice/setting choices
    # ==================================================================

    @router.get(
        "/models",
        response_model=ModelsOut,
        summary="Models and voice/setting choices",
    )
    def get_models(request: Request) -> ModelsOut:
        """F-53's model list, with every voice described.

        The description is not decoration: F-53 says a bare identifier gives a
        caller that is not a person no basis for choosing a voice, so the
        manifest's own text travels here and to the GUI alike.

        A model that is not downloaded reports ``state`` and a reason and is
        still listed.  F-04 forbids hiding it or substituting another, and
        F-53 requires an undownloaded model to be distinguishable from a
        server fault -- which it is, because this answers 200 either way and
        says which.
        """
        credential_of(request)
        budget, _ = _current_budget(context)
        statuses = context.registry.statuses(budget)
        manifest = context.manifest
        models: list[ModelOut] = []
        for status in statuses:
            entry = manifest.get(status.model_id)
            models.append(
                ModelOut(
                    model_id=status.model_id,
                    display_name=status.display_name,
                    state=status.state.value,
                    ready=status.state is ModelState.READY,
                    runnable=status.runnable,
                    unavailable_reason=status.unavailable_reason,
                    sample_rate=status.sample_rate,
                    languages=[Language(code) for code in status.languages],
                    styles=list(SpeakingStyle),
                    voices=[
                        VoiceChoice(
                            voice_id=voice.voice_id,
                            display_name=voice.display_name,
                            gender=voice.gender,
                            description=voice.description,
                        )
                        for voice in entry.voices
                    ],
                    minimum_budget=MinimumBudgetOut(
                        memory_bytes=status.minimum_memory_bytes,
                        cpu_percent=status.minimum_cpu_percent,
                    ),
                    bytes_total=status.bytes_total,
                    bytes_present=status.bytes_present,
                    license=LicenseOut(
                        name=status.license_name,
                        acceptance_required=status.license_acceptance_required,
                        accepted=status.license_accepted,
                    ),
                    download_authorised=status.download_authorised,
                )
            )
        return ModelsOut(
            models=models,
            languages=list(Language),
            styles=list(SpeakingStyle),
            genders=list(Gender),
            input_formats=list(SUPPORTED_FORMATS),
            encodings=list(SELECTABLE_ENCODINGS),
            tempo_min=TEMPO_MIN,
            tempo_max=TEMPO_MAX,
            tempo_default=TEMPO_DEFAULT,
        )

    # ==================================================================
    # F-88 -- pre-flight estimate
    # ==================================================================

    @router.post(
        "/estimate",
        response_model=EstimateOut,
        summary="Validate text and settings and estimate the work, without creating a job",
    )
    def post_estimate(request: Request, body: EstimateRequest) -> EstimateOut:
        """F-88's pre-flight.  Creates no job, loads no model, synthesises nothing.

        An invalid request still answers 200 with ``valid: false`` and every
        problem it found.  F-88 says an estimate returns "validation results",
        and a caller asking "would this work?" is best served by all the
        reasons it would not rather than by the first exception -- which is
        also why the model's readiness is reported here instead of being a
        refusal.
        """
        require_capability(context, request, Capability.GENERATE)
        settings = _voice_settings(body.voice)
        report = estimate(
            body.text,
            settings,
            context.manifest,
            slot_free=not context.engine.busy,
            model_ready=_model_ready(context, settings.model_id),
        )
        return EstimateOut(**report.to_dict())

    # ==================================================================
    # F-54 -- create a job
    # ==================================================================

    @router.post(
        "/jobs",
        response_model=JobOut,
        status_code=202,
        summary="Create a job from text or an upload, with a required duplicate-prevention key",
        responses={
            200: {
                "model": JobOut,
                "description": (
                    "An existing job, returned unchanged for a repeated duplicate-prevention "
                    "key (F-49), or a job that reached a terminal state inside the requested "
                    "wait (F-88)."
                ),
            },
            202: {"description": "Accepted. The job continues; poll or fetch the result."},
        },
        openapi_extra={"requestBody": _create_job_request_body()},
    )
    async def post_jobs(request: Request, response: Response) -> JobOut:
        """F-54's generation request, with F-49's key and F-88's optional wait.

        Async only because the body may be a multipart upload and reading it
        is the one part that belongs on the event loop; everything that can
        block -- validation, the store, the engine -- is handed to a worker
        thread, so a request that opts into a ten-second wait does not stall
        the nine other things N-22 promises to answer within one second.
        """
        body, upload, filename = await _read_create_request(request)
        return await run_in_threadpool(_create_job, context, request, response, body, upload, filename)

    # ==================================================================
    # F-56 -- history
    # ==================================================================

    @router.get(
        "/jobs",
        response_model=JobPage,
        summary="Authorised retained history query",
    )
    def get_jobs(
        request: Request,
        limit: int = Query(
            LIST_PAGE_DEFAULT,
            description=(
                f"Items per page. Clamped to 1..{LIST_PAGE_MAX} rather than refused, "
                "because 4.1 fixes the page size regardless of what is asked."
            ),
        ),
        offset: int = Query(0, description="Rows to skip, oldest-last ordering by creation time."),
        state: JobState | None = Query(None),
        model_id: str | None = Query(None),
        retention: RetentionMode | None = Query(
            None,
            description=(
                "Defaults to 'retained': F-56 authorises listing retained jobs. "
                "Pass 'one_off' to list one-off jobs still inside their lifetime."
            ),
        ),
    ) -> JobPage:
        """F-56's list: summaries only, scoped to the caller's own jobs.

        The body text is absent by design -- F-56 makes the summary the
        default and puts the snapshot behind its own request -- and so is
        every other owner's row, unless the credential is the GUI owner's,
        which F-50 allows to review all jobs.
        """
        credential = require_capability(context, request, Capability.READ_HISTORY)
        page = context.store.list_jobs(
            owner_client_id=owner_scope(credential),
            retention=retention or RetentionMode.RETAINED,
            states=[state] if state is not None else None,
            model_id=model_id,
            limit=limit,
            offset=max(0, offset),
            include_source_text=False,
        )
        return JobPage(
            items=[_summary_out(summary) for summary in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            has_more=page.has_more,
        )

    # ==================================================================
    # F-54 -- one job's state
    # ==================================================================

    @router.get(
        "/jobs/{job_id}",
        response_model=JobOut,
        summary="State, progress, and whether the result is ready",
    )
    def get_job(request: Request, job_id: str) -> JobOut:
        """F-48's status query.  A job owned by another client is 404 (N-19)."""
        _, job = require_job(context, request, job_id, Capability.GENERATE, include_segments=True)
        return _job_out(context, job)

    # ==================================================================
    # F-56 -- the source-text snapshot
    # ==================================================================

    @router.get(
        "/jobs/{job_id}/text",
        response_model=JobTextOut,
        summary="Source-text snapshot of a retained job, separately authorised",
    )
    def get_job_text(request: Request, job_id: str) -> JobTextOut:
        """F-56's separate, separately authorised snapshot request.

        A job whose snapshot is gone answers 404 rather than an empty string.
        4.2 lets a one-off job's text be cleared once its window closes, and
        reporting that as "the text is ''" would be a different fact.
        """
        _, job = require_job(context, request, job_id, Capability.READ_HISTORY)
        text = context.store.get_job_text(job_id)
        if text is None:
            raise EchoActError(
                Code.NOT_FOUND,
                "This job no longer holds a source-text snapshot.",
                detail={"job_id": job_id, "retention": job.retention.value},
            )
        return JobTextOut(
            job_id=job_id, text=text, codepoints=len(text), retention=job.retention
        )

    # ==================================================================
    # F-49 -- cancellation
    # ==================================================================

    @router.post(
        "/jobs/{job_id}/cancel",
        response_model=JobOut,
        status_code=202,
        summary="Cancel a job; an already-terminated job answers 200 with its final state",
        responses={
            200: {
                "model": JobOut,
                "description": "The job had already reached a terminal state; it is returned.",
            },
            202: {"description": "The cancellation was accepted; the slot is released shortly."},
        },
    )
    def post_cancel(request: Request, response: Response, job_id: str) -> JobOut:
        """F-49's cancellation, and N-22's two different deadlines.

        Cancelling has to be *accepted* within a second at p95, while the
        generation slot has five seconds to be released.  The kill therefore
        runs on the service's own thread pool and this handler answers as soon
        as it knows the cancellation was taken, waiting only a quarter second
        in case the job stops instantly and a truer state can be reported.

        Repeating it adds no further side effects, which F-49 requires: an
        already-terminal job is answered 200 with the state it reached, and
        nothing is asked of the engine at all.
        """
        _, job = require_job(context, request, job_id, Capability.GENERATE)
        if job.state.is_terminal:
            response.status_code = 200
            return _job_out(context, _reload(context, job_id))

        future = context.executor.submit(context.engine.cancel, job_id)
        try:
            future.result(CANCEL_ACK_GRACE_S)
        except TimeoutError:
            # The expected case for a job that is mid-segment: the worker has
            # up to five seconds to die and the caller is not made to wait for
            # it.  The kill goes on without this request.
            pass
        response.status_code = 202
        return _job_out(context, _reload(context, job_id))

    # ==================================================================
    # F-55 -- segments
    # ==================================================================

    @router.get(
        "/jobs/{job_id}/segments",
        response_model=SegmentsOut,
        summary="Ready segments and the last sequence number",
    )
    def get_segments(request: Request, job_id: str) -> SegmentsOut:
        """F-55's mapping for the segments that exist.

        Only ready segments are listed.  F-55 forbids returning an ungenerated
        segment as if it were a finished result, and the honest way to say
        "there are more coming" is ``complete: false`` beside a count, not a
        row with null times in it.
        """
        _, job = require_job(context, request, job_id, Capability.READ_RESULTS)
        ready = context.store.list_segments(job_id, ready_only=True)
        return SegmentsOut(
            job_id=job_id,
            segments=[_segment_out(segment) for segment in ready],
            last_sequence=ready[-1].index if ready else None,
            ready_count=len(ready),
            total_segments=job.total_segments,
            complete=job.state is JobState.COMPLETE,
        )

    @router.get(
        "/jobs/{job_id}/segments/{segment_id}/audio",
        summary="Audio of one ready segment",
        response_class=StreamingResponse,
        responses={200: {"content": {AUDIO_MEDIA_TYPE: {}}, "description": "The segment's WAV."}},
    )
    def get_segment_audio(request: Request, job_id: str, segment_id: str) -> StreamingResponse:
        """One ready segment's WAV, streamed.

        A segment that exists but carries no audio -- a range of characters
        F-27 keeps on the timeline without sending it for synthesis -- is
        reported as gone rather than as not yet ready.  "Not ready" invites a
        retry, and this one will never become ready.  Neither will one whose
        job has already ended, which is why the refusal below asks the job
        and not only the segment.
        """
        _, job = require_job(context, request, job_id, Capability.READ_RESULTS)
        segment = _find_segment(context, job_id, segment_id)
        if not segment.ready:
            raise _not_ready(
                job,
                Code.SEGMENT_NOT_READY,
                {"job_id": job_id, "segment_id": segment_id, "index": segment.index},
                ended="That segment was never generated; the job ended first.",
            )
        path = resolve_managed_audio(context, segment.audio_path)
        if path is None:
            raise EchoActError(
                Code.RESULT_MISSING,
                "That segment carries no audio of its own.",
                detail={
                    "job_id": job_id,
                    "segment_id": segment_id,
                    "spoken": segment.is_spoken,
                },
            )
        return _stream(path, f"{job_id}-{segment.index:05d}.wav")

    # ==================================================================
    # F-55 -- the full result
    # ==================================================================

    @router.get(
        "/jobs/{job_id}/audio",
        summary="Completed full audio",
        response_class=StreamingResponse,
        responses={200: {"content": {AUDIO_MEDIA_TYPE: {}}, "description": "The job's WAV."}},
    )
    def get_audio(request: Request, job_id: str) -> StreamingResponse:
        """F-55's completed WAV, in F-82's format, streamed rather than loaded.

        An incomplete job answers 409 and never a partial file: F-55 forbids
        handing back an incomplete final file as though it were finished, and
        F-16's partial export is a GUI operation the user performs knowingly,
        not something a client gets by asking early.
        """
        _, job = require_job(context, request, job_id, Capability.READ_RESULTS)
        result = _require_result(job)
        if job.state is not JobState.COMPLETE:
            raise _not_ready(
                job,
                Code.RESULT_NOT_READY,
                {"job_id": job_id},
                ended="This job ended before its full audio was assembled.",
            )
        if _expired(result):
            raise EchoActError(
                Code.RESULT_EXPIRED, detail={"job_id": job_id, "expires_at": result.expires_at}
            )
        path = resolve_managed_audio(context, result.relative_path)
        if path is None:
            raise EchoActError(Code.RESULT_MISSING, detail={"job_id": job_id})
        return _stream(path, f"{job_id}.wav")

    @router.get(
        "/jobs/{job_id}/result",
        response_model=ResultOut,
        summary="Result metadata: format, rate, length, size, integrity value, expiry",
    )
    def get_result(request: Request, job_id: str) -> ResultOut:
        """4.2's Result entity.

        ``expired`` and ``available`` are separate answers.  4.2 keeps a
        result row after its lifetime passes so that a repeated re-request key
        can return the existing job and the result's expired state, and 5.3
        wants the reason retrieval fails rather than a bare absence.
        """
        _, job = require_job(context, request, job_id, Capability.READ_RESULTS)
        result = _require_result(job)
        path = resolve_managed_audio(context, result.relative_path)
        expired = _expired(result)
        return ResultOut(
            job_id=job_id,
            result_id=result.result_id,
            format=wav.FORMAT_NAME,
            media_type=AUDIO_MEDIA_TYPE,
            sample_rate=result.sample_rate,
            channels=result.channels,
            sample_width_bits=result.sample_width_bits,
            frame_count=result.frame_count,
            duration_ms=result.duration_ms,
            byte_size=result.byte_size,
            integrity=IntegrityOut(
                algorithm="sha256",
                value=result.digest,
                state=context.store.result_integrity(result.result_id).value,
            ),
            created_at=result.created_at,
            expires_at=result.expires_at,
            expired=expired,
            available=bool(path is not None and not expired),
        )

    return router


# ======================================================================
# Job creation, off the event loop
# ======================================================================


async def _read_create_request(
    request: Request,
) -> tuple[CreateJobRequest, bytes | None, str | None]:
    """Read F-54's two alternative body shapes.

    JSON carries the text inline; multipart carries the same object in a
    ``request`` part beside the file.  One model validates both, so the upload
    path cannot drift into accepting something the inline path refuses -- F-37
    requires every entry path to apply the same rules.
    """
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()

    if media_type == "application/json" or media_type == "":
        raw = await request.body()
        return _validate_create(raw or b"{}"), None, None

    if media_type in ("multipart/form-data", "application/x-www-form-urlencoded"):
        form = await _read_form(request)
        try:
            described = form.get("request")
            upload = form.get("file")
            if described is None or not isinstance(described, str):
                raise EchoActError(
                    Code.JOB_KIND_MISSING,
                    "An upload must carry a 'request' part describing the job.",
                    detail={"field": "request"},
                )
            data: bytes | None = None
            filename: str | None = None
            if upload is not None and not isinstance(upload, str):
                data = await upload.read()
                filename = upload.filename
            return _validate_create(described.encode("utf-8")), data, filename
        finally:
            await form.close()

    raise EchoActError(
        Code.FILE_UNSUPPORTED,
        "The request body must be JSON or a multipart upload.",
        detail={"content_type": media_type},
    )


async def _read_form(request: Request) -> FormData:
    """Parse a form body, with Section 2.10's status for one that will not parse.

    Reading the form here rather than through a declared parameter is what
    lets one model validate both body shapes, but it also steps around the
    400 FastAPI wraps its own form handling in.  A body whose multipart
    framing is broken, whose boundary is missing, or which carries more parts
    than the parser will assemble raises out of the parser as neither an
    ``EchoActError`` nor a validation error, so without this it reaches the
    unexpected-exception handler: 500 INTERNAL, a status Section 2.10 does
    not list for a malformed request, in an envelope carrying no code a
    client can act on.  Starlette's refusal takes two shapes -- it wraps the
    parser's exception in a bare 400 when the request carries an app in its
    scope and re-raises the exception itself otherwise -- and the bare 400 is
    no better here: it reaches ``http_exception_error`` as a status with no
    code behind it and becomes the same 500.  Both are caught.

    ``max_part_size`` is 4.1's request cap rather than Starlette's 1 MiB
    default, so the only size limit on a body is the one the gate has already
    enforced -- before a byte was read, from ``Content-Length``, or as the
    chunks arrived.  Left at the default, a part well inside 4.1's 2,000,000
    bytes is refused by a limit no part of this contract states.
    """
    try:
        return await request.form(max_part_size=MAX_REQUEST_BODY_BYTES)
    except StarletteHTTPException as exc:
        if exc.status_code != 400:
            raise
        raise _unreadable_form(exc) from exc
    except (MultiPartException, FormParserError) as exc:
        raise _unreadable_form(exc) from exc


def _unreadable_form(cause: Exception) -> EchoActError:
    """A body that will not parse, as Section 2.10's 400 with a code on it.

    The code is the same one an unparseable JSON body already answers with:
    a body nothing could be read out of has, among other things, stated no
    job kind.  The parser's own text is dropped rather than passed on --
    it quotes the boundary and the byte offset it stopped at, which is a
    fragment of the caller's body, and F-57 keeps that out of a response
    just as N-20 keeps it out of a log.
    """
    return EchoActError(
        Code.MALFORMED_REQUEST,
        "The request body could not be read as a form.",
        detail={"field": "body"},
        cause=cause,
    )


def _validate_create(raw: bytes) -> CreateJobRequest:
    try:
        return CreateJobRequest.model_validate_json(raw)
    except ValidationError as exc:
        # Re-raised as FastAPI's own type so that the single handler in
        # ``service.errors`` decides the code and status; a second translation
        # here is a second place for the contract to drift.
        raise RequestValidationError(exc.errors()) from exc


def _create_job(
    context: ServiceContext,
    request: Request,
    response: Response,
    body: CreateJobRequest,
    upload: bytes | None,
    filename: str | None,
) -> JobOut:
    """F-54 and F-49, on a worker thread.

    The bounded wait is the last thing that happens and changes only when the
    answer is sent.  F-88 is explicit that waiting is an optimisation and
    never a different lifecycle, so the job is identical whether the caller
    waited, the bound passed, or the caller never asked.
    """
    credential = require_capability(context, request, Capability.GENERATE)
    text = _resolve_input(body, upload, filename)
    settings = _voice_settings(body.voice)

    wait_s = clamp_wait(body.wait_s, ceiling_s=context.bounded_wait_ceiling_s)
    # F-88: never wait while a model is being prepared or downloaded.  Asked
    # before submitting, because once the run thread starts the answer becomes
    # a race with it rather than a property of this request.
    would_load = context.engine.would_need_load(settings) if wait_s > 0 else False

    job_request = JobRequest(
        text=text,
        settings=settings,
        request_path=RequestPath.REST,
        owner_client_id=credential.client_id,
        idempotency_key=body.idempotency_key,
        kind=body.kind,
        retention=RetentionMode.RETAINED if body.retain else RetentionMode.ONE_OFF,
        # F-70 names the client in a completion notice.  The credential's own
        # name is used rather than a label the caller supplies: N-18 keeps
        # client-supplied text as data, and a self-chosen label in a
        # notification is text the app would be repeating on trust.
        client_label=credential.name,
        wait_s=wait_s or None,
    )
    job, created = context.engine.submit(job_request)

    waited_s = 0.0
    if created and wait_s > 0 and not would_load:
        started = ids.monotonic()
        job = context.engine.wait(job.job_id, wait_s)
        waited_s = round(ids.monotonic() - started, 3)

    if not created:
        # 4.2: the same key with the same content returns the existing job and
        # its state, terminal or expired included, and regenerates nothing.
        response.status_code = 200
    elif job.state.is_terminal:
        response.status_code = 200
    else:
        response.status_code = 202
    return _job_out(context, job, waited_s=waited_s, duplicate=not created)


def _resolve_input(
    body: CreateJobRequest, upload: bytes | None, filename: str | None
) -> str:
    """F-54: inline text or an upload, never both and never neither.

    An upload goes through ``text.loader.load_bytes``, which is the same
    function the file dialog uses.  F-37 requires an automated caller to reach
    the identical checks in the identical order, and sharing the function is
    the only way that stays true as the checks change.
    """
    has_text = body.text is not None
    has_upload = upload is not None
    if has_text and has_upload:
        raise EchoActError(Code.INPUT_AMBIGUOUS)
    if has_upload:
        loaded = load_bytes(
            upload or b"",
            filename=filename,
            encoding=body.encoding,
            confirm_unknown=body.confirm_unknown,
        )
        return loaded.text
    if not has_text:
        raise EchoActError(Code.INPUT_EMPTY, "Provide inline text or an upload.")
    return body.text or ""


# ======================================================================
# Projections
# ======================================================================


def _voice_settings(voice: Any) -> VoiceSettings:
    return VoiceSettings(
        model_id=voice.model_id,
        language=voice.language,
        gender=voice.gender,
        voice_id=voice.voice_id,
        style=voice.style,
        tempo=voice.tempo,
    )


def _voice_out(settings: VoiceSettings) -> VoiceOut:
    return VoiceOut(
        model_id=settings.model_id,
        voice_id=settings.voice_id,
        gender=settings.gender,
        language=settings.language,
        style=settings.style,
        tempo=settings.tempo,
    )


def _budget_out(budget: Budget | None) -> BudgetOut | None:
    if budget is None:
        return None
    return BudgetOut(
        cpu_percent=budget.cpu_percent,
        memory_bytes=budget.memory_bytes,
        intra_op_threads=budget.intra_op_threads,
        inter_op_threads=budget.inter_op_threads,
    )


def _job_error(code: str | None, message: str | None) -> JobError | None:
    """5.3: a generation failure after acceptance is the job's state, not the
    request's, so it is reported here with the same three facts F-57 puts in a
    refusal -- code, message, and whether a retry can succeed."""
    if not code:
        return None
    try:
        parsed = Code(code)
    except ValueError:
        return JobError(code=code, message=message, retryable=False)
    return JobError(code=parsed.value, message=message, retryable=is_retryable(parsed))


def _job_out(
    context: ServiceContext,
    job: Job,
    *,
    waited_s: float | None = None,
    duplicate: bool | None = None,
) -> JobOut:
    result = job.result
    expired = _expired(result) if result is not None else False
    duration_ms = result.duration_ms if result is not None else job.playable_ms()
    return JobOut(
        job_id=job.job_id,
        kind=job.kind,
        state=job.state,
        terminal=job.state.is_terminal,
        request_path=job.request_path,
        owner_client_id=job.owner_client_id,
        client_label=job.client_label,
        retention=job.retention,
        voice=_voice_out(job.settings),
        budget=_budget_out(job.budget),
        progress=JobProgress(
            generated_segments=job.generated_segments,
            total_segments=job.total_segments,
            fraction=round(job.progress, 4),
        ),
        created_at=job.created_at,
        started_at=job.started_at,
        ended_at=job.ended_at,
        result_ready=bool(result is not None and not expired),
        result_expired=expired,
        audio_duration_ms=duration_ms,
        error=_job_error(job.error_code, job.error_message),
        waited_s=waited_s,
        duplicate=duplicate,
    )


def _summary_out(summary: JobSummary) -> JobOut:
    """The same shape from F-56's summary row, which never reads a WAV."""
    return JobOut(
        job_id=summary.job_id,
        kind=summary.kind,
        state=summary.state,
        terminal=summary.state.is_terminal,
        request_path=summary.request_path,
        owner_client_id=summary.owner_client_id,
        client_label=summary.client_label,
        retention=summary.retention,
        voice=_voice_out(summary.settings),
        budget=_budget_out(summary.budget),
        progress=JobProgress(
            generated_segments=summary.generated_segments,
            total_segments=summary.total_segments,
            fraction=round(
                min(1.0, summary.generated_segments / summary.total_segments)
                if summary.total_segments > 0
                else 0.0,
                4,
            ),
        ),
        created_at=summary.created_at,
        started_at=summary.started_at,
        ended_at=summary.ended_at,
        result_ready=bool(summary.has_result and not summary.result_expired),
        result_expired=summary.result_expired,
        audio_duration_ms=summary.audio_duration_ms,
        error=_job_error(summary.error_code, summary.error_message),
    )


def _segment_out(segment: Segment) -> SegmentOut:
    span = segment.time
    return SegmentOut(
        segment_id=segment.segment_id,
        index=segment.index,
        source_start=segment.source.start,
        source_end=segment.source.end,
        start_ms=span.start_ms if span else 0,
        end_ms=span.end_ms if span else 0,
        duration_ms=span.duration_ms if span else 0,
        language=segment.language,
        spoken=segment.is_spoken,
        has_audio=bool(segment.audio_path),
    )


# ======================================================================
# Small helpers
# ======================================================================


def _app_version() -> str:
    from .. import __version__

    return __version__


def _current_budget(context: ServiceContext) -> tuple[Budget | None, str | None]:
    """The budget in force, or why there is none.

    F-23 refuses to load a model under the floor, and ``resolve_budget_from_system``
    says so by raising.  A status query must not fail for that reason -- it is
    precisely the query that should report it -- so the refusal becomes a
    reason string with no path or host detail in it.
    """
    try:
        return resolve_budget_from_system(context.settings), None
    except EchoActError as exc:
        return None, exc.message


def _model_ready(context: ServiceContext, model_id: str) -> bool:
    """Shallow readiness, for F-88's estimate.

    Shallow because N-22 gives an estimate one second at p95 and a deep verify
    hashes 385 MB.  An unknown model is simply not ready; the estimate's own
    validation reports *why* it is unknown.
    """
    if not context.manifest.has(model_id):
        return False
    try:
        return context.registry.status(model_id, deep=False).state is ModelState.READY
    except EchoActError:
        return False


def _reload(context: ServiceContext, job_id: str) -> Job:
    return context.store.get_job(job_id, include_source_text=False, include_segments=True)


def _find_segment(context: ServiceContext, job_id: str, segment_id: str) -> Segment:
    for segment in context.store.list_segments(job_id):
        if segment.segment_id == segment_id:
            return segment
    raise EchoActError(
        Code.NOT_FOUND, "No such segment.", detail={"job_id": job_id, "segment_id": segment_id}
    )


def _require_result(job: Job) -> Any:
    if job.result is None:
        raise _not_ready(
            job,
            Code.RESULT_NOT_READY,
            {"job_id": job.job_id},
            ended="This job ended without producing a result.",
        )
    return job.result


def _not_ready(job: Job, code: Code, detail: dict[str, Any], *, ended: str) -> EchoActError:
    """"Not finished yet" and "this will never finish" are different refusals.

    Whether something is still coming is a fact about the *job*, not about
    the row being asked for, so the state decides which of the two this is.
    While the job can still get there, the answer is the retryable code with
    N-23's hint on it.  Once the job has ended it cannot, and rule 8 forbids
    a permanent refusal from carrying a hint at all: a client that honours
    the hint -- which F-47 says it must -- would otherwise poll a failed job
    every three seconds for a result that can never appear.  What it gets
    instead is 5.3's reason, the job's terminal state and its own failure
    code, in the same envelope the no-audio segment case already answers.
    """
    described = {**detail, "state": job.state.value}
    if not job.state.is_terminal:
        return EchoActError(code, retry_after_s=NOT_READY_RETRY_AFTER_S, detail=described)
    if job.error_code:
        described["error_code"] = job.error_code
    return EchoActError(Code.RESULT_MISSING, ended, detail=described)


def _expired(result: Any) -> bool:
    return result.expires_at is not None and result.expires_at <= ids.now()


def _stream(path: Path, download_name: str) -> StreamingResponse:
    """Stream a managed WAV.

    ``Content-Length`` comes from the file on disk rather than from the stored
    row: the row is what the digest was taken over, and answering with a
    length the body will not match is worse than answering with the truth.
    The filename offered is built from identifiers the app minted, so nothing
    a caller supplied ever reaches a header.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EchoActError(Code.RESULT_MISSING, cause=exc) from exc
    headers = {
        **_AUDIO_HEADERS,
        "Content-Length": str(size),
        "Content-Disposition": f'attachment; filename="{download_name}"',
    }
    return StreamingResponse(iter_file(path), media_type=AUDIO_MEDIA_TYPE, headers=headers)


def _create_job_request_body() -> dict[str, Any]:
    """The two alternative bodies F-54 allows, for the OpenAPI document.

    Written by hand because FastAPI describes one body per operation and this
    operation has two: inline JSON, or the same object in a ``request`` part
    beside an uploaded file.  F-57 requires the specification to be accurate,
    and an omitted alternative is an inaccuracy a client discovers at runtime.
    """
    reference = {"$ref": "#/components/schemas/CreateJobRequest"}
    return {
        "required": True,
        "content": {
            "application/json": {"schema": reference},
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["request"],
                    "properties": {
                        "request": {
                            "type": "string",
                            "description": (
                                "The CreateJobRequest object, JSON-encoded, with 'text' omitted."
                            ),
                        },
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": "A TXT or Markdown file to be validated and spoken.",
                        },
                    },
                }
            },
        },
    }

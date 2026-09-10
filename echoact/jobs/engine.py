"""The single generation slot, and the machine that drives one job through it.

F-47 allows one generation across the GUI and every client combined, so
this class is the product's narrowest resource and the one place that owns
it.  Everything else -- the window, the REST service, the MCP server --
asks here and is told yes, or busy.

The shape follows from three requirements that pull against each other:

* F-12 wants audio as early as possible, so segments are rendered one at a
  time and published the moment each file is closed, rather than at the end.
* N-22 wants the slot released within five seconds of a cancellation, which
  means the worker can be killed mid-segment.  That is only safe because the
  worker owns nothing durable and because the parent named every output file
  before asking for it, so an interrupted write leaves a file this module
  already knows to discard.
* Section 5.1 says Complete means the audio is ready *and* any requested
  retention succeeded.  So the terminal transition happens after the result
  is written and recorded, never when the last segment lands.

Cancellation and completion can race.  5.1 keeps whichever terminal state
was confirmed first and forbids Canceled from overwriting Complete, and
that is enforced in the store's transition check rather than by ordering
here -- a rule that depends on two threads interleaving politely is not a
rule.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..audio import wav
from ..config.budget import HaltReason, ResourceSampler, halt_decision, resolve_budget_from_system
from ..config.settings import Settings
from ..db.store import Store
from ..domain import (
    Budget,
    Job,
    JobState,
    RequestPath,
    RetentionMode,
    Segment,
    TimeRange,
    VoiceSettings,
)
from ..engine.supervisor import WorkerSupervisor
from ..errors import Code, EchoActError
from ..models.manifest import Manifest
from ..models.registry import ModelRegistry, ModelState
from ..paths import audio_dir, temp_dir
from ..policy import (
    BOUNDED_WAIT_CEILING_S,
    ENGINE_TOTAL_STEPS,
    ONEOFF_RESULT_TTL_S,
    WORKER_RELEASE_DEADLINE_S,
)
from ..util import ids
from ..util.logging import get_logger, job_context
from .request import JobRequest, plan_segments, validate_request

log = get_logger("jobs.engine")

#: What a busy caller is told to wait.  A.5 measured a real-time factor
#: near 0.2, so a typical short job is seconds rather than minutes; the
#: hint is a floor on politeness, not a prediction.
BUSY_RETRY_AFTER_S = 3.0


class EventKind(StrEnum):
    ACCEPTED = "accepted"
    STATE = "state"
    SEGMENT = "segment"
    USAGE = "usage"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class Event:
    """What happened, for anyone watching.

    Carries identifiers and numbers, never audio and never body text: the
    GUI, the log, and a notification all consume these, and N-20 keeps body
    text out of the last two.
    """

    kind: EventKind
    job_id: str
    state: JobState | None = None
    segment_index: int | None = None
    generated: int = 0
    total: int = 0
    request_path: RequestPath | None = None
    client_label: str | None = None
    error_code: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _Run:
    """The mutable state of the one job that is running."""

    job: Job
    segments: list[Segment]
    budget: Budget
    settings: VoiceSettings
    cancel: threading.Event
    finished: threading.Event
    thread: threading.Thread | None = None
    sample_rate: int = 0
    segment_paths: list[str] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)
    halt: EchoActError | None = None


class JobEngine:
    """One slot, one worker, one job at a time."""

    def __init__(
        self,
        *,
        store: Store,
        supervisor: WorkerSupervisor,
        registry: ModelRegistry,
        manifest: Manifest,
        settings: Settings,
        work_dir: Path | None = None,
        result_dir: Path | None = None,
        total_steps: int = ENGINE_TOTAL_STEPS,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._registry = registry
        self._manifest = manifest
        self._settings = settings
        self._work_dir = Path(work_dir) if work_dir else temp_dir()
        self._result_dir = Path(result_dir) if result_dir else audio_dir()
        self._total_steps = total_steps

        self._lock = threading.RLock()
        self._run: _Run | None = None
        self._listeners: list[Callable[[Event], None]] = []
        self._closing = False

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def listen(self, fn: Callable[[Event], None]) -> Callable[[], None]:
        """Subscribe.  Returns an unsubscribe callable.

        Listeners are called on the run thread, so a GUI listener must
        marshal to the main thread rather than touch a widget here.
        """
        self._listeners.append(fn)

        def off() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return off

    def _emit(self, event: Event) -> None:
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception as exc:  # noqa: BLE001 - a listener must not stop a job
                log.warning("job listener failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._run is not None

    def current(self) -> Job | None:
        with self._lock:
            return self._run.job if self._run else None

    def apply_settings(self, settings: Settings) -> None:
        """F-78: a change during a job applies to the *next* job.

        Nothing here touches a running job, which is the whole point: the
        job recorded the budget it started under and keeps it.
        """
        self._settings = settings

    def would_need_load(self, settings: VoiceSettings | None = None) -> bool:
        """F-17: whether the next job pays for a model load."""
        voice = settings or self._settings.voice
        try:
            budget = resolve_budget_from_system(self._settings)
        except EchoActError:
            return True
        return self._supervisor.would_need_load(voice.model_id, budget)

    # ------------------------------------------------------------------
    # Submission (F-47, F-49)
    # ------------------------------------------------------------------

    def submit(self, request: JobRequest) -> tuple[Job, bool]:
        """Accept a job, or refuse it.  Returns ``(job, created)``.

        Order matters and is not arbitrary.  The slot is taken *before* the
        duplicate-prevention key is claimed, because a refusal for busy must
        not consume the key -- F-49's record is meant to identify a job that
        exists, and a busy response creates none.  If the claim then finds
        an existing job, the slot is handed straight back.
        """
        entry = validate_request(request, self._manifest)
        budget = resolve_budget_from_system(self._settings)

        runnable, why = self._registry.can_run(request.settings.model_id, budget)
        if not runnable:
            # F-04: unavailable with the reason, never silently substituted.
            raise EchoActError(Code.MODEL_OVER_BUDGET, why or None)
        self._check_model_preparable(request.settings.model_id)

        with self._lock:
            if self._closing:
                raise EchoActError(Code.SHUTTING_DOWN)
            taken = self._run is None
            if taken:
                self._run = _PLACEHOLDER
        if not taken:
            raise EchoActError(Code.BUSY, retry_after_s=BUSY_RETRY_AFTER_S)

        try:
            job = Job(
                job_id=ids.job_id(),
                kind=request.kind,
                request_path=request.request_path,
                owner_client_id=request.owner_client_id,
                state=JobState.ACCEPTED,
                source_text=request.text,
                settings=request.settings,
                budget=budget,
                retention=request.retention,
                created_at=ids.now(),
                client_label=request.client_label,
                idempotency_key=request.idempotency_key,
            )
            stored, created = self._store.claim_job(
                job,
                client_id=request.owner_client_id,
                key=request.idempotency_key,
                request_digest=request.digest(),
            )
            if not created:
                # 4.2: a repeat returns the existing job and its state,
                # including a terminal or expired one, and regenerates
                # nothing.  A new key is needed to generate again.
                self._release_slot()
                return stored, False

            segments = plan_segments(request.text, request.settings)
            stored.segments = list(self._store.insert_segments(stored.job_id, segments))
            stored.total_segments = len(stored.segments)
            self._store.set_job_budget(stored.job_id, budget)

            run = _Run(
                job=stored,
                segments=stored.segments,
                budget=budget,
                settings=request.settings,
                cancel=threading.Event(),
                finished=threading.Event(),
                sample_rate=entry.sample_rate,
            )
            with self._lock:
                self._run = run
            self._emit(
                Event(
                    EventKind.ACCEPTED,
                    stored.job_id,
                    state=stored.state,
                    total=len(segments),
                    request_path=stored.request_path,
                    client_label=stored.client_label,
                )
            )
            run.thread = threading.Thread(
                target=self._run_job, args=(run,), name=f"echoact-job-{stored.job_id}", daemon=True
            )
            run.thread.start()
            return stored, True
        except BaseException:
            self._release_slot()
            raise

    def _check_model_preparable(self, model_id: str) -> None:
        """Refuse at acceptance what could only fail later anyway.

        5.3 says a request for a model that is not downloaded is *refused*,
        not accepted and then failed, and the same reasoning covers a
        licence nobody has accepted: both are knowable now, neither can
        change while the job waits, and accepting the job would consume
        F-49's key on something that can never run and hand the caller a
        job id to poll instead of an answer.

        Deliberately the shallow check.  N-22 gives acceptance one second
        at p95 and a deep verify hashes 385 MB; the deep pass still runs in
        ``_prepare``, where its cost belongs.
        """
        status = self._registry.status(model_id, deep=False)
        if status.state is ModelState.CORRUPT:
            raise EchoActError(Code.MODEL_CORRUPT, detail={"model_id": model_id})
        if status.state is not ModelState.READY:
            raise EchoActError(Code.MODEL_NOT_READY, detail={"model_id": model_id})
        if status.license_acceptance_required and not status.license_accepted:
            raise EchoActError(
                Code.MODEL_LICENSE_NOT_ACCEPTED,
                detail={"model_id": model_id, "license": status.license_name},
            )

    def _release_slot(self) -> None:
        with self._lock:
            self._run = None

    # ------------------------------------------------------------------
    # Waiting (F-88)
    # ------------------------------------------------------------------

    def wait(self, job_id: str, timeout_s: float) -> Job:
        """Wait up to ``timeout_s`` for a terminal state, then answer anyway.

        F-88 makes this an optimisation and never a different lifecycle:
        the job is untouched whether the bound passes or not, and the caller
        gets the same job either way.  It also never waits on model
        preparation -- the wait begins only once generation is under way,
        which is why the deadline is re-checked against the job's state
        rather than simply slept through.
        """
        bound = max(0.0, min(float(timeout_s), BOUNDED_WAIT_CEILING_S))
        with self._lock:
            run = self._run
        if run is None or run is _PLACEHOLDER or run.job.job_id != job_id:
            return self._store.get_job(job_id)
        if bound > 0:
            run.finished.wait(bound)
        return self._store.get_job(job_id)

    # ------------------------------------------------------------------
    # Cancellation (F-15, F-49, N-22)
    # ------------------------------------------------------------------

    def cancel(self, job_id: str) -> Job:
        """Cancel a job.  Repeating it adds no further side effects (F-49)."""
        job = self._store.get_job(job_id, include_segments=False)
        if job.state.is_terminal:
            return job
        with self._lock:
            run = self._run
            running = run is not None and run is not _PLACEHOLDER and run.job.job_id == job_id
        if not running:
            # Accepted but not the current job: only possible after an
            # abnormal termination, which F-45 reconciles to Interrupted.
            self._store.update_job_state(job_id, JobState.CANCELING, force=True)
            self._store.update_job_state(job_id, JobState.CANCELED)
            return self._store.get_job(job_id, include_segments=False)

        assert run is not None
        self._set_state(run, JobState.CANCELING)
        run.cancel.set()
        # Mid-segment is the case N-22's five seconds is written for: the
        # worker is inside an ONNX call that will not return promptly, so
        # asking politely is not enough.
        elapsed = self._supervisor.kill()
        log.info(job_context(job_id, "canceling", release_s=round(elapsed, 3)))
        run.finished.wait(WORKER_RELEASE_DEADLINE_S)
        return self._store.get_job(job_id, include_segments=False)

    def release_model(self) -> None:
        """F-19's explicit release.  Refused while a job is running, because
        the job would then fail rather than the model being freed."""
        if self.busy:
            raise EchoActError(Code.BUSY, retry_after_s=BUSY_RETRY_AFTER_S)
        self._supervisor.unload()

    def shutdown(self) -> None:
        """F-52: report and stop.  The worker goes first, so nothing is
        still writing when the database closes."""
        with self._lock:
            self._closing = True
            run = self._run
        if run is not None and run is not _PLACEHOLDER:
            run.cancel.set()
            self._supervisor.kill()
            run.finished.wait(WORKER_RELEASE_DEADLINE_S)
        else:
            self._supervisor.kill()

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------

    def _run_job(self, run: _Run) -> None:
        job_id = run.job.job_id
        try:
            self._prepare(run)
            self._generate(run)
            self._finish(run)
        except EchoActError as exc:
            self._fail(run, exc)
        except Exception as exc:  # noqa: BLE001 - a run thread must not vanish
            log.exception("job %s failed unexpectedly", job_id)
            self._fail(run, EchoActError(Code.GENERATION_FAILED, cause=exc))
        finally:
            self._cleanup(run)
            run.finished.set()
            self._release_slot()
            self._announce_finished(run)

    def _announce_finished(self, run: _Run) -> None:
        """The last word on a job, read back from the database.

        Read back rather than assumed, because the terminal state may not
        be the one this thread chose: 5.1 lets a cancellation and a
        completion race and keeps whichever was confirmed first.

        Tolerant of a closed database on purpose. Shutdown kills the worker
        and then closes the store, so this can be the last thing running
        during an exit, and a job that has already ended is not worth an
        exception on the way out.
        """
        job_id = run.job.job_id
        try:
            final = self._store.get_job(
                job_id, include_source_text=False, include_segments=False
            )
            state, generated = final.state, final.generated_segments
            total, error_code = final.total_segments, final.error_code
            path, label = final.request_path, final.client_label
        except EchoActError:
            state = JobState.CANCELED if run.cancel.is_set() else run.job.state
            generated = sum(1 for s in run.segments if s.ready)
            total, error_code = len(run.segments), None
            path, label = run.job.request_path, run.job.client_label
        self._emit(
            Event(
                EventKind.FINISHED,
                job_id,
                state=state,
                generated=generated,
                total=total,
                request_path=path,
                client_label=label,
                error_code=error_code,
            )
        )

    def _prepare(self, run: _Run) -> None:
        """Resolve the model and load it, per F-09, F-17, F-84."""
        self._check_cancelled(run)
        self._set_state(run, JobState.PREPARING_MODEL)

        model_id = run.settings.model_id
        # F-09/5.3: the engine never downloads.  A model that is not present
        # is refused here, and preparing it is the owner's explicit action
        # in the model screen.
        model_dir, from_package = self._registry.resolve_dir(model_id)
        if from_package:
            log.info("model %s served from the package cache", model_id)

        if self._supervisor.would_need_load(model_id, run.budget):
            loaded = self._supervisor.load(model_id, model_dir, run.budget)
            run.sample_rate = loaded.sample_rate
        else:
            current = self._supervisor.loaded_model
            if current is not None:
                run.sample_rate = current.sample_rate
        self._check_cancelled(run)

    def _generate(self, run: _Run) -> None:
        self._set_state(run, JobState.GENERATING)
        job_id = run.job.job_id
        sampler = ResourceSampler(self._supervisor.worker_pid)
        start_frame = 0

        for seg in run.segments:
            self._check_cancelled(run)
            self._check_resources(run, sampler)

            gap_frames = wav.frames_for_ms(seg.trailing_silence_ms, run.sample_rate)
            if not seg.is_spoken:
                # F-27: a range that produces no audio still exists on the
                # timeline, attached to its neighbour.  Nothing is sent to
                # the engine, which A.5 showed would otherwise vocalise it.
                span = TimeRange(
                    wav.ms_for_frames(start_frame, run.sample_rate),
                    wav.ms_for_frames(start_frame + gap_frames, run.sample_rate),
                )
                self._store.mark_segment_ready(
                    job_id, seg.index, time=span, audio_path=None, frame_count=0
                )
                seg.time, seg.ready, seg.frame_count = span, True, 0
                run.segment_paths.append("")
                run.gaps.append(seg.trailing_silence_ms)
                start_frame += gap_frames
                self._emit_segment(run, seg)
                continue

            out = self._work_dir / job_id / f"{seg.index:05d}.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            reply = self._supervisor.synthesize(
                job_id=job_id,
                segment_index=seg.index,
                text=seg.spoken_text,
                lang=seg.language,
                voice_id=run.settings.voice_id,
                speed=_engine_speed(run.settings),
                out_path=out,
                total_steps=self._total_steps,
            )
            self._check_cancelled(run)

            span = TimeRange(
                wav.ms_for_frames(start_frame, run.sample_rate),
                wav.ms_for_frames(start_frame + reply.frame_count + gap_frames, run.sample_rate),
            )
            self._store.mark_segment_ready(
                job_id,
                seg.index,
                time=span,
                audio_path=str(out),
                frame_count=reply.frame_count,
            )
            seg.time = span
            seg.ready = True
            seg.frame_count = reply.frame_count
            seg.audio_path = str(out)
            run.segment_paths.append(str(out))
            run.gaps.append(seg.trailing_silence_ms)
            start_frame += reply.frame_count + gap_frames
            self._emit_segment(run, seg)

    def _finish(self, run: _Run) -> None:
        """Concatenate, record, and only then call the job Complete.

        5.1: "If generation finishes but saving fails, the job is not marked
        Complete."  So the transition is last, after the file exists and the
        row is written.
        """
        self._check_cancelled(run)
        job_id = run.job.job_id
        retained = run.job.retention is RetentionMode.RETAINED
        root = self._result_dir if retained else self._work_dir
        out = root / job_id / "result.wav"
        out.parent.mkdir(parents=True, exist_ok=True)

        spoken = [(p, g) for p, g in zip(run.segment_paths, run.gaps, strict=True) if p]
        if not spoken:
            raise EchoActError(
                Code.GENERATION_FAILED, "The text produced no audio.", detail={"job_id": job_id}
            )
        report = wav.concatenate(
            [p for p, _ in spoken],
            gaps_ms=[g for _, g in spoken],
            out_path=out,
            sample_rate=run.sample_rate,
        )
        now = ids.now()
        from ..domain import Result

        result = Result(
            result_id=ids.result_id(),
            job_id=job_id,
            sample_rate=report.sample_rate,
            channels=1,
            sample_width_bits=16,
            frame_count=report.frame_count,
            byte_size=report.byte_size,
            digest=wav.digest(out),
            created_at=now,
            expires_at=None if retained else now + ONEOFF_RESULT_TTL_S,
            relative_path=str(out),
        )
        self._store.attach_result(result)
        self._set_state(run, JobState.COMPLETE)

    def _fail(self, run: _Run, error: EchoActError) -> None:
        job_id = run.job.job_id
        if run.cancel.is_set():
            # A cancellation in flight looks like a failure from inside the
            # loop.  5.1 wants Canceled, and Complete is never overwritten
            # because the store refuses that transition.
            try:
                self._store.update_job_state(job_id, JobState.CANCELED)
            except (AssertionError, EchoActError):
                pass
            self._emit_state(run, JobState.CANCELED)
            return
        log.warning(job_context(job_id, "failed", code=error.code.value))
        try:
            self._store.record_job_error(job_id, error.code, error.message)
            self._store.update_job_state(job_id, JobState.FAILED)
        except (AssertionError, EchoActError):
            pass
        # F-19: the model is released on error as well as on cancellation.
        self._supervisor.kill()
        self._emit_state(run, JobState.FAILED, error_code=error.code.value)

    def _cleanup(self, run: _Run) -> None:
        """Remove the per-job scratch directory once nothing needs it.

        A retained job's segment audio stays: F-55 lets a client fetch
        individual segments, and F-31 replays a retained job with its
        mapping.  A one-off job's scratch is the result's own home until it
        expires, so it is left for the sweeper rather than deleted here.
        """
        if run.cancel.is_set() and run.job.retention is RetentionMode.ONE_OFF:
            scratch = self._work_dir / run.job.job_id
            shutil.rmtree(scratch, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _check_cancelled(self, run: _Run) -> None:
        if run.cancel.is_set():
            raise EchoActError(Code.GENERATION_FAILED, "Canceled.")

    def _check_resources(self, run: _Run, sampler: ResourceSampler) -> None:
        """F-23 and N-04, checked between segments.

        Between rather than during: the container is what stops a spike
        inside a single ONNX call, and N-03 is explicit that the enforced
        ceiling is the operating system's job.  This is the part that
        reports *why* and releases the model.
        """
        sample = sampler.poll()
        if sample is None:
            return
        decision = halt_decision(sample, run.budget)
        if decision.reason is HaltReason.NONE:
            return
        raise decision.to_error()

    def _set_state(self, run: _Run, state: JobState) -> None:
        self._store.update_job_state(run.job.job_id, state)
        run.job.state = state
        self._emit_state(run, state)

    def _emit_state(self, run: _Run, state: JobState, error_code: str | None = None) -> None:
        self._emit(
            Event(
                EventKind.STATE,
                run.job.job_id,
                state=state,
                generated=sum(1 for s in run.segments if s.ready),
                total=len(run.segments),
                request_path=run.job.request_path,
                client_label=run.job.client_label,
                error_code=error_code,
            )
        )

    def _emit_segment(self, run: _Run, seg: Segment) -> None:
        self._emit(
            Event(
                EventKind.SEGMENT,
                run.job.job_id,
                segment_index=seg.index,
                generated=sum(1 for s in run.segments if s.ready),
                total=len(run.segments),
                request_path=run.job.request_path,
                client_label=run.job.client_label,
                detail={
                    "start_ms": seg.time.start_ms if seg.time else 0,
                    "end_ms": seg.time.end_ms if seg.time else 0,
                    "frame_count": seg.frame_count,
                    "audio_path": seg.audio_path or "",
                },
            )
        )


def _engine_speed(settings: VoiceSettings) -> float:
    """F-07 and F-08 combined into the one number the engine takes.

    A style is a preset over tempo among other things, so the two multiply
    -- and the product is clamped back into F-07's advertised range, since
    a style must never take tempo somewhere the user is told is impossible.
    """
    from ..text.segment import effective_tempo

    return effective_tempo(settings)


class _Placeholder:
    """Marks the slot as taken between the check and the real run object.

    Without it, two submissions could both find the slot free while the
    first was still building its job -- the window is small and, with one
    slot and clients that retry, exactly the window that gets hit.
    """

    __slots__ = ()


_PLACEHOLDER: Any = _Placeholder()


def expire_one_off_results(store: Store, work_root: Path | None = None) -> int:
    """4.1's one-off lifetime, swept.

    Returns how many results were removed.  Retained data is never touched:
    N-16 forbids deleting explicitly retained results to reclaim space, and
    this is a lifetime sweep rather than a space one.
    """
    root = Path(work_root) if work_root else temp_dir()
    now = ids.now()
    removed = 0
    for job_id, result_id, _expires_at, path in _expired(store, now):
        try:
            store.delete_result(result_id)
        except EchoActError:
            continue
        removed += 1
        try:
            if path:
                Path(path).unlink(missing_ok=True)
            scratch = root / job_id
            if scratch.is_dir():
                shutil.rmtree(scratch, ignore_errors=True)
        except OSError:
            pass
    return removed


def _expired(store: Store, now: float) -> Iterable[tuple[str, str, float, str]]:
    conn = store._conn()  # noqa: SLF001 - the sweeper is part of the storage layer
    rows = conn.execute(
        "SELECT job_id, result_id, expires_at, relative_path FROM results"
        " WHERE expires_at IS NOT NULL AND expires_at <= ?",
        (now,),
    ).fetchall()
    return [(r["job_id"], r["result_id"], r["expires_at"], r["relative_path"]) for r in rows]


def clear_temp_tree(work_root: Path | None = None) -> int:
    """N-02: whatever a forced termination left behind, on relaunch.

    Returns the number of entries removed.  Only the scratch tree is
    touched; retained audio lives elsewhere precisely so that this can be
    unconditional.
    """
    root = Path(work_root) if work_root else temp_dir()
    if not root.is_dir():
        return 0
    removed = 0
    for child in root.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                os.unlink(child)
            removed += 1
        except OSError:
            continue
    return removed

"""The synthesis child process: ``python -m echoact.engine.worker``.

A.2 puts synthesis in its own process so that N-03 can put an operating-system
resource container around it and N-21 can measure it apart from the rest of
the app.  N-22 then gives five seconds to release a cancelled job's
resources, which means this process must be killable in the middle of a
segment.  Everything below follows from that:

* It holds a model, a voice-style cache, and nothing else.  No database, no
  job record, no file it named itself.  Killing it loses no durable state.
* Audio goes to the path the *parent* chose, is flushed and closed, and only
  then reported.  A worker killed mid-write leaves a file the parent already
  knows how to discard, instead of a half-line on the pipe.
* ``KeyboardInterrupt`` and ``SystemExit`` are never caught to keep going.
  The parent's escalation from terminate to kill is the mechanism N-22's
  deadline depends on, and a worker that survives it is a defect.
* Closed stdin means the parent is gone, and an orphaned worker holding a
  385 MB model must not outlive it, so the loop ends at EOF.

N-20 shapes the diagnostics: the engine's own exception messages quote the
offending characters of the input, so this module reports exception *types*,
lengths, and identifiers, and never forwards ``str(exc)`` to the parent or to
stderr.
"""

from __future__ import annotations

import errno
import gc
import logging
import os
import sys
import time
import wave
from pathlib import Path
from typing import Any, TextIO

import numpy as np
import psutil
import supertonic

from ..errors import Code, EchoActError
from ..paths import redact
from ..policy import ENGINE_MAX_CHUNK_CODEPOINTS, ENGINE_SILENCE_DURATION_S
from ..util.logging import configure, get_logger
from . import protocol as proto
from . import runtime

log = get_logger("engine.worker")

#: Where ``supertonic.pipeline.TTS`` keeps the four sessions its loader
#: builds.  F-87's check is worthless if it silently inspects nothing, so a
#: pipeline that does not expose all four is refused rather than trusted.
_SESSION_ATTRS: tuple[str, ...] = ("dp_ort", "text_enc_ort", "vector_est_ort", "vocoder_ort")

#: F-82 fixes the segment format: mono, 16-bit PCM, the model's native rate.
_CHANNELS = 1
_SAMPLE_WIDTH_BYTES = 2
_INT16_FULL_SCALE = 32767.0

#: The engine's own code for "language unknown"; F-05's automatic mode is
#: resolved per sentence by the parent, so this is a fallback for a segment
#: that reached us unresolved, not a routine path.
_ENGINE_UNKNOWN_LANG = "na"


def engine_lang(lang: str) -> str:
    """Map a job's language onto an engine language code."""
    code = (lang or "").strip().lower()
    if code in ("", "auto"):
        return _ENGINE_UNKNOWN_LANG
    return code


def pipeline_sessions(tts: Any) -> dict[str, Any]:
    """The ONNX sessions a loaded pipeline holds, by name.

    Reaching into the library's internals is deliberate: F-87 is a claim
    about the sessions that actually run, and the library exposes no other
    way to see them.  If a future version rearranges them, this raises
    instead of quietly verifying an empty set.
    """
    model = getattr(tts, "model", None)
    if model is None:
        raise EchoActError(
            Code.RUNTIME_PROVIDER_REFUSED,
            "The loaded pipeline exposes no inference sessions to verify.",
        )
    found = {name: getattr(model, name) for name in _SESSION_ATTRS if getattr(model, name, None)}
    if len(found) != len(_SESSION_ATTRS):
        missing = [name for name in _SESSION_ATTRS if name not in found]
        raise EchoActError(
            Code.RUNTIME_PROVIDER_REFUSED,
            "The loaded pipeline does not expose every inference session for verification.",
            detail={"missing": missing},
        )
    return found


def write_segment_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> int:
    """Write one segment and return the frame count read back from the file.

    F-82's format is written explicitly rather than left to a library
    default, and the count is read back from the closed file rather than
    taken from the array: the parent builds the whole job's time table from
    this number, so it has to describe the bytes that exist, not the bytes
    intended.
    """
    flat = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = np.round(np.clip(flat, -1.0, 1.0) * _INT16_FULL_SCALE).astype("<i2")

    target = Path(path)
    with open(target, "wb") as raw:
        with wave.open(raw, "wb") as out:
            out.setnchannels(_CHANNELS)
            out.setsampwidth(_SAMPLE_WIDTH_BYTES)
            out.setframerate(int(sample_rate))
            out.writeframes(pcm.tobytes())
        raw.flush()
        os.fsync(raw.fileno())

    with wave.open(str(target), "rb") as back:
        if (
            back.getnchannels() != _CHANNELS
            or back.getsampwidth() != _SAMPLE_WIDTH_BYTES
            or back.getframerate() != int(sample_rate)
        ):
            raise EchoActError(
                Code.GENERATION_FAILED,
                "The segment file was not written in the model's output format.",
            )
        return back.getnframes()


class Worker:
    """One loaded model and the message loop that drives it."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._tts: Any | None = None
        self._model_id = ""
        self._sample_rate = 0
        self._providers: list[str] = []
        self._voices: list[str] = []
        self._styles: dict[str, Any] = {}
        self._proc = psutil.Process()
        # First call establishes the baseline; every later call reports the
        # share since the previous one, which is what N-21 wants per segment.
        self._proc.cpu_percent(None)
        self.running = True

    # -- plumbing ------------------------------------------------------

    def _send(self, msg: Any) -> None:
        proto.write_message(self._out, msg)

    def _rss(self) -> int:
        try:
            return int(self._proc.memory_info().rss)
        except psutil.Error:
            return 0

    def _cpu_percent(self) -> float:
        """This process's share of *total logical capacity*.

        4.1 fixes that unit for every CPU number in the product, while
        psutil reports a process using two cores as 200%.  Reporting the raw
        figure would make the Stats line incomparable with the F-20 budget
        it exists to be checked against.
        """
        try:
            cores = psutil.cpu_count() or 1
            return round(float(self._proc.cpu_percent(None)) / cores, 2)
        except psutil.Error:
            return 0.0

    def announce_ready(self) -> None:
        self._send(proto.Ready(pid=os.getpid()))

    def fail(
        self,
        code: Code,
        message: str,
        *,
        fatal: bool,
        seq: int,
        job_id: str | None = None,
        segment_index: int | None = None,
    ) -> None:
        log.error("worker error code=%s fatal=%s seq=%s", code.value, fatal, seq)
        self._send(
            proto.Error(
                code=code.value,
                message=message,
                fatal=fatal,
                job_id=job_id,
                segment_index=segment_index,
                seq=seq,
            )
        )

    # -- dispatch ------------------------------------------------------

    def handle(self, msg: Any) -> None:
        if isinstance(msg, proto.Load):
            self._on_load(msg)
        elif isinstance(msg, proto.Synthesize):
            self._on_synthesize(msg)
        elif isinstance(msg, proto.Unload):
            self._on_unload(msg)
        elif isinstance(msg, proto.Ping):
            self._send(proto.Pong(rss_bytes=self._rss(), seq=msg.seq))
        elif isinstance(msg, proto.Shutdown):
            self._release()
            self.running = False
        else:
            # A reply arriving on the request pipe is the parent misbehaving;
            # guessing at it would be worse than saying so.
            self.fail(
                Code.INTERNAL,
                f"The worker received a {type(msg).__name__} message, which is not a request.",
                fatal=False,
                seq=getattr(msg, "seq", 0),
            )

    # -- load ----------------------------------------------------------

    def _on_load(self, msg: proto.Load) -> None:
        started = time.monotonic()
        # F-19: a new model or a new limit replaces whatever is resident.
        self._release()

        overreach = [p for p in msg.allowed_providers if p not in runtime.ALLOWED_PROVIDERS]
        if overreach:
            # The allow-list is the product's, not the caller's.  A parent
            # asking for more than local CPU is refused before any weights
            # are touched.
            self.fail(
                Code.RUNTIME_PROVIDER_REFUSED,
                "The requested execution providers exceed the local-only allow-list: "
                + ", ".join(f"{p} ({runtime.provider_kind(p)})" for p in overreach),
                fatal=True,
                seq=msg.seq,
            )
            return

        try:
            usable = runtime.requested_providers()
            refused = runtime.refused_providers()
            if refused:
                log.info("refusing offered execution providers: %s", ", ".join(refused))
            intra, inter = runtime.normalize_threads(msg.intra_op_threads, msg.inter_op_threads)
            log.info(
                "loading model=%s dir=%s providers=%s intra=%d inter=%d",
                msg.model_id,
                redact(msg.model_dir),
                ",".join(usable),
                intra,
                inter,
            )
            tts = supertonic.TTS(
                model=msg.model_id,
                model_dir=msg.model_dir,
                # F-63/F-84: the parent resolved and verified the manifest.
                # A worker that could download would make N-01's "no
                # automatic external communication" unprovable from here.
                auto_download=False,
                intra_op_num_threads=intra,
                inter_op_num_threads=inter,
            )
            providers = runtime.verify_sessions(pipeline_sessions(tts))
            sample_rate = int(tts.sample_rate)
            voices = [str(v) for v in getattr(tts, "voice_style_names", [])]
        except EchoActError as exc:
            self.fail(exc.code, exc.message, fatal=True, seq=msg.seq)
            return
        except FileNotFoundError:
            self.fail(
                Code.MODEL_NOT_READY,
                f"The model's files are not present under {redact(msg.model_dir)}.",
                fatal=True,
                seq=msg.seq,
            )
            return
        except MemoryError:
            self.fail(
                Code.INSUFFICIENT_RESOURCES,
                "There was not enough memory to load the model.",
                fatal=True,
                seq=msg.seq,
            )
            return
        except (ValueError, TypeError, KeyError):
            # Malformed config, a bad indexer, or a session the library
            # would not build: the files are there but unusable.  An id the
            # engine does not know is the one case that is not corruption,
            # and F-04 wants those distinguished rather than both reported
            # as a damaged model.
            if msg.model_id not in getattr(supertonic, "AVAILABLE_MODELS", ()):
                self.fail(
                    Code.MODEL_UNKNOWN,
                    f"The engine has no model named {msg.model_id!r}.",
                    fatal=True,
                    seq=msg.seq,
                )
            else:
                self.fail(
                    Code.MODEL_CORRUPT,
                    f"The model under {redact(msg.model_dir)} could not be loaded.",
                    fatal=True,
                    seq=msg.seq,
                )
            return
        except Exception as exc:
            self.fail(
                Code.GENERATION_FAILED,
                f"The model could not be loaded ({type(exc).__name__}).",
                fatal=True,
                seq=msg.seq,
            )
            return

        self._tts = tts
        self._model_id = msg.model_id
        self._sample_rate = sample_rate
        self._providers = providers
        self._voices = voices
        load_seconds = time.monotonic() - started
        log.info("loaded model=%s in %.2fs providers=%s", msg.model_id, load_seconds, providers)
        self._send(
            proto.Loaded(
                model_id=msg.model_id,
                sample_rate=sample_rate,
                voices=voices,
                providers=list(providers),
                load_seconds=load_seconds,
                seq=msg.seq,
            )
        )

    # -- synthesize ----------------------------------------------------

    def _style(self, voice_id: str) -> Any:
        """One voice style per voice, reused across a job's segments.

        Reloading the style per segment would re-read and re-parse a JSON
        vector for every sentence of a document, which N-06's time-to-first-
        audio has no reason to pay more than once.
        """
        cached = self._styles.get(voice_id)
        if cached is None:
            assert self._tts is not None
            cached = self._tts.get_voice_style(voice_id)
            self._styles[voice_id] = cached
        return cached

    def _on_synthesize(self, msg: proto.Synthesize) -> None:
        if self._tts is None:
            self.fail(
                Code.MODEL_NOT_READY,
                "No model is loaded in the synthesis worker.",
                fatal=False,
                seq=msg.seq,
                job_id=msg.job_id,
                segment_index=msg.segment_index,
            )
            return
        if not msg.text.strip():
            # F-27 allows a segment with nothing to speak; it must never
            # reach the engine, which raises on empty text.
            self.fail(
                Code.INPUT_EMPTY,
                "The segment contains nothing to speak.",
                fatal=False,
                seq=msg.seq,
                job_id=msg.job_id,
                segment_index=msg.segment_index,
            )
            return

        started = time.monotonic()
        try:
            style = self._style(msg.voice_id)
        except FileNotFoundError:
            self.fail(
                Code.VOICE_UNKNOWN,
                f"The model has no voice {msg.voice_id!r}.",
                fatal=False,
                seq=msg.seq,
                job_id=msg.job_id,
                segment_index=msg.segment_index,
            )
            return
        except Exception as exc:
            self.fail(
                Code.GENERATION_FAILED,
                f"The voice {msg.voice_id!r} could not be loaded ({type(exc).__name__}).",
                fatal=False,
                seq=msg.seq,
                job_id=msg.job_id,
                segment_index=msg.segment_index,
            )
            return

        try:
            wav, _engine_duration = self._tts.synthesize(
                msg.text,
                style,
                total_steps=msg.total_steps,
                speed=msg.speed,
                # Both arguments make one of our segments exactly one engine
                # call: A.5 measured the engine re-chunking Korean at 120
                # characters, and its own inter-chunk silence is inert once
                # we chunk first, so F-82's silence stays the parent's.
                max_chunk_length=ENGINE_MAX_CHUNK_CODEPOINTS,
                silence_duration=ENGINE_SILENCE_DURATION_S,
                lang=engine_lang(msg.lang),
            )
            synth_seconds = time.monotonic() - started
            samples = np.asarray(wav, dtype=np.float32).reshape(-1)
            peak = float(np.max(np.abs(samples))) if samples.size else 0.0
            frame_count = write_segment_wav(msg.out_path, samples, self._sample_rate)
        except MemoryError:
            # F-23: a job that cannot fit halts, and the parent will kill a
            # worker whose address space is already exhausted.
            self.fail(
                Code.OUT_OF_MEMORY,
                "The worker ran out of memory while generating this segment.",
                fatal=True,
                seq=msg.seq,
                job_id=msg.job_id,
                segment_index=msg.segment_index,
            )
            return
        except EchoActError as exc:
            self.fail(
                exc.code,
                exc.message,
                fatal=exc.code is Code.RUNTIME_PROVIDER_REFUSED,
                seq=msg.seq,
                job_id=msg.job_id,
                segment_index=msg.segment_index,
            )
            return
        except OSError as exc:
            full = exc.errno == errno.ENOSPC
            self.fail(
                Code.STORAGE_FULL if full else Code.GENERATION_FAILED,
                f"The segment could not be written to {redact(msg.out_path)}.",
                fatal=False,
                seq=msg.seq,
                job_id=msg.job_id,
                segment_index=msg.segment_index,
            )
            return
        except Exception as exc:
            # One segment failing is one non-fatal error: whether the job
            # continues is the parent's decision, not the worker's.  The
            # engine quotes offending characters in its own messages, so
            # only the exception type crosses the pipe (N-20).
            log.warning(
                "segment failed job=%s index=%d chars=%d error=%s",
                msg.job_id,
                msg.segment_index,
                len(msg.text),
                type(exc).__name__,
            )
            self.fail(
                Code.GENERATION_FAILED,
                f"Speech generation failed for this segment ({type(exc).__name__}).",
                fatal=False,
                seq=msg.seq,
                job_id=msg.job_id,
                segment_index=msg.segment_index,
            )
            return

        self._send(
            proto.Audio(
                job_id=msg.job_id,
                segment_index=msg.segment_index,
                out_path=str(msg.out_path),
                frame_count=frame_count,
                sample_rate=self._sample_rate,
                synth_seconds=synth_seconds,
                peak=peak,
                seq=msg.seq,
            )
        )
        # N-21 measures the generation job separately; reporting after the
        # segment costs nothing and spares the parent a poll on the request
        # path, which is what the Stats message exists for.
        self._send(
            proto.Stats(rss_bytes=self._rss(), cpu_percent=self._cpu_percent(), seq=msg.seq)
        )

    # -- release -------------------------------------------------------

    def _release(self) -> None:
        self._tts = None
        self._styles.clear()
        self._model_id = ""
        self._sample_rate = 0
        self._providers = []
        self._voices = []
        gc.collect()

    def _on_unload(self, msg: proto.Unload) -> None:
        """F-19's explicit release.

        The protocol has no acknowledgement type for Unload, so the reply is
        a Stats line carrying the same ``seq``: the fact worth knowing about
        a release is how much memory came back, and a parent that treats
        Stats as unsolicited simply ignores it.
        """
        self._release()
        self._send(
            proto.Stats(rss_bytes=self._rss(), cpu_percent=self._cpu_percent(), seq=msg.seq)
        )


def serve(stdin: TextIO, stdout: TextIO) -> int:
    """Read requests until Shutdown or end of input.

    EOF is a normal exit: it means the parent's pipe is gone, and N-22's
    resource release cannot depend on a worker that keeps a model resident
    after its parent has died.
    """
    worker = Worker(stdout)
    worker.announce_ready()
    while worker.running:
        line = stdin.readline()
        if not line:
            log.info("stdin closed; worker exiting")
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = proto.decode(line)
        except (ValueError, TypeError):
            # Framing is one JSON object per line; a line that is not one is
            # a protocol error, never something to guess at.
            worker.fail(
                Code.INTERNAL,
                "The worker received a line that is not a protocol message.",
                fatal=False,
                seq=0,
            )
            continue
        worker.handle(msg)
    return 0


def _reconfigure_stdio() -> None:
    """Force UTF-8 framing regardless of the console code page.

    On this machine the default console encoding is cp949, which cannot
    represent every character a document may contain; the protocol fixes
    UTF-8, so the streams are pinned rather than inherited.
    """
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        log.warning("stdio streams could not be reconfigured to UTF-8")


def main() -> int:
    configure(level=logging.INFO, to_file=False)
    _reconfigure_stdio()
    try:
        return serve(sys.stdin, sys.stdout)
    except Exception as exc:
        # Anything escaping the loop ends the worker, but the parent gets a
        # code rather than a silent exit followed by WORKER_LOST.
        proto.write_message(
            sys.stdout,
            proto.Error(
                code=Code.INTERNAL.value,
                message=f"The synthesis worker stopped ({type(exc).__name__}).",
                fatal=True,
            ),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

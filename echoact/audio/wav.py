"""F-82's audio format: mono 16-bit PCM WAV at the model's native rate.

Every function here answers one sentence of F-82 -- "the full WAV is a
bit-exact concatenation of its segment audio together with any inter-segment
silence attributed by F-08" -- and F-16's partial export, which exists only
because that concatenation is already defined.

Two implementation choices are deliberate:

* The standard library's ``wave`` module plus numpy, not ``soundfile``.  The
  byte layout of a result is a requirement (F-82) and its digest is stored
  and compared (4.2), so the bytes have to be ours rather than libsndfile's.
  ``soundfile`` remains the right tool for reading a file a *user* supplies;
  nothing about a file we wrote ourselves should need it.
* Concatenation copies raw frames block by block and never materialises a
  job's audio.  N-21 forbids unbounded in-memory loading, and it is reachable
  here rather than theoretical: F-03's 50,000 code points is roughly two
  hours of speech, about 700 MB of PCM.

Sample rate is a parameter everywhere and a constant nowhere.  F-82 says the
rate is the model's and is never resampled, so a second model with a
different rate must not require a change in this file.
"""

from __future__ import annotations

import errno
import hashlib
import os
import struct
import wave
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np

from ..domain import Segment, TimeRange
from ..errors import Code, EchoActError
from ..paths import redact

#: F-82's format, fixed for every segment and every result.
CHANNELS: Final = 1
SAMPLE_WIDTH_BYTES: Final = 2
SAMPLE_WIDTH_BITS: Final = 16
FORMAT_NAME: Final = "wav_pcm_s16le_mono"

INT16_MIN: Final = -32768
INT16_MAX: Final = 32767
#: Full scale for float -> int16.  32767 rather than 32768 keeps +1.0 and
#: -1.0 symmetric, so a normalised signal never clips merely by reaching full
#: scale; genuine overshoot still clips and ``WriteReport.clipped`` says so.
FLOAT_FULL_SCALE: Final = 32767.0
#: Full scale for reporting a *peak*, which is a magnitude: int16 can hold
#: -32768, so dividing by 32768 keeps an untouched int16 signal at or below
#: 1.0 and reserves "above 1.0" to mean the source really did overshoot.
PEAK_FULL_SCALE: Final = 32768.0

# A RIFF header is 32-bit: the data chunk's size and the RIFF chunk's size
# (the file size minus 8, i.e. 36 + payload for the canonical header) must
# both fit in an unsigned 32-bit field.  Beyond that the *format* cannot
# describe the file -- no amount of disk space helps -- so the payload is
# capped here and checked before anything is written.  Section 8's inputs
# come nowhere near it, which ``test_wav.py`` asserts rather than assumes.
RIFF_MAX_CHUNK_BYTES: Final = 0xFFFF_FFFF
CANONICAL_HEADER_BYTES: Final = 44
MAX_PCM_BYTES: Final = RIFF_MAX_CHUNK_BYTES - (CANONICAL_HEADER_BYTES - 8)
MAX_FRAMES: Final = MAX_PCM_BYTES // (CHANNELS * SAMPLE_WIDTH_BYTES)

#: Frames copied per read/write while streaming.  1 MiB of PCM.
BLOCK_FRAMES: Final = 512 * 1024
#: Bytes hashed per read in :func:`digest`.
DIGEST_BLOCK_BYTES: Final = 1 << 20

_PART_SUFFIX: Final = ".part"


# ======================================================================
# Reports
# ======================================================================


@dataclass(frozen=True, slots=True)
class WriteReport:
    """What :func:`write_segment` put on disk.

    ``frame_count`` is what a segment's time range is built from -- F-82
    forbids resampling, so frames over the model's rate is the exact duration
    and nothing has to trust the engine's own idea of seconds.
    """

    path: str
    sample_rate: int
    frame_count: int
    byte_size: int
    #: Largest absolute amplitude, 1.0 being full scale.  Above 1.0 means the
    #: source overshot and was clipped.
    peak: float
    #: Samples that had to be clipped.  Silently flattening a signal is the
    #: kind of defect Section 8.3 calls release-blocking, so it is counted.
    clipped: int

    @property
    def duration_ms(self) -> int:
        return ms_for_frames(self.frame_count, self.sample_rate)


@dataclass(frozen=True, slots=True)
class WavInfo:
    """Everything 4.2's Result entity records about a file except its digest.

    ``format`` describes what was actually found, not what was expected: a
    probe of a foreign file reports it honestly and lets the caller decide,
    while :func:`read_wav` and :func:`concatenate` refuse it.
    """

    path: str
    format: str
    sample_rate: int
    channels: int
    sample_width_bits: int
    frame_count: int
    byte_size: int
    duration_ms: int

    @property
    def is_output_format(self) -> bool:
        """True when this is the format F-82 fixes for segments and results."""
        return (
            self.format == FORMAT_NAME
            and self.channels == CHANNELS
            and self.sample_width_bits == SAMPLE_WIDTH_BITS
        )


@dataclass(frozen=True, slots=True)
class ConcatReport:
    """The result of :func:`concatenate`, including the time table.

    ``spans`` is per input segment and each one *includes* the silence
    written after it: F-27 attributes inter-segment silence to the preceding
    segment, and 4.2's times are milliseconds from the start of the audio, so
    the spans tile the file with no holes.
    """

    path: str
    sample_rate: int
    frame_count: int
    byte_size: int
    segment_frames: tuple[int, ...]
    gap_frames: tuple[int, ...]
    spans: tuple[TimeRange, ...]

    @property
    def duration_ms(self) -> int:
        return ms_for_frames(self.frame_count, self.sample_rate)


@dataclass(frozen=True, slots=True)
class PartialExport:
    """F-16's numbers.  Naming and labelling are the caller's business.

    F-16 requires the user to be told how much of the source text the file
    covers, and requires a file exported mid-job never to be mistaken for the
    whole document.  Both need a figure rather than a flag, so this carries
    the counts and lets the GUI, REST, and MCP phrase them.
    """

    path: str
    sample_rate: int
    frame_count: int
    byte_size: int
    spans: tuple[TimeRange, ...]
    segment_count: int
    total_segments: int
    covered_codepoints: int
    total_codepoints: int
    #: True only when every segment was exported and the file reaches the end
    #: of the source text.  Anything else is presented as partial.
    complete: bool

    @property
    def duration_ms(self) -> int:
        return ms_for_frames(self.frame_count, self.sample_rate)

    @property
    def coverage(self) -> float:
        """Fraction of the source text's code points the file covers, 0 to 1."""
        if self.total_codepoints <= 0:
            return 1.0 if self.complete else 0.0
        return min(1.0, self.covered_codepoints / self.total_codepoints)


# ======================================================================
# Frame arithmetic
# ======================================================================


def frames_for_ms(ms: int, sample_rate: int) -> int:
    """Exact silence, rounded to the nearest frame.

    F-08's pauses are given in milliseconds and a file is counted in frames;
    at 44,100 Hz a 250 ms pause is exactly 11,025 frames, and the rounding
    here only matters for a rate where it is not exact.
    """
    if ms < 0:
        raise EchoActError(Code.INTERNAL, "A pause cannot be negative.", detail={"ms": ms})
    return (ms * sample_rate + 500) // 1000


def ms_for_frames(frames: int, sample_rate: int) -> int:
    """Milliseconds from the start of the audio, matching ``Result.duration_ms``."""
    if sample_rate <= 0:
        raise EchoActError(Code.INTERNAL, "A sample rate must be positive.")
    return int(round(frames * 1000 / sample_rate))


def max_duration_ms(sample_rate: int) -> int:
    """Longest audio a 32-bit RIFF header can describe at this rate."""
    return ms_for_frames(MAX_FRAMES, sample_rate)


# ======================================================================
# Writing
# ======================================================================


def write_segment(
    path: str | os.PathLike[str],
    samples: np.ndarray,
    sample_rate: int,
) -> WriteReport:
    """Write one segment's audio in F-82's format.

    Accepts the engine's float output or int16 directly.  Float is scaled and
    rounded to nearest, then clipped -- ``astype(np.int16)`` alone truncates
    toward zero *and* wraps on overflow, which turns a loud sample into an
    equally loud sample of the opposite sign, so neither step is optional.

    The file appears at ``path`` only once it is complete: it is written
    beside the target and renamed.  A worker killed mid-write therefore
    leaves no file at all, which is stronger than the protocol's rule that
    the parent discards a file it has not been told about.
    """
    pcm, frame_count, peak, clipped = _to_pcm(samples)
    target = Path(path)
    _check_capacity(len(pcm), target)
    with _open_writer(target, sample_rate) as writer:
        writer.writeframesraw(pcm)
    return WriteReport(
        path=str(target),
        sample_rate=sample_rate,
        frame_count=frame_count,
        byte_size=_size_of(target),
        peak=peak,
        clipped=clipped,
    )


def _to_pcm(samples: np.ndarray) -> tuple[bytes, int, float, int]:
    arr = np.asarray(samples)
    if arr.ndim == 2 and 1 in arr.shape:
        arr = arr.reshape(-1)
    if arr.ndim != 1:
        raise EchoActError(
            Code.INTERNAL,
            "Segment audio must be mono.",
            detail={"shape": list(arr.shape)},
        )

    if arr.dtype == np.int16:
        peak = float(np.abs(arr.astype(np.int32)).max()) / PEAK_FULL_SCALE if arr.size else 0.0
        pcm = np.ascontiguousarray(arr, dtype="<i2")
        clipped = 0
    elif np.issubdtype(arr.dtype, np.floating):
        floats = arr if arr.dtype == np.float64 else arr.astype(np.float32, copy=False)
        if arr.size and not bool(np.isfinite(floats).all()):
            raise EchoActError(Code.INTERNAL, "Segment audio contains NaN or infinity.")
        peak = float(np.abs(floats).max()) if arr.size else 0.0
        scaled = np.rint(floats * FLOAT_FULL_SCALE)
        clipped = int(np.count_nonzero((scaled < INT16_MIN) | (scaled > INT16_MAX)))
        pcm = np.clip(scaled, INT16_MIN, INT16_MAX).astype("<i2")
    else:
        raise EchoActError(
            Code.INTERNAL,
            "Segment audio must be float or int16.",
            detail={"dtype": str(arr.dtype)},
        )
    return pcm.tobytes(), int(pcm.size), peak, clipped


# ======================================================================
# Reading
# ======================================================================


def probe(path: str | os.PathLike[str]) -> WavInfo:
    """Read only the header.  Cheap enough to call on every segment.

    This is how a Result's format, length, and size in 4.2 are obtained
    without reading the audio, which N-21 requires for a file that can be
    hundreds of megabytes.
    """
    target = Path(path)
    size = _size_of(target)
    with _open_reader(target) as reader:
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        rate = reader.getframerate()
        frames = reader.getnframes()
        comptype = reader.getcomptype()
    if rate <= 0:
        raise EchoActError(
            Code.FILE_CORRUPT,
            "The WAV header declares no sample rate.",
            detail={"path": redact(target)},
        )
    return WavInfo(
        path=str(target),
        format=_format_name(comptype, width, channels),
        sample_rate=rate,
        channels=channels,
        sample_width_bits=width * 8,
        frame_count=frames,
        byte_size=size,
        duration_ms=ms_for_frames(frames, rate),
    )


def read_wav(path: str | os.PathLike[str]) -> tuple[np.ndarray, int]:
    """Read a whole file we wrote, as int16 samples and its rate.

    Refuses anything that is not F-82's format rather than silently coping,
    because coping would mean resampling or downmixing and F-82 forbids both.

    This loads the file.  That is right for a segment, which F-81 caps at
    twenty seconds, and wrong for a job's full WAV; use :func:`iter_blocks`
    or :func:`probe` for those, per N-21.
    """
    target = Path(path)
    info = probe(target)
    require_output_format(info)
    with _open_reader(target) as reader:
        raw = _read_all(reader, info.frame_count, target)
    return np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True), info.sample_rate


def iter_blocks(
    path: str | os.PathLike[str],
    *,
    block_frames: int = BLOCK_FRAMES,
) -> Iterator[np.ndarray]:
    """Stream a file in int16 blocks, for anything that must not hold it all.

    N-21's "no unbounded in-memory loading" covers playback and result
    retrieval as much as export, so the bounded read path is public.

    The file is checked before the iterator is returned, not on the first
    ``next``: a player that builds its source ahead of time should learn
    about a missing or foreign file then, not mid-stream.
    """
    if block_frames <= 0:
        raise EchoActError(Code.INTERNAL, "A block must contain at least one frame.")
    target = Path(path)
    require_output_format(probe(target))
    return _iter_blocks(target, block_frames)


def _iter_blocks(target: Path, block_frames: int) -> Iterator[np.ndarray]:
    with _open_reader(target) as reader:
        while True:
            raw = _read_block(reader, block_frames, target)
            if not raw:
                return
            yield np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True)


def require_output_format(info: WavInfo) -> None:
    """Raise unless ``info`` describes the format F-82 fixes."""
    if info.is_output_format:
        return
    raise EchoActError(
        Code.FILE_CORRUPT,
        "That file is not the mono 16-bit PCM WAV this app writes.",
        detail={
            "path": redact(info.path),
            "found": info.format,
            "expected": FORMAT_NAME,
        },
    )


def digest(path: str | os.PathLike[str]) -> str:
    """SHA-256 of the whole file, streamed.

    4.2 stores this as a Result's integrity verification data.  It covers the
    header as well as the audio, so a result whose header was rewritten is a
    different result -- which is the point of recording it.
    """
    target = Path(path)
    hasher = hashlib.sha256()
    try:
        with open(target, "rb") as handle:
            while True:
                block = handle.read(DIGEST_BLOCK_BYTES)
                if not block:
                    break
                hasher.update(block)
    except FileNotFoundError as exc:
        raise EchoActError(Code.FILE_NOT_FOUND, detail={"path": redact(target)}) from exc
    except OSError as exc:
        raise _os_error(exc, target) from exc
    return hasher.hexdigest()


# ======================================================================
# Concatenation (F-82) and partial export (F-16)
# ======================================================================


def concatenate(
    segment_paths: Sequence[str | os.PathLike[str] | None],
    gaps_ms: Sequence[int],
    out_path: str | os.PathLike[str],
    sample_rate: int,
) -> ConcatReport:
    """Join segment audio into one file, bit-exactly.

    ``gaps_ms[i]`` is written *after* segment ``i``, the last one included:
    F-27 attributes inter-segment silence to the segment that precedes it, so
    a segment's span ends where the next segment's audio begins.  Whether the
    final segment has a trailing pause is the caller's policy, expressed by
    passing zero; deciding it here would put F-08's presets in the wrong
    module and would break the property F-16 relies on, that exporting again
    later yields a file with the same prefix.

    A ``None`` path is a segment that produced no audio -- F-27's attachment
    rule should make these rare -- and contributes only its silence, so the
    spans still line up one-to-one with the segments.

    Every input is probed before the output is opened, so a mixed rate or a
    damaged segment fails without leaving a file behind.  Frames are copied
    in blocks and never accumulated (N-21).
    """
    paths = [None if p is None else Path(p) for p in segment_paths]
    gaps = [int(g) for g in gaps_ms]
    if len(gaps) != len(paths):
        raise EchoActError(
            Code.INTERNAL,
            "Each segment needs exactly one trailing pause.",
            detail={"segments": len(paths), "gaps": len(gaps)},
        )
    if not paths:
        raise EchoActError(
            Code.SEGMENT_NOT_READY,
            "There is no generated audio to write yet.",
        )
    if sample_rate <= 0:
        raise EchoActError(Code.INTERNAL, "A sample rate must be positive.")

    segment_frames: list[int] = []
    for source in paths:
        if source is None:
            segment_frames.append(0)
            continue
        info = probe(source)
        require_output_format(info)
        if info.sample_rate != sample_rate:
            # F-82: one job, one format.  Resampling is the only alternative
            # and the requirement rules it out, so this is a hard stop.
            raise EchoActError(
                Code.INTERNAL,
                "Segment audio does not share the job's sample rate.",
                detail={
                    "path": redact(source),
                    "found": info.sample_rate,
                    "expected": sample_rate,
                },
            )
        segment_frames.append(info.frame_count)

    gap_frames = [frames_for_ms(g, sample_rate) for g in gaps]
    total_frames = sum(segment_frames) + sum(gap_frames)
    target = Path(out_path)
    _check_capacity(total_frames * SAMPLE_WIDTH_BYTES, target)

    with _open_writer(target, sample_rate) as writer:
        for source, silence in zip(paths, gap_frames, strict=True):
            if source is not None:
                _copy_frames(source, writer)
            _write_silence(writer, silence)

    byte_size = _size_of(target)
    written = byte_size - CANONICAL_HEADER_BYTES
    if written != total_frames * SAMPLE_WIDTH_BYTES:
        raise EchoActError(
            Code.INTERNAL,
            "The joined file is not the size its segments add up to.",
            detail={"expected": total_frames * SAMPLE_WIDTH_BYTES, "found": written},
        )
    return ConcatReport(
        path=str(target),
        sample_rate=sample_rate,
        frame_count=total_frames,
        byte_size=byte_size,
        segment_frames=tuple(segment_frames),
        gap_frames=tuple(gap_frames),
        spans=_spans(segment_frames, gap_frames, sample_rate),
    )


def export_partial(
    segments: Sequence[Segment],
    out_path: str | os.PathLike[str],
    *,
    sample_rate: int,
    total_codepoints: int,
) -> PartialExport:
    """F-16: export what has been generated so far, and say how much that is.

    Only the *leading* run of ready segments is written.  Taking every ready
    segment and skipping the holes would be wrong twice over: the file would
    silently omit text from its middle, and every time range after the hole
    would disagree with the job's, so the exported file could not be followed
    against the highlighting the user already sees.

    Each exported segment's trailing silence is included, the last one's too.
    That is what makes a later export a longer file with the same prefix
    rather than a differently aligned one -- F-16 asks for exactly that, and
    forbids treating the second export as a continuation.

    The digest 4.2 wants is not computed here; hashing hundreds of megabytes
    is not free and an export to a user's folder never needs one.  Call
    :func:`digest` when a Result is being recorded.
    """
    ready = _leading_ready(segments)
    if not ready:
        raise EchoActError(
            Code.SEGMENT_NOT_READY,
            "No segment has been generated yet, so there is nothing to save.",
            detail={"total_segments": len(segments)},
        )
    paths: list[str | None] = []
    for segment in ready:
        if segment.audio_path is None and segment.is_spoken:
            raise EchoActError(
                Code.SEGMENT_NOT_READY,
                "A segment is marked ready but has no audio.",
                detail={"segment_index": segment.index},
            )
        paths.append(segment.audio_path)
    gaps = [segment.trailing_silence_ms for segment in ready]

    report = concatenate(paths, gaps, out_path, sample_rate)

    covered = min(max(ready[-1].source.end, 0), max(total_codepoints, 0))
    complete = len(ready) == len(segments) and covered >= total_codepoints
    return PartialExport(
        path=report.path,
        sample_rate=report.sample_rate,
        frame_count=report.frame_count,
        byte_size=report.byte_size,
        spans=report.spans,
        segment_count=len(ready),
        total_segments=len(segments),
        covered_codepoints=covered,
        total_codepoints=total_codepoints,
        complete=complete,
    )


def _leading_ready(segments: Sequence[Segment]) -> list[Segment]:
    run: list[Segment] = []
    for segment in segments:
        if not segment.ready:
            break
        run.append(segment)
    return run


def _spans(
    segment_frames: Sequence[int],
    gap_frames: Sequence[int],
    sample_rate: int,
) -> tuple[TimeRange, ...]:
    spans: list[TimeRange] = []
    cursor = 0
    for audio, silence in zip(segment_frames, gap_frames, strict=True):
        start = cursor
        cursor += audio + silence
        spans.append(
            TimeRange(
                start_ms=ms_for_frames(start, sample_rate),
                end_ms=ms_for_frames(cursor, sample_rate),
            )
        )
    return tuple(spans)


# ======================================================================
# File plumbing.  No OSError and no wave.Error leaves this module (rule 3).
# ======================================================================


@contextmanager
def _open_writer(path: Path, sample_rate: int) -> Iterator[wave.Wave_write]:
    if sample_rate <= 0:
        raise EchoActError(Code.INTERNAL, "A sample rate must be positive.")
    temp = path.with_name(path.name + _PART_SUFFIX)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = wave.open(str(temp), "wb")
    except OSError as exc:
        raise _os_error(exc, path) from exc
    try:
        try:
            writer.setnchannels(CHANNELS)
            writer.setsampwidth(SAMPLE_WIDTH_BYTES)
            writer.setframerate(sample_rate)
            yield writer
        finally:
            # close() is where ``wave`` patches the RIFF and data sizes, so a
            # payload too large for a 32-bit field fails here, not earlier.
            writer.close()
    except OSError as exc:
        _discard(temp)
        raise _os_error(exc, path) from exc
    except struct.error as exc:
        # The only field that can overflow here is a 32-bit RIFF size, and
        # ``_check_capacity`` should have caught it before a byte was written.
        _discard(temp)
        raise EchoActError(
            Code.INTERNAL,
            "The audio is longer than a WAV file can describe.",
            detail={"path": redact(path), "max_pcm_bytes": MAX_PCM_BYTES},
        ) from exc
    except wave.Error as exc:
        _discard(temp)
        raise EchoActError(
            Code.INTERNAL,
            "The audio file could not be written.",
            detail={"path": redact(path)},
        ) from exc
    except BaseException:
        _discard(temp)
        raise
    try:
        os.replace(temp, path)
    except OSError as exc:
        _discard(temp)
        raise _os_error(exc, path) from exc


@contextmanager
def _open_reader(path: Path) -> Iterator[wave.Wave_read]:
    try:
        reader = wave.open(str(path), "rb")
    except FileNotFoundError as exc:
        raise EchoActError(Code.FILE_NOT_FOUND, detail={"path": redact(path)}) from exc
    except OSError as exc:
        raise _os_error(exc, path) from exc
    except (wave.Error, EOFError) as exc:
        raise EchoActError(
            Code.FILE_CORRUPT,
            "That file is not a readable WAV.",
            detail={"path": redact(path)},
        ) from exc
    try:
        yield reader
    finally:
        reader.close()


def _copy_frames(source: Path, writer: wave.Wave_write) -> None:
    with _open_reader(source) as reader:
        while True:
            raw = _read_block(reader, BLOCK_FRAMES, source)
            if not raw:
                return
            writer.writeframesraw(raw)


def _write_silence(writer: wave.Wave_write, frames: int) -> None:
    remaining = frames
    while remaining > 0:
        block = min(remaining, BLOCK_FRAMES)
        writer.writeframesraw(bytes(block * SAMPLE_WIDTH_BYTES))
        remaining -= block


def _read_block(reader: wave.Wave_read, frames: int, path: Path) -> bytes:
    try:
        return reader.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise EchoActError(
            Code.FILE_CORRUPT,
            "That WAV ends before its header says it should.",
            detail={"path": redact(path)},
        ) from exc
    except OSError as exc:
        raise _os_error(exc, path) from exc


def _read_all(reader: wave.Wave_read, frames: int, path: Path) -> bytes:
    raw = _read_block(reader, frames, path)
    if len(raw) != frames * SAMPLE_WIDTH_BYTES:
        raise EchoActError(
            Code.FILE_CORRUPT,
            "That WAV holds less audio than its header declares.",
            detail={"path": redact(path)},
        )
    return raw


def _check_capacity(pcm_bytes: int, path: Path) -> None:
    if pcm_bytes > MAX_PCM_BYTES:
        raise EchoActError(
            Code.INTERNAL,
            "The audio is longer than a WAV file can describe.",
            detail={
                "path": redact(path),
                "pcm_bytes": pcm_bytes,
                "max_pcm_bytes": MAX_PCM_BYTES,
            },
        )


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError as exc:
        raise EchoActError(Code.FILE_NOT_FOUND, detail={"path": redact(path)}) from exc
    except OSError as exc:
        raise _os_error(exc, path) from exc


def _format_name(comptype: str, width_bytes: int, channels: int) -> str:
    if comptype != "NONE":
        return f"wav_{comptype.lower()}"
    layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels}ch")
    return f"wav_pcm_s{width_bytes * 8}le_{layout}"


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Discarding the half-written temporary is already the failure path;
        # N-02's relaunch cleanup covers whatever survives in the temp tree.
        pass


def _os_error(exc: OSError, path: Path) -> EchoActError:
    """Map the filesystem's complaint onto a code the surfaces already know."""
    detail = {"path": redact(path)}
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return EchoActError(Code.STORAGE_FULL, detail=detail, cause=exc)
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return EchoActError(Code.FILE_PERMISSION, detail=detail, cause=exc)
    if exc.errno == errno.ENOENT:
        return EchoActError(Code.FILE_NOT_FOUND, detail=detail, cause=exc)
    return EchoActError(
        Code.INTERNAL,
        "The audio file could not be written or read.",
        detail=detail,
        cause=exc,
    )


__all__ = [
    "BLOCK_FRAMES",
    "CANONICAL_HEADER_BYTES",
    "CHANNELS",
    "FORMAT_NAME",
    "MAX_FRAMES",
    "MAX_PCM_BYTES",
    "SAMPLE_WIDTH_BITS",
    "SAMPLE_WIDTH_BYTES",
    "ConcatReport",
    "PartialExport",
    "WavInfo",
    "WriteReport",
    "concatenate",
    "digest",
    "export_partial",
    "frames_for_ms",
    "iter_blocks",
    "max_duration_ms",
    "ms_for_frames",
    "probe",
    "read_wav",
    "require_output_format",
    "write_segment",
]

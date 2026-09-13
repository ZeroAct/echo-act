"""Streaming playback with a frame-accurate clock.

F-12 starts playback at the first ready segment and continues into segments
that do not exist yet, so this is not a file player with a progress bar: it
is a timeline that grows while it is being consumed.

Three decisions come straight from requirements.

**The clock is the audio callback's own frame counter, anchored to the
device's DAC time.**  N-12 allows 300 ms between the sound and the
highlight, and it also asks for the output device's latency as a separate
figure.  A player that reports "where I think I am" cannot supply either;
PortAudio hands the callback the moment its buffer will be *heard*, so
:meth:`Player.position_ms` extrapolates from that instant and is right to
within a buffer even between callbacks.

**Starvation does not advance the clock.**  When the next segment is not
generated yet the callback emits silence, but the timeline stands still --
otherwise the mark would run off into text nobody has heard.  Section 5.2's
"waiting for next segment" is that state, and F-28 keeps the previous
segment marked while it lasts.

**Audio is streamed from disk, never held whole.**  N-21 forbids unbounded
in-memory loading and a 50,000 character document is around two hours of
44.1 kHz mono, so a feeder thread keeps a couple of seconds ahead and no
more.

This module is deliberately free of Qt: the GUI polls
:meth:`Player.position_ms` on its own repaint timer and pushes the value
into the reading surface.  That is a repaint cadence, not a clock.
"""

from __future__ import annotations

import threading
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import numpy as np

from ..domain import Segment
from ..errors import Code, EchoActError
from ..util.logging import get_logger
from . import wav

log = get_logger("audio.player")

#: How far ahead the feeder reads.  Large enough that a disk hiccup or a
#: scheduling gap cannot starve the device, small enough that a seek does
#: not have to discard much.
BUFFER_SECONDS = 1.5
#: Feeder read size.  Roughly 25 ms at 44.1 kHz.
READ_FRAMES = 1024
#: Device buffer.  PortAudio picks a default when this is 0, which on
#: Windows tends to be large; asking for a small one keeps the gap between
#: a pause and silence short enough not to be noticed.
BLOCKSIZE = 512


class PlayerState(StrEnum):
    """Section 5.2's playback states."""

    NOT_READY = "not_ready"
    PLAYING = "playing"
    PAUSED = "paused"
    WAITING = "waiting_for_segment"
    STOPPED = "stopped"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class Entry:
    """One segment's audio on the timeline, plus the silence after it.

    The gap belongs to this entry, not to the next one: F-27 attributes
    inter-segment silence to the preceding segment, so a position inside
    the gap still marks the sentence that just finished.
    """

    segment_index: int
    path: str
    frame_count: int
    gap_frames: int
    start_frame: int

    @property
    def total_frames(self) -> int:
        return self.frame_count + self.gap_frames

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.total_frames


@dataclass
class Timeline:
    """The ordered, growing sequence of ready segments.

    Appended to as generation proceeds (F-12) and never rewritten: a job's
    segments arrive in order and their durations are fixed once measured.
    """

    sample_rate: int
    entries: list[Entry] = field(default_factory=list)
    #: True once every segment of the job has been appended, so the player
    #: can tell "the end" from "not generated yet".
    complete: bool = False

    def append(self, segment_index: int, path: str, frame_count: int, gap_frames: int) -> Entry:
        entry = Entry(
            segment_index=segment_index,
            path=str(path),
            frame_count=frame_count,
            gap_frames=gap_frames,
            start_frame=self.total_frames,
        )
        self.entries.append(entry)
        return entry

    @property
    def total_frames(self) -> int:
        return self.entries[-1].end_frame if self.entries else 0

    @property
    def duration_ms(self) -> int:
        return wav.ms_for_frames(self.total_frames, self.sample_rate)

    def locate(self, frame: int) -> tuple[int, int] | None:
        """Which entry covers ``frame``, and how far into it.

        Returns ``None`` past the end of what exists, which is exactly the
        condition F-14 uses to refuse a seek into ungenerated audio.
        """
        if frame < 0 or frame >= self.total_frames:
            return None
        lo, hi = 0, len(self.entries) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            e = self.entries[mid]
            if frame < e.start_frame:
                hi = mid - 1
            elif frame >= e.end_frame:
                lo = mid + 1
            else:
                return mid, frame - e.start_frame
        return None

    def segment_at(self, frame: int) -> int | None:
        found = self.locate(frame)
        return self.entries[found[0]].segment_index if found else None


def append_segment(timeline: Timeline, segment: Segment) -> Entry | None:
    """Put one ready segment on the timeline, gap and all.

    The arithmetic lives here rather than in its callers because there are
    now two of them -- the window following its own job and F-89's external
    playback -- and the gap is the part that is easy to get subtly wrong: it
    is whatever the segment's own time range has left over after its audio,
    since F-82 attributes the silence to the preceding segment.
    """
    if not segment.audio_path or segment.time is None:
        return None
    rate = timeline.sample_rate
    span = wav.frames_for_ms(segment.time.end_ms, rate) - wav.frames_for_ms(
        segment.time.start_ms, rate
    )
    return timeline.append(
        segment.index,
        segment.audio_path,
        segment.frame_count,
        max(0, span - segment.frame_count),
    )


def timeline_from_segments(
    sample_rate: int, segments: Iterable[Segment], *, complete: bool = False
) -> Timeline:
    """A timeline for audio that already exists on disk.

    Segments arrive in order and any that is not ready ends the timeline:
    F-55 forbids presenting ungenerated audio as a finished result, and a
    timeline that skipped a hole would do exactly that.
    """
    timeline = Timeline(sample_rate=sample_rate)
    for segment in sorted(segments, key=lambda s: s.index):
        if append_segment(timeline, segment) is None:
            break
    else:
        timeline.complete = complete
    return timeline


class _Feeder:
    """Reads the timeline into a bounded byte ring, ahead of the callback.

    Kept apart from the player so the one thing that touches the disk is
    also the one thing that can be paused, flushed, and restarted on a seek
    without the audio callback ever blocking on I/O.
    """

    def __init__(self, timeline: Timeline, capacity_frames: int) -> None:
        self._timeline = timeline
        self._capacity = capacity_frames * 2  # int16 mono
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._room = threading.Event()
        self._room.set()
        self._read_frame = 0  # next timeline frame to be read from disk
        self._reader: wave.Wave_read | None = None
        self._reader_index = -1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="echoact-feeder", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._room.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None
        self._close_reader()

    # -- consumption ---------------------------------------------------

    def take(self, frames: int) -> bytes:
        """Up to ``frames`` frames.  Short returns mean starvation."""
        want = frames * 2
        with self._lock:
            out = bytes(self._buf[:want])
            del self._buf[: len(out)]
        if len(self._buf) < self._capacity:
            self._room.set()
        return out

    def seek(self, frame: int) -> None:
        with self._lock:
            self._buf.clear()
            self._read_frame = frame
        self._close_reader()
        self._room.set()

    @property
    def read_frame(self) -> int:
        return self._read_frame

    # -- production ----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            if len(self._buf) >= self._capacity:
                self._room.clear()
                self._room.wait(timeout=0.05)
                continue
            try:
                chunk = self._read_next()
            except EchoActError:
                raise
            except OSError as exc:
                log.warning("feeder read failed: %s", type(exc).__name__)
                chunk = b""
            if not chunk:
                # Either the end of what exists, or a file that is not
                # there yet.  Either way, wait rather than spin.
                self._stop.wait(0.02)
                continue
            with self._lock:
                self._buf += chunk

    def _read_next(self) -> bytes:
        found = self._timeline.locate(self._read_frame)
        if found is None:
            return b""
        index, offset = found
        entry = self._timeline.entries[index]

        if offset >= entry.frame_count:
            # Inside this segment's trailing silence.  Silence is generated
            # rather than stored: F-82 makes the gap the app's, and writing
            # it to disk would make the segment file no longer a bit-exact
            # part of the concatenation.
            remaining = entry.total_frames - offset
            n = min(remaining, READ_FRAMES)
            self._read_frame += n
            return b"\x00\x00" * n

        reader = self._ensure_reader(index, offset)
        n = min(entry.frame_count - offset, READ_FRAMES)
        raw = reader.readframes(n)
        got = len(raw) // 2
        if got == 0:
            return b""
        self._read_frame += got
        return raw

    def _ensure_reader(self, index: int, offset: int) -> wave.Wave_read:
        if self._reader is None or self._reader_index != index:
            self._close_reader()
            path = Path(self._timeline.entries[index].path)
            self._reader = wave.open(str(path), "rb")
            self._reader_index = index
            self._reader.setpos(offset)
        elif self._reader.tell() != offset:
            self._reader.setpos(offset)
        return self._reader

    def _close_reader(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:  # noqa: BLE001 - closing must never raise onward
                pass
        self._reader = None
        self._reader_index = -1


class Player:
    """Plays a :class:`Timeline` through PortAudio.

    Volume is applied to the outgoing samples only.  F-67 requires playback
    volume not to alter the generated WAV, and at 100% the samples are
    passed through untouched rather than multiplied by one, so no rounding
    happens on the ordinary path either.
    """

    def __init__(
        self,
        *,
        on_state: Callable[[PlayerState], None] | None = None,
        on_segment: Callable[[int], None] | None = None,
        on_device_lost: Callable[[str], None] | None = None,
    ) -> None:
        self._timeline: Timeline | None = None
        self._feeder: _Feeder | None = None
        self._stream = None  # sounddevice.RawOutputStream
        self._device: int | str | None = None
        self._sample_rate = 0

        self._state = PlayerState.NOT_READY
        self._played_frames = 0
        self._anchor_frames = 0
        self._anchor_time = 0.0
        self._current_segment: int | None = None
        self._lock = threading.Lock()

        self._volume = 1.0
        self._muted = False

        self._on_state = on_state
        self._on_segment = on_segment
        self._on_device_lost = on_device_lost

    # -- configuration --------------------------------------------------

    @property
    def state(self) -> PlayerState:
        return self._state

    @property
    def volume(self) -> float:
        return self._volume

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, value))

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, value: bool) -> None:
        self._muted = value

    def set_device(self, device: int | str | None) -> None:
        """Choose an output device (F-67).

        Changing it while playing reopens the stream at the current
        position rather than restarting, because the user chose a device,
        not a rewind.
        """
        if device == self._device:
            return
        self._device = device
        if self._stream is not None:
            was = self._state
            at = self._played_frames
            self._close_stream()
            self._open_stream()
            self.seek_frames(at)
            if was is PlayerState.PLAYING:
                self.play()

    @property
    def output_latency_ms(self) -> float:
        """N-12 asks for the device's latency as a separate, recorded
        figure rather than folded into the synchronisation budget."""
        if self._stream is None:
            return 0.0
        return float(self._stream.latency) * 1000.0

    # -- the timeline ---------------------------------------------------

    def load(self, timeline: Timeline) -> None:
        """Attach a job's timeline.  Does not start playback.

        Starting is always someone else's decision: F-83 lets the user turn
        auto-play off, and F-89 makes an external request's playback
        conditional on the owner's setting and on nobody else listening.
        Neither of those is a judgement this class could make.
        """
        self.stop()
        self._timeline = timeline
        self._sample_rate = timeline.sample_rate
        capacity = int(BUFFER_SECONDS * timeline.sample_rate)
        self._feeder = _Feeder(timeline, capacity)
        self._played_frames = 0
        self._current_segment = None
        self._set_state(PlayerState.NOT_READY if not timeline.entries else PlayerState.STOPPED)

    def timeline_grew(self) -> None:
        """Tell the player that segments were appended.

        F-13 keeps the user's paused state even when new audio arrives, so
        this never changes the state except to lift the waiting condition.
        """
        if self._state is PlayerState.WAITING:
            self._set_state(PlayerState.PLAYING)
        elif self._state is PlayerState.NOT_READY and self._timeline and self._timeline.entries:
            self._set_state(PlayerState.STOPPED)

    @property
    def playable_ms(self) -> int:
        return self._timeline.duration_ms if self._timeline else 0

    # -- transport -------------------------------------------------------

    def play(self) -> None:
        if self._timeline is None or not self._timeline.entries:
            return
        if self._stream is None:
            self._open_stream()
        assert self._feeder is not None
        self._feeder.start()
        with self._lock:
            self._anchor_frames = self._played_frames
            self._anchor_time = self._stream_time()
        self._set_state(PlayerState.PLAYING)

    def pause(self) -> None:
        """F-13: pausing does not halt generation, and the position stands."""
        if self._state in (PlayerState.PLAYING, PlayerState.WAITING):
            self._set_state(PlayerState.PAUSED)

    def stop(self) -> None:
        """F-13 and F-28: stop clears the position and the mark."""
        if self._feeder is not None:
            self._feeder.seek(0)
        with self._lock:
            self._played_frames = 0
            self._anchor_frames = 0
        self._current_segment = None
        if self._state is not PlayerState.NOT_READY:
            self._set_state(PlayerState.STOPPED)

    def close(self) -> None:
        self._close_stream()
        if self._feeder is not None:
            self._feeder.close()
            self._feeder = None
        self._timeline = None
        self._set_state(PlayerState.NOT_READY)

    def seek_ms(self, ms: int) -> bool:
        """F-14: seeking is allowed only inside generated segments."""
        if self._timeline is None:
            return False
        frame = wav.frames_for_ms(max(0, ms), self._timeline.sample_rate)
        return self.seek_frames(frame)

    def seek_frames(self, frame: int) -> bool:
        if self._timeline is None:
            return False
        if frame < 0 or frame >= max(1, self._timeline.total_frames):
            return False
        assert self._feeder is not None
        self._feeder.seek(frame)
        with self._lock:
            self._played_frames = frame
            self._anchor_frames = frame
            self._anchor_time = self._stream_time()
        seg = self._timeline.segment_at(frame)
        if seg is not None and seg != self._current_segment:
            self._current_segment = seg
            if self._on_segment:
                self._on_segment(seg)
        return True

    # -- the clock --------------------------------------------------------

    def position_ms(self) -> int:
        """Where the listener is, now.

        Extrapolated from the moment the last filled buffer will reach the
        device, so it is continuous between callbacks rather than stepping
        once per block.  Clamped to what has actually been handed to the
        device, so it can never report audio nobody has heard.
        """
        if self._timeline is None or self._sample_rate == 0:
            return 0
        with self._lock:
            anchor_f = self._anchor_frames
            anchor_t = self._anchor_time
            played = self._played_frames
        if self._state is not PlayerState.PLAYING:
            return wav.ms_for_frames(played, self._sample_rate)
        elapsed = max(0.0, self._stream_time() - anchor_t)
        frames = anchor_f + int(elapsed * self._sample_rate)
        return wav.ms_for_frames(min(frames, played), self._sample_rate)

    @property
    def current_segment(self) -> int | None:
        return self._current_segment

    def _stream_time(self) -> float:
        if self._stream is None:
            return 0.0
        try:
            return float(self._stream.time)
        except Exception:  # noqa: BLE001 - a closing stream must not raise here
            return 0.0

    # -- the device --------------------------------------------------------

    def _open_stream(self) -> None:
        import sounddevice as sd

        try:
            self._stream = sd.RawOutputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=BLOCKSIZE,
                device=self._device,
                callback=self._callback,
                finished_callback=self._finished,
            )
            self._stream.start()
        except Exception as exc:  # sounddevice raises several unrelated types
            self._stream = None
            raise EchoActError(
                Code.OUTPUT_DEVICE_UNAVAILABLE,
                "The audio output device could not be opened.",
                cause=exc,
            ) from exc

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise onward
            pass

    def _finished(self) -> None:
        """PortAudio aborted the stream, which on Windows is what a device
        being unplugged looks like.  F-67 pauses rather than switching to
        another speaker without the user."""
        if self._state in (PlayerState.PLAYING, PlayerState.WAITING):
            self._set_state(PlayerState.PAUSED)
            if self._on_device_lost:
                self._on_device_lost("the output device stopped")

    def _callback(self, outdata, frames: int, time_info, status) -> None:
        """Runs on PortAudio's thread.  No allocation beyond the block, no
        locks held across I/O, and no exceptions -- a raise here silences
        the stream."""
        try:
            self._fill(outdata, frames, time_info)
        except Exception as exc:  # noqa: BLE001 - never let the device die
            outdata[:] = b"\x00" * (frames * 2)
            log.error("audio callback failed: %s", type(exc).__name__)

    def _fill(self, outdata, frames: int, time_info) -> None:
        silence = b"\x00\x00" * frames
        with self._lock:
            self._anchor_frames = self._played_frames
            self._anchor_time = float(getattr(time_info, "outputBufferDacTime", 0.0)) or (
                self._stream_time()
            )

        if self._state is not PlayerState.PLAYING or self._feeder is None:
            outdata[:] = silence
            return

        raw = self._feeder.take(frames)
        got = len(raw) // 2
        if got:
            raw = self._apply_gain(raw)
        if got < frames:
            raw = raw + b"\x00\x00" * (frames - got)
        outdata[:] = raw

        if got == 0:
            self._starved()
            return

        with self._lock:
            self._played_frames += got
        self._note_position()

    def _apply_gain(self, raw: bytes) -> bytes:
        if self._muted:
            return b"\x00" * len(raw)
        if self._volume >= 0.999:
            return raw  # bit-exact passthrough at full volume
        block = np.frombuffer(raw, dtype="<i2").astype(np.float32) * self._volume
        return np.clip(block, -32768, 32767).astype("<i2").tobytes()

    def _starved(self) -> None:
        """Nothing to play.  Either the job ended or the next segment is
        not generated yet, and Section 5.2 distinguishes those."""
        tl = self._timeline
        if tl is None:
            return
        at_end = self._played_frames >= tl.total_frames
        if at_end and tl.complete:
            self._set_state(PlayerState.ENDED)
        elif at_end:
            self._set_state(PlayerState.WAITING)

    def _note_position(self) -> None:
        tl = self._timeline
        if tl is None:
            return
        seg = tl.segment_at(max(0, self._played_frames - 1))
        if seg is not None and seg != self._current_segment:
            self._current_segment = seg
            if self._on_segment:
                self._on_segment(seg)

    def _set_state(self, state: PlayerState) -> None:
        if state is self._state:
            return
        self._state = state
        if self._on_state:
            self._on_state(state)

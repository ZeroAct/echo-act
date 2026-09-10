"""The wire between the application process and the synthesis worker.

A.2 puts synthesis in a child process because N-03's enforced limits and
N-21's separate measurement both need an operating-system resource container,
and because N-22 gives five seconds to release resources -- which means the
worker must be killable mid-segment.  That last point shapes this protocol:

* The parent owns all durable state.  The worker holds a model and nothing
  else, so killing it at any instant loses no record and corrupts nothing.
* Every audio payload is written to a file the parent named *before* asking
  for it, and the worker reports the path only after the bytes are flushed
  and the file is closed.  A worker killed mid-write leaves a file the parent
  already knows to discard, rather than a half-message on a pipe.
* Requests carry a monotonically increasing ``seq``; replies echo it.  A late
  reply for a cancelled request is dropped by the parent rather than matched
  to whatever ran next.

Framing is one JSON object per line on stdin/stdout, UTF-8, no embedded
newlines.  The worker's stderr is free-form diagnostics; N-20 forbids body
text there, so the worker logs lengths and identifiers only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, TextIO


class MsgType(StrEnum):
    # parent -> worker
    LOAD = "load"
    SYNTHESIZE = "synthesize"
    UNLOAD = "unload"
    PING = "ping"
    SHUTDOWN = "shutdown"
    # worker -> parent
    READY = "ready"
    LOADED = "loaded"
    AUDIO = "audio"
    ERROR = "error"
    PONG = "pong"
    STATS = "stats"


@dataclass(slots=True)
class Load:
    """Prepare a model.  ``model_dir`` is resolved by the parent against the
    F-84 manifest; the worker never searches for weights and never downloads.

    ``allowed_providers`` is F-87's allow-list.  The worker refuses to run if
    a constructed session reports a provider outside it -- the installed
    runtime offers a remotely executing one, so N-01 depends on this check
    happening in the process that actually holds the session.
    """

    model_id: str
    model_dir: str
    intra_op_threads: int
    inter_op_threads: int
    allowed_providers: list[str]
    seq: int = 0
    type: str = MsgType.LOAD


@dataclass(slots=True)
class Synthesize:
    """Render one segment.

    ``text`` is already normalised and already split by F-81; the worker does
    no segmentation of its own.  It must also stop the engine from re-chunking
    internally, or the segment's duration stops being exactly known -- A.5
    measured the engine re-chunking Korean at 120 characters.

    ``out_path`` is chosen by the parent so that a killed worker cannot leave
    a file the parent does not know about.  Silence is *not* the worker's
    business: F-82 makes inter-segment silence the application's, and A.5
    found the engine's own silence parameter inert once we chunk first.
    """

    job_id: str
    segment_index: int
    text: str
    lang: str
    voice_id: str
    speed: float
    total_steps: int
    out_path: str
    seq: int = 0
    type: str = MsgType.SYNTHESIZE


@dataclass(slots=True)
class Unload:
    """F-19's explicit release, without ending the worker process."""

    seq: int = 0
    type: str = MsgType.UNLOAD


@dataclass(slots=True)
class Ping:
    seq: int = 0
    type: str = MsgType.PING


@dataclass(slots=True)
class Shutdown:
    seq: int = 0
    type: str = MsgType.SHUTDOWN


@dataclass(slots=True)
class Ready:
    """Sent once at start-up, before any request is read."""

    pid: int
    seq: int = 0
    type: str = MsgType.READY


@dataclass(slots=True)
class Loaded:
    model_id: str
    sample_rate: int
    voices: list[str]
    providers: list[str]
    load_seconds: float
    seq: int = 0
    type: str = MsgType.LOADED


@dataclass(slots=True)
class Audio:
    """One segment's audio, already on disk.

    ``frame_count`` is authoritative for the segment's duration: F-82 forbids
    resampling, so frames divided by the model's rate is the exact length, and
    the parent builds the segment time table from these numbers rather than
    from anything the engine reports about seconds.
    """

    job_id: str
    segment_index: int
    out_path: str
    frame_count: int
    sample_rate: int
    synth_seconds: float
    peak: float
    seq: int = 0
    type: str = MsgType.AUDIO


@dataclass(slots=True)
class Error:
    """A failure attributable to one request, or to the worker as a whole.

    ``code`` is an ``echoact.errors.Code`` value.  The worker chooses among a
    small set -- it cannot know the caller's context -- and the parent maps
    the rest.
    """

    code: str
    message: str
    fatal: bool = False
    job_id: str | None = None
    segment_index: int | None = None
    seq: int = 0
    type: str = MsgType.ERROR


@dataclass(slots=True)
class Pong:
    rss_bytes: int
    seq: int = 0
    type: str = MsgType.PONG


@dataclass(slots=True)
class Stats:
    """Unsolicited resource report, so N-21 can measure the generation job
    apart from total app usage without the parent having to poll on the
    request path."""

    rss_bytes: int
    cpu_percent: float
    seq: int = 0
    type: str = MsgType.STATS


_BY_TYPE: dict[str, type] = {
    MsgType.LOAD: Load,
    MsgType.SYNTHESIZE: Synthesize,
    MsgType.UNLOAD: Unload,
    MsgType.PING: Ping,
    MsgType.SHUTDOWN: Shutdown,
    MsgType.READY: Ready,
    MsgType.LOADED: Loaded,
    MsgType.AUDIO: Audio,
    MsgType.ERROR: Error,
    MsgType.PONG: Pong,
    MsgType.STATS: Stats,
}

Message = (
    Load | Synthesize | Unload | Ping | Shutdown | Ready | Loaded | Audio | Error | Pong | Stats
)


def encode(msg: Any) -> str:
    """One line of UTF-8 JSON, newline included."""
    return json.dumps(asdict(msg), ensure_ascii=False, separators=(",", ":")) + "\n"


def decode(line: str) -> Message:
    """Parse one line.  A line that is not a known message is a protocol
    error, never something to guess at."""
    data = json.loads(line)
    kind = data.get("type")
    cls = _BY_TYPE.get(kind)
    if cls is None:
        raise ValueError(f"unknown worker message type: {kind!r}")
    return cls(**data)


def write_message(stream: TextIO, msg: Any) -> None:
    stream.write(encode(msg))
    stream.flush()


@dataclass(slots=True)
class Counter:
    """Sequence numbers, so a reply to a cancelled request can be recognised
    and dropped rather than mistaken for the current one."""

    value: int = field(default=0)

    def next(self) -> int:
        self.value += 1
        return self.value

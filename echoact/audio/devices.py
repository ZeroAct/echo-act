"""Audio output devices: enumeration, selection, and loss.

F-67 needs a device list the user can choose from, and requires that a
device disappearing pauses playback rather than moving the sound to another
speaker.  F-68 adds that after the machine resumes from sleep, the device
list and the system resources are re-checked and playback is left paused.

PortAudio caches its device list at initialisation, so a device plugged in
or removed after start-up is invisible until the library is reinitialised.
That is why :func:`refresh` exists and why it is called on resume rather
than merely re-querying.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import Code, EchoActError
from ..util.logging import get_logger

log = get_logger("audio.devices")


@dataclass(frozen=True, slots=True)
class OutputDevice:
    index: int
    name: str
    host_api: str
    max_channels: int
    default_samplerate: float
    is_default: bool

    @property
    def key(self) -> str:
        """A stable-ish identifier to persist in settings.

        PortAudio indices are positional and shift when devices come and
        go, so a remembered index would silently select a different
        speaker.  The name plus host API survives a reboot; the index does
        not, and is resolved fresh each time.
        """
        return f"{self.host_api}::{self.name}"


def _sd():
    import sounddevice as sd

    return sd


def refresh() -> None:
    """Re-read the device list from the operating system.

    Needed after a resume (F-68) and after the user plugs something in;
    without it PortAudio keeps answering from the list it built at start-up.
    """
    sd = _sd()
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:  # noqa: BLE001 - a failed refresh is not fatal
        log.warning("device refresh failed: %s", type(exc).__name__)


def list_output_devices() -> list[OutputDevice]:
    sd = _sd()
    try:
        devices = sd.query_devices()
        apis = sd.query_hostapis()
        default_index = sd.default.device[1]
    except Exception as exc:  # noqa: BLE001
        raise EchoActError(
            Code.OUTPUT_DEVICE_UNAVAILABLE,
            "The audio system could not be queried.",
            cause=exc,
        ) from exc

    out: list[OutputDevice] = []
    for i, d in enumerate(devices):
        if int(d.get("max_output_channels", 0)) <= 0:
            continue
        api = apis[int(d.get("hostapi", 0))]["name"] if apis else ""
        out.append(
            OutputDevice(
                index=i,
                name=str(d.get("name", "")).strip(),
                host_api=str(api),
                max_channels=int(d["max_output_channels"]),
                default_samplerate=float(d.get("default_samplerate", 0.0)),
                is_default=(i == default_index),
            )
        )
    return out


def default_output_device() -> OutputDevice | None:
    for d in list_output_devices():
        if d.is_default:
            return d
    devices = list_output_devices()
    return devices[0] if devices else None


def resolve(key: str | None) -> OutputDevice | None:
    """Find the remembered device, or report that it is gone.

    Returning ``None`` for a key that no longer matches is deliberate: F-67
    forbids switching to another speaker without the user's confirmation,
    so the caller has to decide rather than being handed a substitute.
    """
    if not key:
        return default_output_device()
    for d in list_output_devices():
        if d.key == key:
            return d
    return None


def supports_rate(device_index: int, sample_rate: int) -> bool:
    """Whether the device can take our output format.

    F-82 forbids resampling, so a device that cannot accept the model's
    native rate is a problem to report rather than to paper over.
    """
    sd = _sd()
    try:
        sd.check_output_settings(
            device=device_index, channels=1, dtype="int16", samplerate=sample_rate
        )
        return True
    except Exception:  # noqa: BLE001 - any refusal is a refusal
        return False


class DeviceWatcher:
    """Polls for a change in the set of output devices.

    Polling rather than an OS notification because the two supported
    platforms signal this differently and neither reaches Python without a
    native extension; the cost is one cheap query every few seconds, which
    N-28 tolerates because it yields to nothing.
    """

    def __init__(
        self,
        on_change: Callable[[list[OutputDevice]], None],
        *,
        interval_s: float = 3.0,
    ) -> None:
        self._on_change = on_change
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: tuple[str, ...] = ()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._seen = self._snapshot()
        self._thread = threading.Thread(target=self._run, name="echoact-devices", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)

    def _snapshot(self) -> tuple[str, ...]:
        try:
            return tuple(d.key for d in list_output_devices())
        except EchoActError:
            return ()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            current = self._snapshot()
            if current == self._seen:
                continue
            # A change in the list is only trustworthy after a refresh;
            # PortAudio would otherwise keep reporting the stale set.
            refresh()
            current = self._snapshot()
            if current == self._seen:
                continue
            self._seen = current
            try:
                self._on_change(list_output_devices())
            except Exception as exc:  # noqa: BLE001
                log.warning("device change handler failed: %s", type(exc).__name__)

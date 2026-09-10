"""The model lifecycle: where weights live, whether they are sound, and how
they get there (F-09, F-63, F-64, F-65, resolved against F-84's manifest).

The app owns its own model cache under :func:`echoact.paths.model_cache_dir`
rather than letting the ``supertonic`` package download into
``~/.cache/supertonic3``.  F-73 has to size the model cache separately from
documents and audio, F-65 has to be able to delete it, and F-76 offers it as
its own deletion scope; none of that is possible for a directory a third-party
package owns and shares with whatever else on the machine imports it.  A
verified copy already sitting in the package's cache is still *read* rather
than re-downloaded -- 385 MB is not worth a second trip to satisfy tidiness --
but it is never written to, and deleting the app's cache says so.

Everything that decides "sound" goes through the manifest.  Nothing here
trusts a file because it exists, a size because a server reported it, or a
directory because the last run left it behind.

N-11's licence gate sits on the way to a *directory*, not only on the way to
a download.  A model that is already on disk -- borrowed from the package
cache above, or prepared before a release amended the restrictions -- is
never downloaded again, so a gate that only guarded :meth:`ModelRegistry.download`
would be one the two commonest cases walk straight past.

Four requirements shape the awkward parts:

* N-21 forbids reading a whole file into memory to hash it, and a
  verification pass over 385 MB has to be interruptible, so every read is a
  bounded chunk with a cancellation check between chunks.
* F-64 wants progress, failure, cancellation, and a retry that reuses sound
  data.  Downloads therefore stream into a ``.part`` file next to the target
  and are renamed into place only after the digest matches.  A cancelled or
  crashed download leaves a partial file that the next attempt resumes from
  and that verification ignores.  What a cancelled attempt *reports* rests on
  digests as well -- the ones the opening pass computed and the ones the
  transfer itself matched -- and never on a cheap size check, because a size
  check would call a same-size tampered file sound and hand F-63's screen a
  corrupt model described as ready.
* N-23 forbids a repeated request multiplying the work.  Preparation of one
  model is serialised for the whole process, so a second caller waits and
  then finds the files already in place instead of streaming a second copy
  through the same ``.part``.
* Section 5.3 forbids an external caller triggering an arbitrary large
  download.  A download asked for over REST or MCP is refused unless the
  owner pre-authorised that model in the GUI.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ..domain import Budget, RequestPath
from ..errors import Code, EchoActError
from ..paths import data_dir, model_cache_dir, redact
from ..policy import GIB, LOW_SPACE_WARNING_BYTES
from ..util.ids import now
from ..util.logging import get_logger
from .catalog import MANIFEST
from .manifest import LicenseTerms, Manifest, ModelEntry, ModelFile

log = get_logger("models.registry")

#: Read buffer for hashing and for streaming a download.  Not a policy limit
#: -- it is the granularity at which N-21's cancellation and F-64's progress
#: are observable, and the size at which neither is expensive.
_CHUNK_BYTES = 1 << 20

#: Network timeouts for a model download.  F-09 says preparation needs the
#: internet; nothing else in the product does, so these are not a policy
#: number shared with anything.
_CONNECT_TIMEOUT_S = 15.0
_READ_TIMEOUT_S = 60.0

#: How long a caller is told to wait before retrying a failed download.
_DOWNLOAD_RETRY_AFTER_S = 5.0

#: How long a caller queued behind another preparation of the same model
#: waits before looking at its cancel token again.  N-22 allows five seconds
#: to stop; a queued attempt gives up well inside that.
_LOCK_POLL_S = 0.25

_PART_SUFFIX = ".part"

#: Where the ``supertonic`` package would keep the same weights.  This is a
#: coupling to that package's layout, kept here rather than in the manifest
#: because it describes *another* program's cache, not the model.
_PACKAGE_CACHE_NAMES = {"supertonic-3": "supertonic3"}


# ======================================================================
# Cancellation
# ======================================================================


class CancelToken:
    """A flag one thread sets and another notices between chunks.

    Verification and download both run off the Qt main thread and both have
    to stop promptly: N-22 gives five seconds to release resources, and a
    256 MB hash does not fit in that if it can only be interrupted at file
    boundaries.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_s: float) -> bool:
        return self._event.wait(timeout_s)


def _cancelled(token: CancelToken | None) -> bool:
    return token is not None and token.cancelled


# ======================================================================
# Verification results (F-65)
# ======================================================================


class ModelState(StrEnum):
    """F-65's four answers to "are this model's files sound?".

    ``PARTIAL`` and ``CORRUPT`` are kept apart because they call for
    different actions and F-64 treats them differently on retry: a partial
    model needs the rest fetched, a corrupt one needs bad files replaced.
    """

    NOT_PRESENT = "not_present"
    PARTIAL = "partial"
    CORRUPT = "corrupt"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class FileStatus:
    """What verification learned about one manifest file."""

    relative_path: str
    expected_bytes: int
    actual_bytes: int
    present: bool
    size_ok: bool
    #: ``None`` when no digest was computed: the file was absent, the wrong
    #: size, or the pass was a shallow one.
    digest_ok: bool | None
    #: ``False`` when verification stopped before reaching this file.
    checked: bool = True

    @property
    def ok(self) -> bool:
        return self.checked and self.present and self.size_ok and self.digest_ok is not False

    @property
    def corrupt(self) -> bool:
        return self.present and (not self.size_ok or self.digest_ok is False)


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """The evidence behind a :class:`ModelState`.

    F-63 shows disk usage and F-64 decides what to re-fetch, so the report
    keeps per-file detail rather than collapsing to a verdict.
    """

    model_id: str
    root: Path
    files: tuple[FileStatus, ...]
    #: False for a presence-and-size pass, which cannot see a tampered file.
    deep: bool
    cancelled: bool = False

    @property
    def state(self) -> ModelState:
        if any(f.corrupt for f in self.files):
            return ModelState.CORRUPT
        if all(f.ok for f in self.files):
            return ModelState.READY
        if any(f.present for f in self.files):
            return ModelState.PARTIAL
        return ModelState.NOT_PRESENT

    @property
    def bytes_present(self) -> int:
        return sum(f.actual_bytes for f in self.files if f.present)

    @property
    def bytes_expected(self) -> int:
        return sum(f.expected_bytes for f in self.files)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(f.relative_path for f in self.files if not f.present)

    @property
    def damaged(self) -> tuple[str, ...]:
        return tuple(f.relative_path for f in self.files if f.corrupt)

    @property
    def unusable(self) -> tuple[str, ...]:
        """Everything a download would have to fetch to make this ready."""
        return tuple(f.relative_path for f in self.files if not f.ok)

    def status(self, relative_path: str) -> FileStatus | None:
        for f in self.files:
            if f.relative_path == relative_path:
                return f
        return None


# ======================================================================
# Download reporting (F-64)
# ======================================================================


class DownloadPhase(StrEnum):
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """One progress tick.  F-64 requires progress to be visible, and F-63
    requires the required storage to be known before the transfer starts, so
    the totals are populated from the manifest rather than from the server."""

    model_id: str
    phase: DownloadPhase
    relative_path: str
    file_index: int
    file_count: int
    file_bytes_done: int
    file_bytes_total: int
    bytes_done: int
    bytes_total: int

    @property
    def fraction(self) -> float:
        if self.bytes_total <= 0:
            return 1.0
        return min(1.0, self.bytes_done / self.bytes_total)


ProgressCallback = Callable[[DownloadProgress], None]


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    """What one preparation attempt did.

    Cancellation is an outcome, not an error: F-64 makes it a first-class
    control, and raising for it would force every caller to catch an
    exception for something the user asked for.  A failure that the user did
    not ask for still raises, so the two are never confused.
    """

    model_id: str
    completed: bool
    cancelled: bool
    bytes_downloaded: int
    reused: tuple[str, ...]
    fetched: tuple[str, ...]
    report: VerifyReport

    @property
    def state(self) -> ModelState:
        return self.report.state


# ======================================================================
# Where acceptance and authorisation are recorded (N-11, 5.3)
# ======================================================================


class ModelPreferences(Protocol):
    """The two owner decisions the lifecycle has to remember.

    N-11 puts licence acceptance in settings, and 5.3 puts the
    pre-authorisation of a model for external download there too.  Both are
    keyed by a fingerprint of the terms accepted, not by model id alone: if
    a later release ships different restrictions, the old acceptance does
    not silently cover them.
    """

    def license_accepted(self, model_id: str, fingerprint: str) -> bool: ...

    def record_license_acceptance(self, model_id: str, fingerprint: str) -> None: ...

    def download_authorised(self, model_id: str) -> bool: ...

    def set_download_authorised(self, model_id: str, allowed: bool) -> None: ...


class JsonModelPreferences:
    """A small JSON file beside the settings, used until
    ``echoact.config.settings`` exists to hold these two records.

    It fails closed: a file that cannot be read counts as nothing accepted
    and nothing authorised, because the failure mode of guessing the other
    way is preparing a model whose terms the user never saw.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        # Resolved per call, never at construction: ``paths.data_dir`` is
        # cached and tests redirect it after this object exists.
        return self._path if self._path is not None else data_dir() / "model_state.json"

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, data: dict[str, dict[str, object]]) -> None:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            code = Code.STORAGE_FULL if exc.errno == errno.ENOSPC else Code.INTERNAL
            raise EchoActError(
                code,
                "The model licence decision could not be saved.",
                detail={"path": redact(path)},
                cause=exc,
            ) from exc

    def license_accepted(self, model_id: str, fingerprint: str) -> bool:
        record = self._read().get("licenses_accepted", {})
        entry = record.get(model_id) if isinstance(record, dict) else None
        return isinstance(entry, dict) and entry.get("fingerprint") == fingerprint

    def record_license_acceptance(self, model_id: str, fingerprint: str) -> None:
        data = self._read()
        accepted = data.get("licenses_accepted")
        if not isinstance(accepted, dict):
            accepted = {}
        accepted[model_id] = {"fingerprint": fingerprint, "accepted_at": now()}
        data["licenses_accepted"] = accepted
        self._write(data)

    def download_authorised(self, model_id: str) -> bool:
        record = self._read().get("downloads_authorised", {})
        return isinstance(record, dict) and record.get(model_id) is True

    def set_download_authorised(self, model_id: str, allowed: bool) -> None:
        data = self._read()
        authorised = data.get("downloads_authorised")
        if not isinstance(authorised, dict):
            authorised = {}
        authorised[model_id] = bool(allowed)
        data["downloads_authorised"] = authorised
        self._write(data)


# ======================================================================
# Transport (F-09, F-64)
# ======================================================================


@dataclass(slots=True)
class RemoteBody:
    """A response being streamed.  ``resumed`` says whether the server
    honoured a Range request; if it did not, the caller must start over
    rather than append the whole file to a partial one."""

    chunks: Iterator[bytes]
    resumed: bool
    total_bytes: int | None


class Fetcher(Protocol):
    def open(self, url: str, *, offset: int = 0) -> AbstractContextManager[RemoteBody]: ...


class HttpxFetcher:
    """HTTPS streaming, with the range request F-64's retry depends on.

    ``huggingface_hub`` is installed (a dependency of ``supertonic``) and is
    used for the one thing it is authoritative about -- building the resolve
    URL for a repository at a pinned revision.  Its ``hf_hub_download`` is
    not used to transfer: it cannot be cancelled part-way through a 256 MB
    file and reports progress only through a tqdm bar, and F-64 requires
    both.  Streaming here also keeps the bytes going straight into the app's
    own cache instead of the hub's parallel one.
    """

    def __init__(self, *, connect_timeout_s: float = _CONNECT_TIMEOUT_S,
                 read_timeout_s: float = _READ_TIMEOUT_S) -> None:
        self._connect_timeout_s = connect_timeout_s
        self._read_timeout_s = read_timeout_s

    @contextmanager
    def open(self, url: str, *, offset: int = 0) -> Iterator[RemoteBody]:
        import httpx  # imported here so a GUI-only start-up never pays for it

        headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        timeout = httpx.Timeout(
            self._read_timeout_s, connect=self._connect_timeout_s, read=self._read_timeout_s
        )
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise EchoActError(
                            Code.MODEL_DOWNLOAD_FAILED,
                            f"The model server answered {response.status_code}.",
                            detail={"status": response.status_code},
                            retry_after_s=_DOWNLOAD_RETRY_AFTER_S,
                        )
                    length = response.headers.get("content-length")
                    yield RemoteBody(
                        chunks=response.iter_bytes(_CHUNK_BYTES),
                        resumed=offset > 0 and response.status_code == 206,
                        total_bytes=int(length) if length and length.isdigit() else None,
                    )
        except httpx.HTTPError as exc:
            # 5.3: a network outage says why preparation failed and stays
            # retryable; it never touches an already-prepared model.
            raise EchoActError(
                Code.MODEL_DOWNLOAD_FAILED,
                "The model could not be downloaded: the connection failed.",
                detail={"reason": type(exc).__name__},
                retry_after_s=_DOWNLOAD_RETRY_AFTER_S,
                cause=exc,
            ) from exc


def resolve_url(entry: ModelEntry, file: ModelFile) -> str:
    """The pinned-revision URL for one manifest file."""
    from huggingface_hub import hf_hub_url

    return hf_hub_url(entry.repo_id, file.relative_path, revision=entry.revision)


# ======================================================================
# Status projection (F-53, F-63)
# ======================================================================


@dataclass(frozen=True, slots=True)
class ModelStatus:
    """One row of F-63's model management screen, and F-53's model entry.

    ``runnable`` and ``unavailable_reason`` travel together because F-04
    forbids hiding a model that cannot run in the current budget and forbids
    substituting another for it: the only correct presentation is the model,
    shown as unavailable, with the reason.
    """

    model_id: str
    display_name: str
    state: ModelState
    sample_rate: int
    languages: tuple[str, ...]
    bytes_present: int
    bytes_total: int
    disk_bytes: int
    runnable: bool
    unavailable_reason: str | None
    license_name: str
    license_acceptance_required: bool
    license_accepted: bool
    download_authorised: bool
    #: True when the only sound copy is the ``supertonic`` package's cache.
    using_package_cache: bool
    minimum_memory_bytes: int
    minimum_cpu_percent: int


# ======================================================================
# The registry
# ======================================================================


class ModelRegistry:
    """F-63 to F-65's operations over F-84's manifest."""

    def __init__(
        self,
        manifest: Manifest = MANIFEST,
        *,
        root: Path | None = None,
        preferences: ModelPreferences | None = None,
        fetcher: Fetcher | None = None,
        package_cache_dirs: dict[str, Path] | None = None,
    ) -> None:
        self.manifest = manifest
        self._root = root
        self.preferences: ModelPreferences = preferences or JsonModelPreferences()
        self._fetcher = fetcher
        self._package_cache_dirs = package_cache_dirs

    # -- locations ------------------------------------------------------

    @property
    def root(self) -> Path:
        """Resolved per call: ``paths.model_cache_dir`` reads the
        environment through an ``lru_cache`` that a test may have cleared."""
        return self._root if self._root is not None else model_cache_dir()

    def entry(self, model_id: str) -> ModelEntry:
        return self.manifest.get(model_id)

    def model_dir(self, model_id: str) -> Path:
        """The app-owned directory for a model's files."""
        return self.root / self.entry(model_id).model_id

    def package_cache_dir(self, model_id: str) -> Path | None:
        """Where the ``supertonic`` package keeps the same weights, if the
        model is one it knows about.

        Read-only as far as this app is concerned.  F-73 reports it apart
        from the app's own cache, and F-65's delete does not touch it: it
        belongs to another program and may be shared with other tools.
        """
        if self._package_cache_dirs is not None:
            return self._package_cache_dirs.get(model_id)
        name = _PACKAGE_CACHE_NAMES.get(model_id)
        if name is None:
            return None
        override = os.environ.get("SUPERTONIC_CACHE_DIR")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".cache" / name

    # -- verification (F-65) --------------------------------------------

    def verify(
        self,
        model_id: str,
        *,
        deep: bool = True,
        cancel: CancelToken | None = None,
        progress: ProgressCallback | None = None,
        root: Path | None = None,
    ) -> VerifyReport:
        """Check every manifest file's size and, when ``deep``, its digest.

        Size first because a truncated download is the common case and
        hashing it would be a second wasted; the digest is what catches the
        tampering F-84 and A-23 care about.  Reading is chunked and checks
        the cancel token between chunks, per N-21.

        A pass cut short cannot report ``READY``: a file it never reached,
        or whose hash it abandoned part-way, counts as unchecked, and an
        unchecked file is not sound.
        """
        entry = self.entry(model_id)
        base = root if root is not None else self.model_dir(model_id)
        statuses: list[FileStatus] = []
        cancelled = False
        done = 0
        total = entry.total_bytes

        for index, file in enumerate(entry.files):
            if _cancelled(cancel):
                cancelled = True
                statuses.extend(_unchecked(f) for f in entry.files[index:])
                break
            status, read = self._verify_file(file, base, deep=deep, cancel=cancel)
            statuses.append(status)
            done += read
            if progress is not None:
                progress(
                    DownloadProgress(
                        model_id=model_id,
                        phase=DownloadPhase.CHECKING,
                        relative_path=file.relative_path,
                        file_index=index,
                        file_count=entry.file_count,
                        file_bytes_done=status.actual_bytes,
                        file_bytes_total=file.byte_size,
                        bytes_done=done,
                        bytes_total=total,
                    )
                )
            if _cancelled(cancel):
                cancelled = True
                statuses.extend(_unchecked(f) for f in entry.files[index + 1 :])
                break

        return VerifyReport(
            model_id=model_id,
            root=base,
            files=tuple(statuses),
            deep=deep,
            cancelled=cancelled,
        )

    def _verify_file(
        self, file: ModelFile, base: Path, *, deep: bool, cancel: CancelToken | None
    ) -> tuple[FileStatus, int]:
        path = file.path_under(base)
        try:
            size = path.stat().st_size
        except OSError:
            return (
                FileStatus(
                    relative_path=file.relative_path,
                    expected_bytes=file.byte_size,
                    actual_bytes=0,
                    present=False,
                    size_ok=False,
                    digest_ok=None,
                ),
                0,
            )
        size_ok = size == file.byte_size
        digest_ok: bool | None = None
        read = 0
        if size_ok and deep:
            digest, read = _sha256_file(path, cancel=cancel)
            digest_ok = digest == file.sha256 if digest is not None else None
        return (
            FileStatus(
                relative_path=file.relative_path,
                expected_bytes=file.byte_size,
                actual_bytes=size,
                present=True,
                size_ok=size_ok,
                digest_ok=digest_ok,
                checked=not (size_ok and deep and digest_ok is None),
            ),
            read,
        )

    def state(self, model_id: str, *, cancel: CancelToken | None = None) -> ModelState:
        """F-65's question, answered against digests."""
        return self.verify(model_id, deep=True, cancel=cancel).state

    def quick_state(self, model_id: str) -> ModelState:
        """Presence and size only.

        For a screen that refreshes: hashing 385 MB to redraw a list is
        wasteful, and a size check catches everything except deliberate
        tampering.  Anything that is about to *load* the model calls
        :meth:`prepared_dir`, which is deep.
        """
        return self.verify(model_id, deep=False).state

    def resolve_dir(
        self, model_id: str, *, deep: bool = True, cancel: CancelToken | None = None
    ) -> tuple[Path, bool]:
        """The directory holding a sound copy, and whether it is the
        package's cache rather than the app's own.

        Raises rather than returning a doubtful path: F-84 says a model whose
        files do not match the manifest is reported as corrupted, not used.

        The licence is checked here and not only in :meth:`download`, because
        this is the path a model actually reaches the engine by (N-11, F-80,
        A-23).  Neither case that matters passes through a download: the
        package cache is read where it lies, and a release that amends
        Attachment A leaves the files READY, so nothing would ever ask again.

        It is checked *after* the files are found, not before.  N-11 makes
        acceptance a condition of preparing a model, so for a model that is
        not on this machine the useful answer is that it is not prepared --
        which is the flow that presents the terms.  Reporting an unaccepted
        licence for a model that also is not there would be true and
        useless, and would send the owner to the wrong screen.
        """
        own = self.verify(model_id, deep=deep, cancel=cancel)
        if own.state is ModelState.READY:
            self._require_license(self.entry(model_id))
            return self.model_dir(model_id), False

        fallback = self.package_cache_dir(model_id)
        if fallback is not None and fallback.is_dir():
            other = self.verify(model_id, deep=deep, cancel=cancel, root=fallback)
            if other.state is ModelState.READY:
                # The case the gate exists for: borrowed weights reach the
                # engine without a download ever being asked for.
                self._require_license(self.entry(model_id))
                log.info("model %s served from the package cache %s", model_id, redact(fallback))
                return fallback, True

        if own.state is ModelState.CORRUPT:
            raise EchoActError(
                Code.MODEL_CORRUPT,
                detail={"model_id": model_id, "files": list(own.damaged)},
            )
        raise EchoActError(
            Code.MODEL_NOT_READY,
            detail={"model_id": model_id, "missing": len(own.unusable)},
        )

    def prepared_dir(
        self,
        model_id: str,
        *,
        budget: Budget | None = None,
        deep: bool = True,
        cancel: CancelToken | None = None,
    ) -> Path:
        """What the engine is given for ``Load.model_dir``.

        The budget check happens here as well as on the selection screen
        because F-04's promise is that a model that cannot run is refused
        with a reason, and the last chance to keep that promise is the
        moment before loading.
        """
        if budget is not None:
            self.ensure_can_run(model_id, budget)
        path, _ = self.resolve_dir(model_id, deep=deep, cancel=cancel)
        return path

    # -- budget (F-04, N-05) --------------------------------------------

    def can_run(self, model_id: str, budget: Budget) -> tuple[bool, str | None]:
        """F-04: a model that cannot run under the current budget is shown
        as unavailable *with the reason*, never hidden and never replaced.

        Returns ``(True, None)`` or ``(False, reason)``.  The reason is
        written for a person and repeated verbatim to an API caller, so it
        names both the requirement and the current value.
        """
        entry = self.entry(model_id)
        minimum = entry.minimum_budget
        reasons: list[str] = []
        if budget.memory_bytes < minimum.memory_bytes:
            reasons.append(
                f"{entry.display_name} is approved from a memory budget of "
                f"{_gib(minimum.memory_bytes)}; the current budget is "
                f"{_gib(budget.memory_bytes)}"
            )
        if budget.cpu_percent < minimum.cpu_percent:
            reasons.append(
                f"{entry.display_name} is approved from a CPU budget of "
                f"{minimum.cpu_percent}%; the current budget is {budget.cpu_percent}%"
            )
        if reasons:
            return False, "; ".join(reasons) + "."
        return True, None

    def ensure_can_run(self, model_id: str, budget: Budget) -> None:
        runnable, reason = self.can_run(model_id, budget)
        if not runnable:
            raise EchoActError(
                Code.MODEL_OVER_BUDGET,
                reason,
                detail={
                    "model_id": model_id,
                    "minimum_memory_bytes": self.entry(model_id).minimum_budget.memory_bytes,
                    "minimum_cpu_percent": self.entry(model_id).minimum_budget.cpu_percent,
                },
            )

    # -- licence (N-11, F-80) -------------------------------------------

    def license_terms(self, model_id: str) -> LicenseTerms:
        return self.entry(model_id).license

    def license_acceptance_required(self, model_id: str) -> bool:
        """True while N-11's terms still have to be shown and accepted.

        The fingerprint covers the restrictions themselves, so a release
        that ships changed terms asks again rather than inheriting consent
        the user gave to different text.
        """
        terms = self.license_terms(model_id)
        if not terms.acceptance_required:
            return False
        return not self.preferences.license_accepted(model_id, _fingerprint(terms))

    def accept_license(self, model_id: str) -> None:
        """Record that the owner accepted this model's terms (N-11)."""
        terms = self.license_terms(model_id)
        self.preferences.record_license_acceptance(model_id, _fingerprint(terms))
        log.info("licence accepted for model %s (%s)", model_id, terms.name)

    # -- external authorisation (5.3) -----------------------------------

    def download_authorised(self, model_id: str) -> bool:
        self.entry(model_id)
        return self.preferences.download_authorised(model_id)

    def set_download_authorised(self, model_id: str, allowed: bool) -> None:
        """The GUI's pre-authorisation switch from Section 5.3.

        Without it, a REST or MCP request can never start a download: the
        rule exists so that an integration cannot make the machine fetch
        hundreds of megabytes on its own initiative.
        """
        self.entry(model_id)
        self.preferences.set_download_authorised(model_id, allowed)
        log.info("download authorisation for %s set to %s", model_id, bool(allowed))

    # -- preparation (F-09, F-64) ---------------------------------------

    def download(
        self,
        model_id: str,
        progress_cb: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
        *,
        request_path: RequestPath = RequestPath.GUI,
        discard_partial: bool = False,
    ) -> DownloadOutcome:
        """Fetch whatever is missing or unsound, and nothing else (F-64).

        The work is decided by a verification pass, so a retry after a
        failure re-downloads exactly the files that are absent or corrupt and
        reuses the rest.  Within a file, a ``.part`` left by an earlier
        attempt is resumed from with a range request; if the server ignores
        the range, the transfer restarts rather than appending to it.

        Nothing is renamed into place until its digest matches the manifest,
        which is what makes F-64's "a cancelled download is never shown as
        ready" true by construction rather than by a flag someone has to
        remember to clear.

        Only one attempt per model runs at a time (N-23).  A second caller --
        the GUI and a REST request, two REST requests, a repair overtaking a
        download -- waits, and then its own verification pass finds the work
        already done and fetches nothing.
        """
        entry = self.entry(model_id)
        self._require_license(entry)
        self._require_authorisation(entry, request_path)

        with _preparing(self.model_dir(model_id), cancel) as held:
            if not held:
                # Cancelled while queued behind another attempt.  Nothing was
                # examined, so nothing is claimed: the pass below runs with an
                # already-cancelled token and reports every file unchecked.
                return self._cancelled_outcome(
                    entry, self.verify(model_id, cancel=cancel), progress_cb, 0, (), ()
                )
            return self._prepare(entry, progress_cb, cancel, discard_partial=discard_partial)

    def _prepare(
        self,
        entry: ModelEntry,
        progress_cb: ProgressCallback | None,
        cancel: CancelToken | None,
        *,
        discard_partial: bool,
    ) -> DownloadOutcome:
        """:meth:`download`'s body, with this model's preparation lock held."""
        model_id = entry.model_id
        report = self.verify(model_id, cancel=cancel, progress=progress_cb)
        if report.cancelled:
            return self._cancelled_outcome(entry, report, progress_cb, 0, (), ())

        todo = [f for f in entry.files if not _is_ok(report, f.relative_path)]
        reused = tuple(f.relative_path for f in entry.files if _is_ok(report, f.relative_path))
        if not todo:
            self._emit(
                progress_cb,
                entry,
                DownloadPhase.COMPLETE,
                None,
                0,
                report.bytes_expected,
                report.bytes_expected,
            )
            return DownloadOutcome(
                model_id=model_id,
                completed=True,
                cancelled=False,
                bytes_downloaded=0,
                reused=reused,
                fetched=(),
                report=report,
            )

        base = self.model_dir(model_id)
        remaining = sum(f.byte_size for f in todo)
        self._require_space(base, remaining)

        fetcher = self._fetcher if self._fetcher is not None else HttpxFetcher()
        already = report.bytes_expected - remaining
        downloaded = 0
        fetched: list[str] = []

        for index, file in enumerate(todo):
            if _cancelled(cancel):
                return self._cancelled_outcome(
                    entry,
                    self._interrupted_report(entry, report, fetched, None),
                    progress_cb,
                    downloaded,
                    reused,
                    tuple(fetched),
                )

            def tick(
                file_done: int,
                *,
                current: ModelFile = file,
                position: int = index,
                prior: int = already + downloaded,
            ) -> None:
                self._emit(
                    progress_cb,
                    entry,
                    DownloadPhase.DOWNLOADING,
                    current,
                    file_done,
                    prior + file_done,
                    report.bytes_expected,
                    file_index=position,
                    file_count=len(todo),
                )

            written, cancelled = self._download_file(
                entry,
                file,
                base,
                fetcher,
                cancel=cancel,
                discard_partial=discard_partial,
                on_chunk=tick,
            )
            downloaded += written
            if cancelled:
                return self._cancelled_outcome(
                    entry,
                    self._interrupted_report(entry, report, fetched, file.relative_path),
                    progress_cb,
                    downloaded,
                    reused,
                    tuple(fetched),
                )
            fetched.append(file.relative_path)

        final = self.verify(model_id, cancel=cancel)
        if final.cancelled:
            # Cancelled after the last byte landed: the files may well be
            # sound, but this pass did not establish that, and F-64 would
            # rather report the cancellation than a readiness it guessed.
            return self._cancelled_outcome(
                entry, final, progress_cb, downloaded, reused, tuple(fetched)
            )
        if final.state is not ModelState.READY:
            # Everything was digest-checked on the way in, so this means the
            # tree changed underneath us.  F-84: report corrupt, do not use.
            raise EchoActError(
                Code.MODEL_CORRUPT,
                detail={"model_id": model_id, "files": list(final.unusable)},
            )
        self._emit(
            progress_cb,
            entry,
            DownloadPhase.COMPLETE,
            None,
            0,
            final.bytes_expected,
            final.bytes_expected,
        )
        log.info(
            "model %s prepared: %d file(s) fetched, %d reused", model_id, len(fetched), len(reused)
        )
        return DownloadOutcome(
            model_id=model_id,
            completed=True,
            cancelled=False,
            bytes_downloaded=downloaded,
            reused=reused,
            fetched=tuple(fetched),
            report=final,
        )

    def repair(
        self,
        model_id: str,
        progress_cb: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
        *,
        request_path: RequestPath = RequestPath.GUI,
    ) -> DownloadOutcome:
        """F-65's repair: replace whatever does not match the manifest.

        Stronger than a retry in one respect -- it discards half-downloaded
        parts instead of resuming them.  A retry assumes the interruption was
        the problem; a repair is asked for because something is wrong, and a
        partial file is a candidate for what that is.
        """
        return self.download(
            model_id,
            progress_cb,
            cancel,
            request_path=request_path,
            discard_partial=True,
        )

    def _download_file(
        self,
        entry: ModelEntry,
        file: ModelFile,
        base: Path,
        fetcher: Fetcher,
        *,
        cancel: CancelToken | None,
        discard_partial: bool,
        on_chunk: Callable[[int], None],
    ) -> tuple[int, bool]:
        """Stream one file into place.  Returns (bytes written, cancelled)."""
        target = file.path_under(base)
        part = target.with_name(target.name + _PART_SUFFIX)
        with _writing(target.parent):
            target.parent.mkdir(parents=True, exist_ok=True)

        # A target that exists here failed verification, so it is not
        # something to keep: F-64 re-downloads corrupted data.
        _unlink(target)
        if discard_partial:
            _unlink(part)

        url = resolve_url(entry, file)
        for attempt in (1, 2):
            written, cancelled, digest = self._stream_to_part(
                url, part, file, fetcher, cancel=cancel, on_chunk=on_chunk
            )
            if cancelled:
                return written, True
            if digest == file.sha256:
                _replace(part, target)
                return written, False
            # Bad bytes.  The first explanation is a damaged transfer or a
            # stale resume, so throw the part away and fetch the whole file
            # once more before concluding anything about the upstream.
            _unlink(part)
            log.warning(
                "digest mismatch for %s (attempt %d)", redact(target), attempt
            )
        raise EchoActError(
            Code.MODEL_CORRUPT,
            "A downloaded model file does not match the manifest.",
            detail={"model_id": entry.model_id, "file": file.relative_path},
        )

    def _stream_to_part(
        self,
        url: str,
        part: Path,
        file: ModelFile,
        fetcher: Fetcher,
        *,
        cancel: CancelToken | None,
        on_chunk: Callable[[int], None],
    ) -> tuple[int, bool, str | None]:
        hasher = hashlib.sha256()
        offset = 0
        try:
            existing = part.stat().st_size
        except OSError:
            existing = 0
        if 0 < existing < file.byte_size:
            # Hash what is already there so the digest covers the whole file
            # without re-reading it after the transfer.
            digest_so_far, _ = _sha256_file(part, cancel=cancel, hasher=hasher)
            if digest_so_far is None:
                return 0, True, None
            offset = existing
        elif existing:
            _unlink(part)

        written = 0
        with fetcher.open(url, offset=offset) as body:
            if offset and not body.resumed:
                # The server sent the whole file; appending would corrupt it.
                hasher = hashlib.sha256()
                offset = 0
            mode = "ab" if offset else "wb"
            # Opening, every write, and the flush at the end of the block are
            # all inside ``_writing``: rule 3 lets no OSError out of here, and
            # 5.3 wants a disk that fills mid-transfer reported as a failed
            # save rather than as a crash.
            with _writing(part), part.open(mode) as sink:
                for chunk in body.chunks:
                    if _cancelled(cancel):
                        sink.flush()
                        return written, True, None
                    sink.write(chunk)
                    hasher.update(chunk)
                    written += len(chunk)
                    on_chunk(offset + written)
        return written, False, hasher.hexdigest()

    # -- deletion (F-65, F-76) ------------------------------------------

    def delete(self, model_id: str, *, in_use: bool = False) -> int:
        """Remove the app's copy of a model.  Returns the bytes freed.

        Only the model's own directory under the model cache is touched.
        F-65 is explicit that deleting a model does not delete retained
        documents or audio results, and those live under different roots in
        :mod:`echoact.paths`; nothing here can reach them.

        ``in_use`` is the caller's answer to "is a job holding this model?".
        F-65 requires that job to be cancelled and the model released first,
        and the registry cannot see jobs, so it refuses and says why rather
        than guessing.
        """
        entry = self.entry(model_id)
        if in_use:
            raise EchoActError(
                Code.DELETE_BLOCKED_IN_USE,
                "The model is loaded by a running job; cancel it first.",
                detail={"model_id": entry.model_id},
            )
        target = self.model_dir(model_id)
        root = self.root
        # Structural guard: the only thing this method may ever remove is a
        # named subdirectory of the model cache.
        if target == root or root not in target.parents:
            raise AssertionError(f"refusing to delete {target} outside {root}")
        # N-23: deleting the tree a download is streaming into would leave it
        # renaming files into a directory nobody expects to exist.  Refused
        # rather than queued, because F-65's delete is a GUI action and rule 7
        # forbids the main thread waiting minutes for a transfer to finish.
        lock = _prepare_lock(target)
        if not lock.acquire(blocking=False):
            raise EchoActError(
                Code.DELETE_BLOCKED_IN_USE,
                "The model is being prepared; cancel that first.",
                detail={"model_id": entry.model_id},
                retry_after_s=_DOWNLOAD_RETRY_AFTER_S,
            )
        try:
            freed = _tree_bytes(target)
            try:
                shutil.rmtree(target)
            except FileNotFoundError:
                return 0
            except OSError as exc:
                raise EchoActError(
                    Code.INTERNAL,
                    "The model files could not be deleted.",
                    detail={"model_id": entry.model_id, "path": redact(target)},
                    cause=exc,
                ) from exc
        finally:
            lock.release()
        log.info("deleted model %s (%d bytes)", entry.model_id, freed)
        return freed

    # -- reporting (F-53, F-63, F-73) -----------------------------------

    def disk_usage(self, model_id: str) -> int:
        """Bytes the app's copy occupies, partial downloads included."""
        return _tree_bytes(self.model_dir(model_id))

    def total_disk_usage(self) -> int:
        """F-73's "model cache" figure: the whole app-owned cache tree."""
        return _tree_bytes(self.root)

    def status(
        self, model_id: str, budget: Budget | None = None, *, deep: bool = False
    ) -> ModelStatus:
        """One model as F-63 and F-53 present it."""
        entry = self.entry(model_id)
        report = self.verify(model_id, deep=deep)
        using_package = False
        if report.state is not ModelState.READY:
            fallback = self.package_cache_dir(model_id)
            if fallback is not None and fallback.is_dir():
                other = self.verify(model_id, deep=deep, root=fallback)
                if other.state is ModelState.READY:
                    report = other
                    using_package = True
        runnable, reason = (True, None) if budget is None else self.can_run(model_id, budget)
        return ModelStatus(
            model_id=entry.model_id,
            display_name=entry.display_name,
            state=report.state,
            sample_rate=entry.sample_rate,
            languages=tuple(lang.value for lang in entry.languages),
            bytes_present=report.bytes_present,
            bytes_total=entry.total_bytes,
            disk_bytes=self.disk_usage(model_id),
            runnable=runnable,
            unavailable_reason=reason,
            license_name=entry.license.name,
            license_acceptance_required=entry.license.acceptance_required,
            license_accepted=not self.license_acceptance_required(model_id),
            download_authorised=self.preferences.download_authorised(model_id),
            using_package_cache=using_package,
            minimum_memory_bytes=entry.minimum_budget.memory_bytes,
            minimum_cpu_percent=entry.minimum_budget.cpu_percent,
        )

    def statuses(self, budget: Budget | None = None) -> tuple[ModelStatus, ...]:
        return tuple(self.status(m.model_id, budget) for m in self.manifest)

    # -- gates ----------------------------------------------------------

    def _require_license(self, entry: ModelEntry) -> None:
        if self.license_acceptance_required(entry.model_id):
            raise EchoActError(
                Code.MODEL_LICENSE_NOT_ACCEPTED,
                detail={"model_id": entry.model_id, "license": entry.license.name},
            )

    def _require_authorisation(self, entry: ModelEntry, request_path: RequestPath) -> None:
        """Section 5.3: only a model the owner pre-authorised in the GUI may
        be downloaded on an external request."""
        if request_path is RequestPath.GUI:
            return
        if not self.preferences.download_authorised(entry.model_id):
            raise EchoActError(
                Code.MODEL_DOWNLOAD_FORBIDDEN,
                detail={"model_id": entry.model_id, "request_path": request_path.value},
            )

    def _require_space(self, base: Path, needed: int) -> None:
        """F-09: preparation needs storage, and running the disk to zero is
        worse than refusing.  4.1's low-space figure is the headroom left."""
        probe = base
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            return
        if free < needed + LOW_SPACE_WARNING_BYTES:
            raise EchoActError(
                Code.STORAGE_FULL,
                "There is not enough free disk space to prepare this model.",
                detail={"needed_bytes": needed, "free_bytes": free},
            )

    # -- helpers --------------------------------------------------------

    def _emit(
        self,
        cb: ProgressCallback | None,
        entry: ModelEntry,
        phase: DownloadPhase,
        file: ModelFile | None,
        file_done: int,
        done: int,
        total: int,
        *,
        file_index: int = 0,
        file_count: int = 0,
    ) -> None:
        if cb is None:
            return
        cb(
            DownloadProgress(
                model_id=entry.model_id,
                phase=phase,
                relative_path=file.relative_path if file else "",
                file_index=file_index,
                file_count=file_count or entry.file_count,
                file_bytes_done=file_done,
                file_bytes_total=file.byte_size if file else 0,
                bytes_done=done,
                bytes_total=total,
            )
        )

    def _interrupted_report(
        self,
        entry: ModelEntry,
        baseline: VerifyReport,
        fetched: Iterable[str],
        in_flight: str | None,
    ) -> VerifyReport:
        """What a cancelled attempt is entitled to say about the model.

        F-64: a cancelled download is never shown as ready.  The only
        evidence of soundness that exists at this point is a digest -- the
        one the opening deep pass computed for a file the attempt did not
        have to fetch, or the one the transfer matched before renaming a file
        into place.  Verifying again here could only be a shallow pass, since
        N-22 allows five seconds to stop and 385 MB does not hash in that;
        and a shallow pass calls a same-size tampered file sound, so it would
        let a cancellation report READY for a model the deep pass had just
        called corrupt.  The deep verdicts are therefore carried forward
        instead of being thrown away.

        The file being written when the cancellation landed counts as
        unknown: its target was removed before the transfer began, and only
        a ``.part`` stands in its place.
        """
        done = set(fetched)
        statuses: list[FileStatus] = []
        for file in entry.files:
            prior = baseline.status(file.relative_path)
            if file.relative_path in done:
                statuses.append(
                    FileStatus(
                        relative_path=file.relative_path,
                        expected_bytes=file.byte_size,
                        actual_bytes=file.byte_size,
                        present=True,
                        size_ok=True,
                        digest_ok=True,
                    )
                )
            elif prior is None or file.relative_path == in_flight:
                statuses.append(_unchecked(file))
            else:
                statuses.append(prior)
        return VerifyReport(
            model_id=entry.model_id,
            root=self.model_dir(entry.model_id),
            files=tuple(statuses),
            deep=baseline.deep,
            cancelled=True,
        )

    def _cancelled_outcome(
        self,
        entry: ModelEntry,
        report: VerifyReport,
        cb: ProgressCallback | None,
        downloaded: int,
        reused: tuple[str, ...],
        fetched: tuple[str, ...],
    ) -> DownloadOutcome:
        self._emit(cb, entry, DownloadPhase.CANCELLED, None, 0, report.bytes_present,
                   entry.total_bytes)
        log.info("model %s preparation cancelled", entry.model_id)
        return DownloadOutcome(
            model_id=entry.model_id,
            completed=False,
            cancelled=True,
            bytes_downloaded=downloaded,
            reused=reused,
            fetched=fetched,
            report=report,
        )


# ======================================================================
# Module helpers
# ======================================================================


#: One preparation lock per model directory, shared by every registry in
#: this process.  N-23: two requests for one model must not become two
#: transfers.  The ``.part`` path is derived from the model id and the file
#: path alone, so two attempts open the same file -- one truncating while the
#: other appends -- and the digest each computes over its own stream says
#: nothing about the bytes that ended up on disk.  Keyed by directory rather
#: than by model id, so two registries over one cache serialise and two over
#: different caches do not.
_PREPARE_LOCKS: dict[str, threading.RLock] = {}
_PREPARE_LOCKS_GUARD = threading.Lock()


def _prepare_lock(directory: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(directory))
    with _PREPARE_LOCKS_GUARD:
        lock = _PREPARE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PREPARE_LOCKS[key] = lock
        return lock


@contextmanager
def _preparing(directory: Path, cancel: CancelToken | None) -> Iterator[bool]:
    """Hold one model's preparation lock (N-23).

    Yields ``True`` with the lock held, or ``False`` when the caller
    cancelled while waiting for the attempt in front of it -- the wait is
    polled rather than indefinite so that N-22's five seconds to stop hold
    for a queued attempt too.
    """
    lock = _prepare_lock(directory)
    while not lock.acquire(timeout=_LOCK_POLL_S):
        if _cancelled(cancel):
            yield False
            return
    try:
        yield True
    finally:
        lock.release()


@contextmanager
def _writing(path: Path) -> Iterator[None]:
    """Convert a failed write into this module's own error (rule 3, 5.3).

    :meth:`ModelRegistry._require_space` cannot stand in for this.  It asks
    once, before a transfer that runs for minutes: it does not see another
    process taking the last of the disk in the meantime, a cache directory
    that turns out to be read-only, or a scanner holding the ``.part`` open
    -- and on a volume whose free space cannot be read at all it declines to
    answer and lets the transfer start anyway.
    """
    try:
        yield
    except OSError as exc:
        out_of_room = exc.errno in (errno.ENOSPC, errno.EDQUOT)
        raise EchoActError(
            Code.STORAGE_FULL if out_of_room else Code.MODEL_DOWNLOAD_FAILED,
            "There is not enough free disk space to finish preparing this model."
            if out_of_room
            else "The model file could not be written to the cache.",
            detail={"path": redact(path)},
            retry_after_s=None if out_of_room else _DOWNLOAD_RETRY_AFTER_S,
            cause=exc,
        ) from exc


def _sha256_file(
    path: Path, *, cancel: CancelToken | None = None, hasher: hashlib._Hash | None = None
) -> tuple[str | None, int]:
    """Digest a file in bounded chunks.  ``None`` means cancelled.

    N-21 forbids holding the file in memory -- ``vector_estimator.onnx`` is
    256 MB and the generation budget starts at 2 GiB -- and the same loop is
    what makes a verification pass interruptible within N-22's five seconds.
    """
    digest = hasher if hasher is not None else hashlib.sha256()
    read = 0
    try:
        with path.open("rb") as handle:
            while True:
                if _cancelled(cancel):
                    return None, read
                chunk = handle.read(_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                read += len(chunk)
    except OSError as exc:
        raise EchoActError(
            Code.MODEL_CORRUPT,
            "A model file could not be read.",
            detail={"path": redact(path)},
            cause=exc,
        ) from exc
    return digest.hexdigest(), read


def _unchecked(file: ModelFile) -> FileStatus:
    return FileStatus(
        relative_path=file.relative_path,
        expected_bytes=file.byte_size,
        actual_bytes=0,
        present=False,
        size_ok=False,
        digest_ok=None,
        checked=False,
    )


def _is_ok(report: VerifyReport, relative_path: str) -> bool:
    status = report.status(relative_path)
    return status is not None and status.ok


def _tree_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in _walk(root):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _walk(root: Path) -> Iterable[Path]:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield Path(dirpath) / name


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _replace(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError as exc:
        raise EchoActError(
            Code.MODEL_DOWNLOAD_FAILED,
            "A downloaded model file could not be moved into place.",
            detail={"path": redact(target)},
            retry_after_s=_DOWNLOAD_RETRY_AFTER_S,
            cause=exc,
        ) from exc


def _fingerprint(terms: LicenseTerms) -> str:
    """Identify the exact terms accepted, so changed terms are re-asked."""
    digest = hashlib.sha256()
    digest.update(terms.name.encode("utf-8"))
    digest.update(terms.pass_through_obligation.encode("utf-8"))
    for restriction in terms.restrictions:
        digest.update(b"\x00")
        digest.update(restriction.encode("utf-8"))
    return digest.hexdigest()[:32]


def _gib(value: int) -> str:
    """4.1: memory is shown in GiB."""
    return f"{value / GIB:.1f} GiB"


__all__ = [
    "CancelToken",
    "DownloadOutcome",
    "DownloadPhase",
    "DownloadProgress",
    "Fetcher",
    "FileStatus",
    "HttpxFetcher",
    "JsonModelPreferences",
    "ModelPreferences",
    "ModelRegistry",
    "ModelState",
    "ModelStatus",
    "RemoteBody",
    "VerifyReport",
    "resolve_url",
]

"""The model lifecycle: F-63's screen, F-64's download, F-65's verify,
repair and delete, F-09 and Section 5.3's gates on who may start one.

Everything here runs against a three-file toy manifest and an injected
transport, so no test needs the network or the real 385 MB weights.  What is
being checked is the policy, not the bytes: which states are distinguished,
what a retry re-fetches, what a cancellation leaves behind, and who is
allowed to ask.  ``tests/test_manifest.py`` checks the shipped manifest
against the real files.
"""

from __future__ import annotations

import hashlib
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from echoact import paths
from echoact.domain import Budget, Gender, Language, RequestPath
from echoact.errors import Code, EchoActError
from echoact.models import registry as reg
from echoact.models.catalog import DEFAULT_MODEL_ID
from echoact.models.manifest import (
    LicenseTerms,
    Manifest,
    MinimumBudget,
    ModelEntry,
    ModelFile,
    VoiceEntry,
)
from echoact.models.registry import (
    CancelToken,
    DownloadPhase,
    DownloadProgress,
    ModelRegistry,
    ModelState,
    RemoteBody,
    resolve_url,
)
from echoact.policy import GIB

MODEL_ID = "test-model"

CONTENT: dict[str, bytes] = {
    "onnx/graph.bin": b"graph-bytes-" * 500,
    "onnx/table.json": b'{"table": true}',
    "voice_styles/V1.json": b"voice-one-" * 200,
}

GRAPH = "onnx/graph.bin"

ENOUGH = Budget(cpu_percent=20, memory_bytes=4 * GIB, intra_op_threads=2)


# ----------------------------------------------------------------------
# Fixtures and doubles
# ----------------------------------------------------------------------


def _entry(
    *,
    restrictions: tuple[str, ...] = ("(a) Do no harm.",),
    acceptance_required: bool = True,
) -> ModelEntry:
    return ModelEntry(
        model_id=MODEL_ID,
        display_name="Test Model",
        repo_id="org/test-model",
        revision="0" * 40,
        files=tuple(
            ModelFile(path, hashlib.sha256(body).hexdigest(), len(body))
            for path, body in CONTENT.items()
        ),
        license=LicenseTerms(
            name="Test RAIL",
            restrictions=restrictions,
            pass_through_obligation="paragraph 5",
            acceptance_required=acceptance_required,
        ),
        sample_rate=44_100,
        languages=(Language.KO, Language.EN),
        minimum_budget=MinimumBudget(memory_bytes=2 * GIB, cpu_percent=10),
        voices=(VoiceEntry("V1", Gender.FEMALE, "Female 1", "a voice, not yet reviewed"),),
    )


class FakeFetcher:
    """A transport that serves bytes from a dict and records every request.

    Ranges are honoured or refused on demand, because F-64's retry has to
    work either way and a server that ignores ``Range`` is the case that
    silently corrupts a resumed file if it is not noticed.
    """

    def __init__(
        self,
        blobs: dict[str, bytes],
        *,
        supports_range: bool = True,
        chunk: int = 64,
        error: EchoActError | None = None,
    ) -> None:
        self.blobs = blobs
        self.supports_range = supports_range
        self.chunk = chunk
        self.error = error
        self.requests: list[tuple[str, int]] = []

    @contextmanager
    def open(self, url: str, *, offset: int = 0) -> Iterator[RemoteBody]:
        self.requests.append((url, offset))
        if self.error is not None:
            raise self.error
        data = self.blobs[url]
        resumed = offset > 0 and self.supports_range
        body = data[offset:] if resumed else data
        yield RemoteBody(
            chunks=(body[i : i + self.chunk] for i in range(0, len(body), self.chunk)),
            resumed=resumed,
            total_bytes=len(body),
        )

    def urls(self) -> list[str]:
        return [url for url, _ in self.requests]


def _blobs(entry: ModelEntry, override: dict[str, bytes] | None = None) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for file in entry.files:
        body = CONTENT[file.relative_path]
        if override and file.relative_path in override:
            body = override[file.relative_path]
        out[resolve_url(entry, file)] = body
    return out


def _install(
    root: Path,
    *,
    skip: tuple[str, ...] = (),
    damage: tuple[str, ...] = (),
    truncate: tuple[str, ...] = (),
) -> None:
    """Put a copy of the model on disk, optionally imperfect."""
    for path, body in CONTENT.items():
        if path in skip:
            continue
        if path in damage:
            # Same length, different bytes: only the digest can see this.
            body = bytes((b + 1) % 256 for b in body)
        if path in truncate:
            body = body[: len(body) // 2]
        target = root.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every path the app uses points inside ``tmp_path`` for this test."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ECHOACT_MODEL_DIR", raising=False)
    monkeypatch.delenv("SUPERTONIC_CACHE_DIR", raising=False)
    paths.data_dir.cache_clear()
    yield tmp_path
    paths.data_dir.cache_clear()


@pytest.fixture
def entry() -> ModelEntry:
    return _entry()


@pytest.fixture
def fetcher(entry: ModelEntry) -> FakeFetcher:
    return FakeFetcher(_blobs(entry))


@pytest.fixture
def registry(data_root: Path, entry: ModelEntry, fetcher: FakeFetcher) -> ModelRegistry:
    return ModelRegistry(
        Manifest((entry,)),
        fetcher=fetcher,
        package_cache_dirs={},
    )


@pytest.fixture
def accepted(registry: ModelRegistry) -> ModelRegistry:
    """A registry whose owner has accepted the licence (N-11)."""
    registry.accept_license(MODEL_ID)
    return registry


# ----------------------------------------------------------------------
# Verification (F-65)
# ----------------------------------------------------------------------


def test_a_model_with_nothing_on_disk_is_not_present(registry: ModelRegistry) -> None:
    assert registry.state(MODEL_ID) is ModelState.NOT_PRESENT


def test_a_model_missing_one_file_is_partial_not_corrupt(registry: ModelRegistry) -> None:
    _install(registry.model_dir(MODEL_ID), skip=(GRAPH,))
    report = registry.verify(MODEL_ID)
    assert report.state is ModelState.PARTIAL
    assert report.missing == (GRAPH,)
    assert report.damaged == ()


def test_a_truncated_file_is_corrupt(registry: ModelRegistry) -> None:
    _install(registry.model_dir(MODEL_ID), truncate=(GRAPH,))
    assert registry.state(MODEL_ID) is ModelState.CORRUPT


def test_tampering_that_keeps_the_size_is_only_visible_to_the_digest(
    registry: ModelRegistry,
) -> None:
    # A-23 replaces a file with same-length bytes; F-84 requires that to be
    # reported as corrupted rather than used.
    _install(registry.model_dir(MODEL_ID), damage=(GRAPH,))
    assert registry.quick_state(MODEL_ID) is ModelState.READY
    assert registry.state(MODEL_ID) is ModelState.CORRUPT
    assert registry.verify(MODEL_ID).damaged == (GRAPH,)


def test_a_complete_matching_copy_is_ready(registry: ModelRegistry) -> None:
    _install(registry.model_dir(MODEL_ID))
    report = registry.verify(MODEL_ID)
    assert report.state is ModelState.READY
    assert report.bytes_present == report.bytes_expected
    assert report.unusable == ()


def test_a_cancelled_verification_never_reports_ready(registry: ModelRegistry) -> None:
    _install(registry.model_dir(MODEL_ID))
    token = CancelToken()
    token.cancel()
    report = registry.verify(MODEL_ID, cancel=token)
    assert report.cancelled is True
    assert report.state is not ModelState.READY


def test_verification_stops_part_way_when_cancelled_mid_pass(registry: ModelRegistry) -> None:
    _install(registry.model_dir(MODEL_ID))
    token = CancelToken()
    seen: list[str] = []

    def watch(event: DownloadProgress) -> None:
        seen.append(event.relative_path)
        token.cancel()

    report = registry.verify(MODEL_ID, cancel=token, progress=watch)
    assert len(seen) == 1
    assert report.cancelled is True
    assert report.state is not ModelState.READY


def test_digesting_a_file_never_loads_it_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # N-21: the largest shipped file is 256 MB and the generation budget
    # starts at 2 GiB, so verification reads in bounded chunks.
    blob = bytes(range(256)) * 4_000  # ~1 MB, several chunks at any size
    path = tmp_path / "big.bin"
    path.write_bytes(blob)
    monkeypatch.setattr(reg, "_CHUNK_BYTES", 4096)
    reads: list[int] = []
    real_open = Path.open

    class Spy:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            return self._handle.read(size)  # type: ignore[attr-defined]

        def __enter__(self) -> Spy:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()  # type: ignore[attr-defined]

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Spy(real_open(self, *a, **k)))
    digest, read = reg._sha256_file(path)
    assert digest == hashlib.sha256(blob).hexdigest()
    assert read == len(blob)
    assert len(reads) > 1
    assert max(reads) <= 4096


# ----------------------------------------------------------------------
# Download (F-09, F-64)
# ----------------------------------------------------------------------


def test_a_download_fetches_every_file_and_ends_ready(
    accepted: ModelRegistry, fetcher: FakeFetcher
) -> None:
    outcome = accepted.download(MODEL_ID)
    assert outcome.completed is True
    assert outcome.cancelled is False
    assert outcome.state is ModelState.READY
    assert len(outcome.fetched) == len(CONTENT)
    root = accepted.model_dir(MODEL_ID)
    for path, body in CONTENT.items():
        assert root.joinpath(*path.split("/")).read_bytes() == body


def test_progress_is_reported_against_the_manifest_total(accepted: ModelRegistry) -> None:
    events: list[DownloadProgress] = []
    accepted.download(MODEL_ID, events.append)
    phases = {e.phase for e in events}
    assert DownloadPhase.CHECKING in phases
    assert DownloadPhase.DOWNLOADING in phases
    assert events[-1].phase is DownloadPhase.COMPLETE
    assert events[-1].fraction == 1.0
    total = sum(len(b) for b in CONTENT.values())
    assert all(e.bytes_total == total for e in events)


def test_a_retry_reuses_sound_files_and_refetches_only_the_bad_one(
    accepted: ModelRegistry, fetcher: FakeFetcher, entry: ModelEntry
) -> None:
    # F-64: on retry, sound already-downloaded data is used and corrupted
    # data is re-downloaded.
    _install(accepted.model_dir(MODEL_ID), damage=(GRAPH,))
    outcome = accepted.download(MODEL_ID)
    assert outcome.state is ModelState.READY
    assert outcome.fetched == (GRAPH,)
    assert set(outcome.reused) == set(CONTENT) - {GRAPH}
    graph_url = resolve_url(entry, entry.file(GRAPH))
    assert fetcher.urls() == [graph_url]


def test_a_cancelled_download_is_never_shown_as_ready(
    accepted: ModelRegistry, fetcher: FakeFetcher
) -> None:
    token = CancelToken()

    def stop_early(event: DownloadProgress) -> None:
        if event.phase is DownloadPhase.DOWNLOADING:
            token.cancel()

    outcome = accepted.download(MODEL_ID, stop_early, token)
    assert outcome.cancelled is True
    assert outcome.completed is False
    assert outcome.state is not ModelState.READY
    assert accepted.quick_state(MODEL_ID) is not ModelState.READY
    root = accepted.model_dir(MODEL_ID)
    # Nothing is renamed into place until its digest matches, so the target
    # does not exist and the partial data sits beside it.
    assert not root.joinpath("onnx", "graph.bin").exists()
    assert root.joinpath("onnx", "graph.bin.part").exists()


def test_a_second_attempt_resumes_the_bytes_the_cancelled_one_kept(
    accepted: ModelRegistry, entry: ModelEntry
) -> None:
    token = CancelToken()

    def stop_early(event: DownloadProgress) -> None:
        if event.phase is DownloadPhase.DOWNLOADING:
            token.cancel()

    accepted.download(MODEL_ID, stop_early, token)
    part = accepted.model_dir(MODEL_ID) / "onnx" / "graph.bin.part"
    kept = part.stat().st_size
    assert 0 < kept < len(CONTENT[GRAPH])

    resumed = FakeFetcher(_blobs(entry))
    accepted._fetcher = resumed
    outcome = accepted.download(MODEL_ID)
    assert outcome.state is ModelState.READY
    graph_url = resolve_url(entry, entry.file(GRAPH))
    assert (graph_url, kept) in resumed.requests
    assert (accepted.model_dir(MODEL_ID) / "onnx" / "graph.bin").read_bytes() == CONTENT[GRAPH]


def test_a_server_that_ignores_the_range_restarts_instead_of_appending(
    accepted: ModelRegistry, entry: ModelEntry
) -> None:
    token = CancelToken()

    def stop_early(event: DownloadProgress) -> None:
        if event.phase is DownloadPhase.DOWNLOADING:
            token.cancel()

    accepted.download(MODEL_ID, stop_early, token)
    accepted._fetcher = FakeFetcher(_blobs(entry), supports_range=False)
    outcome = accepted.download(MODEL_ID)
    assert outcome.state is ModelState.READY
    assert (accepted.model_dir(MODEL_ID) / "onnx" / "graph.bin").read_bytes() == CONTENT[GRAPH]


def test_bytes_that_do_not_match_the_manifest_are_corrupt_not_installed(
    accepted: ModelRegistry, entry: ModelEntry
) -> None:
    wrong = bytes((b + 7) % 256 for b in CONTENT[GRAPH])
    liar = FakeFetcher(_blobs(entry, {GRAPH: wrong}))
    accepted._fetcher = liar
    with pytest.raises(EchoActError) as caught:
        accepted.download(MODEL_ID)
    assert caught.value.code is Code.MODEL_CORRUPT
    root = accepted.model_dir(MODEL_ID)
    assert not root.joinpath("onnx", "graph.bin").exists()
    assert not root.joinpath("onnx", "graph.bin.part").exists()
    # Fetched once, then once more from scratch before giving up: a damaged
    # transfer is the likelier explanation than a changed upstream.
    graph_url = resolve_url(entry, entry.file(GRAPH))
    assert liar.urls().count(graph_url) == 2


def test_a_transport_failure_is_retryable_and_says_so(
    accepted: ModelRegistry, entry: ModelEntry
) -> None:
    # 5.3: a network outage reports why preparation failed; N-23 wants the
    # hint only on something a retry could fix.
    accepted._fetcher = FakeFetcher(
        _blobs(entry),
        error=EchoActError(Code.MODEL_DOWNLOAD_FAILED, retry_after_s=5.0),
    )
    with pytest.raises(EchoActError) as caught:
        accepted.download(MODEL_ID)
    assert caught.value.code is Code.MODEL_DOWNLOAD_FAILED
    assert caught.value.retryable is True
    assert caught.value.retry_after_s == 5.0
    assert accepted.state(MODEL_ID) is not ModelState.READY


def test_preparation_is_refused_before_there_is_room_for_it(
    accepted: ModelRegistry, fetcher: FakeFetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        reg.shutil,
        "disk_usage",
        lambda _p: types.SimpleNamespace(total=1 << 40, used=1 << 40, free=1024),
    )
    with pytest.raises(EchoActError) as caught:
        accepted.download(MODEL_ID)
    assert caught.value.code is Code.STORAGE_FULL
    assert fetcher.requests == []


# ----------------------------------------------------------------------
# Repair and delete (F-65, F-76)
# ----------------------------------------------------------------------


def test_repair_replaces_the_damaged_file_and_discards_a_partial(
    accepted: ModelRegistry, entry: ModelEntry
) -> None:
    root = accepted.model_dir(MODEL_ID)
    _install(root, damage=(GRAPH,))
    stale = root / "onnx" / "graph.bin.part"
    stale.write_bytes(b"nonsense")
    outcome = accepted.repair(MODEL_ID)
    assert outcome.state is ModelState.READY
    assert not stale.exists()
    assert (root / "onnx" / "graph.bin").read_bytes() == CONTENT[GRAPH]


def test_deleting_a_model_leaves_documents_and_audio_alone(
    accepted: ModelRegistry, data_root: Path
) -> None:
    # F-65 in as many words: deleting a model does not delete retained
    # documents or audio results.
    paths.ensure_tree()
    keep_audio = paths.audio_dir() / "job.wav"
    keep_audio.write_bytes(b"RIFF")
    keep_db = paths.db_path()
    keep_db.write_bytes(b"SQLite")
    _install(accepted.model_dir(MODEL_ID))

    freed = accepted.delete(MODEL_ID)

    assert freed == sum(len(b) for b in CONTENT.values())
    assert not accepted.model_dir(MODEL_ID).exists()
    assert keep_audio.read_bytes() == b"RIFF"
    assert keep_db.read_bytes() == b"SQLite"
    assert accepted.state(MODEL_ID) is ModelState.NOT_PRESENT


def test_deleting_a_model_a_job_is_holding_is_refused(accepted: ModelRegistry) -> None:
    _install(accepted.model_dir(MODEL_ID))
    with pytest.raises(EchoActError) as caught:
        accepted.delete(MODEL_ID, in_use=True)
    assert caught.value.code is Code.DELETE_BLOCKED_IN_USE
    assert accepted.state(MODEL_ID) is ModelState.READY


def test_deleting_what_is_not_there_is_not_an_error(accepted: ModelRegistry) -> None:
    assert accepted.delete(MODEL_ID) == 0


def test_the_model_cache_is_sized_on_its_own(accepted: ModelRegistry) -> None:
    # F-73 shows the model cache apart from documents, audio, and logs.
    assert accepted.total_disk_usage() == 0
    _install(accepted.model_dir(MODEL_ID))
    assert accepted.disk_usage(MODEL_ID) == sum(len(b) for b in CONTENT.values())
    assert accepted.total_disk_usage() == accepted.disk_usage(MODEL_ID)


# ----------------------------------------------------------------------
# The licence gate (N-11, F-80)
# ----------------------------------------------------------------------


def test_a_model_is_not_prepared_before_its_terms_are_accepted(
    registry: ModelRegistry, fetcher: FakeFetcher
) -> None:
    assert registry.license_acceptance_required(MODEL_ID) is True
    with pytest.raises(EchoActError) as caught:
        registry.download(MODEL_ID)
    assert caught.value.code is Code.MODEL_LICENSE_NOT_ACCEPTED
    assert fetcher.requests == []

    registry.accept_license(MODEL_ID)
    assert registry.license_acceptance_required(MODEL_ID) is False
    assert registry.download(MODEL_ID).state is ModelState.READY


def test_acceptance_survives_a_restart(
    data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    first = ModelRegistry(Manifest((entry,)), fetcher=fetcher, package_cache_dirs={})
    first.accept_license(MODEL_ID)
    second = ModelRegistry(Manifest((entry,)), fetcher=fetcher, package_cache_dirs={})
    assert second.license_acceptance_required(MODEL_ID) is False


def test_changed_restrictions_are_asked_about_again(data_root: Path, entry: ModelEntry) -> None:
    # N-11 is about the terms, not the model id: consent to one text is not
    # consent to a stricter one shipped later.
    ModelRegistry(Manifest((entry,)), package_cache_dirs={}).accept_license(MODEL_ID)
    amended = _entry(restrictions=("(a) Do no harm.", "(b) And nothing else."))
    later = ModelRegistry(Manifest((amended,)), package_cache_dirs={})
    assert later.license_acceptance_required(MODEL_ID) is True


def test_a_model_without_acceptance_terms_needs_none(data_root: Path) -> None:
    free = _entry(acceptance_required=False)
    registry = ModelRegistry(Manifest((free,)), package_cache_dirs={})
    assert registry.license_acceptance_required(MODEL_ID) is False


# ----------------------------------------------------------------------
# Who may start a download (Section 5.3)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("path", [RequestPath.REST, RequestPath.MCP])
def test_an_integration_cannot_start_an_unauthorised_download(
    accepted: ModelRegistry, fetcher: FakeFetcher, path: RequestPath
) -> None:
    with pytest.raises(EchoActError) as caught:
        accepted.download(MODEL_ID, request_path=path)
    assert caught.value.code is Code.MODEL_DOWNLOAD_FORBIDDEN
    assert caught.value.retryable is False
    assert fetcher.requests == []


def test_the_owner_can_pre_authorise_one_model_for_integrations(
    accepted: ModelRegistry,
) -> None:
    accepted.set_download_authorised(MODEL_ID, True)
    assert accepted.download_authorised(MODEL_ID) is True
    assert accepted.download(MODEL_ID, request_path=RequestPath.REST).state is ModelState.READY


def test_authorisation_can_be_withdrawn(accepted: ModelRegistry) -> None:
    accepted.set_download_authorised(MODEL_ID, True)
    accepted.set_download_authorised(MODEL_ID, False)
    with pytest.raises(EchoActError) as caught:
        accepted.download(MODEL_ID, request_path=RequestPath.MCP)
    assert caught.value.code is Code.MODEL_DOWNLOAD_FORBIDDEN


def test_the_gui_needs_no_pre_authorisation(accepted: ModelRegistry) -> None:
    assert accepted.download_authorised(MODEL_ID) is False
    assert accepted.download(MODEL_ID, request_path=RequestPath.GUI).state is ModelState.READY


# ----------------------------------------------------------------------
# Budget (F-04, N-05)
# ----------------------------------------------------------------------


def test_a_model_that_cannot_run_says_why_rather_than_disappearing(
    registry: ModelRegistry,
) -> None:
    tight = Budget(cpu_percent=20, memory_bytes=1 * GIB, intra_op_threads=1)
    runnable, reason = registry.can_run(MODEL_ID, tight)
    assert runnable is False
    assert reason is not None
    assert "2.0 GiB" in reason and "1.0 GiB" in reason
    # F-04 forbids hiding it: the status row still describes the model.
    status = registry.status(MODEL_ID, tight)
    assert status.model_id == MODEL_ID
    assert status.runnable is False
    assert status.unavailable_reason == reason


def test_a_cpu_budget_below_the_approved_floor_is_named_separately(
    registry: ModelRegistry,
) -> None:
    starved = Budget(cpu_percent=5, memory_bytes=4 * GIB, intra_op_threads=1)
    runnable, reason = registry.can_run(MODEL_ID, starved)
    assert runnable is False
    assert reason is not None and "CPU" in reason and "5%" in reason


def test_a_sufficient_budget_runs_without_a_reason(registry: ModelRegistry) -> None:
    assert registry.can_run(MODEL_ID, ENOUGH) == (True, None)
    registry.ensure_can_run(MODEL_ID, ENOUGH)


def test_loading_is_refused_over_budget_even_when_the_files_are_ready(
    registry: ModelRegistry,
) -> None:
    _install(registry.model_dir(MODEL_ID))
    tight = Budget(cpu_percent=20, memory_bytes=1 * GIB, intra_op_threads=1)
    with pytest.raises(EchoActError) as caught:
        registry.prepared_dir(MODEL_ID, budget=tight)
    assert caught.value.code is Code.MODEL_OVER_BUDGET


# ----------------------------------------------------------------------
# Resolving a directory for the engine
# ----------------------------------------------------------------------


def test_the_engine_is_given_the_app_owned_directory(registry: ModelRegistry) -> None:
    _install(registry.model_dir(MODEL_ID))
    assert registry.prepared_dir(MODEL_ID, budget=ENOUGH) == registry.model_dir(MODEL_ID)
    assert registry.model_dir(MODEL_ID).parent == paths.model_cache_dir()


def test_a_verified_copy_in_the_package_cache_is_read_rather_than_redownloaded(
    data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    package = data_root / "package-cache"
    _install(package)
    registry = ModelRegistry(
        Manifest((entry,)), fetcher=fetcher, package_cache_dirs={MODEL_ID: package}
    )
    path, borrowed = registry.resolve_dir(MODEL_ID)
    assert (path, borrowed) == (package, True)
    assert registry.status(MODEL_ID, deep=True).using_package_cache is True
    assert fetcher.requests == []
    # Deleting the app's cache never reaches into another program's.
    registry.delete(MODEL_ID)
    assert (package / "onnx" / "graph.bin").exists()


def test_a_damaged_package_cache_is_not_borrowed(
    data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    package = data_root / "package-cache"
    _install(package, damage=(GRAPH,))
    registry = ModelRegistry(
        Manifest((entry,)), fetcher=fetcher, package_cache_dirs={MODEL_ID: package}
    )
    with pytest.raises(EchoActError) as caught:
        registry.resolve_dir(MODEL_ID)
    assert caught.value.code is Code.MODEL_NOT_READY


def test_a_corrupt_local_copy_is_reported_corrupt_rather_than_missing(
    registry: ModelRegistry,
) -> None:
    _install(registry.model_dir(MODEL_ID), damage=(GRAPH,))
    with pytest.raises(EchoActError) as caught:
        registry.resolve_dir(MODEL_ID)
    assert caught.value.code is Code.MODEL_CORRUPT


def test_an_unprepared_model_is_not_ready_rather_than_a_fault(registry: ModelRegistry) -> None:
    with pytest.raises(EchoActError) as caught:
        registry.resolve_dir(MODEL_ID)
    assert caught.value.code is Code.MODEL_NOT_READY


# ----------------------------------------------------------------------
# Unknown models
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.state("nope"),
        lambda r: r.verify("nope"),
        lambda r: r.model_dir("nope"),
        lambda r: r.download("nope"),
        lambda r: r.delete("nope"),
        lambda r: r.status("nope"),
        lambda r: r.can_run("nope", ENOUGH),
        lambda r: r.license_acceptance_required("nope"),
        lambda r: r.set_download_authorised("nope", True),
    ],
)
def test_an_unknown_model_is_refused_by_every_entry_point(
    registry: ModelRegistry, call
) -> None:
    with pytest.raises(EchoActError) as caught:
        call(registry)
    assert caught.value.code is Code.MODEL_UNKNOWN


# ----------------------------------------------------------------------
# Status projection (F-53, F-63)
# ----------------------------------------------------------------------


def test_status_reports_what_the_management_screen_needs(accepted: ModelRegistry) -> None:
    accepted.download(MODEL_ID)
    status = accepted.status(MODEL_ID, ENOUGH)
    assert status.state is ModelState.READY
    assert status.display_name == "Test Model"
    assert status.sample_rate == 44_100
    assert status.languages == ("ko", "en")
    assert status.bytes_total == sum(len(b) for b in CONTENT.values())
    assert status.disk_bytes >= status.bytes_total
    assert status.license_name == "Test RAIL"
    assert status.license_acceptance_required is True
    assert status.license_accepted is True
    assert status.download_authorised is False
    assert status.minimum_memory_bytes == 2 * GIB
    assert status.runnable is True


def test_the_required_space_is_known_before_anything_is_downloaded(
    registry: ModelRegistry,
) -> None:
    # F-63 reports the required storage before the transfer starts, which is
    # only possible because the manifest carries the sizes.
    status = registry.status(MODEL_ID)
    assert status.state is ModelState.NOT_PRESENT
    assert status.bytes_present == 0
    assert status.bytes_total == sum(len(b) for b in CONTENT.values())


# ----------------------------------------------------------------------
# Against the real model (needs the weights)
# ----------------------------------------------------------------------


@pytest.mark.engine
@pytest.mark.skipif(
    not (Path.home() / ".cache" / "supertonic3").is_dir(),
    reason="Supertonic 3 weights are not present on this machine",
)
def test_the_shipped_manifest_verifies_the_real_weights_where_they_already_are(
    data_root: Path,
) -> None:
    # The app's own cache is empty in this tmp tree, so this also exercises
    # the fallback: a verified copy in the package's cache is read rather
    # than downloaded again.
    registry = ModelRegistry()
    assert registry.state(DEFAULT_MODEL_ID) is ModelState.NOT_PRESENT
    path, borrowed = registry.resolve_dir(DEFAULT_MODEL_ID)
    assert borrowed is True
    assert path == Path.home() / ".cache" / "supertonic3"
    assert registry.can_run(DEFAULT_MODEL_ID, ENOUGH) == (True, None)

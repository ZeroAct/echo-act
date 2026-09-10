"""The model management screen: F-63's report, F-64's download control,
F-65's verify/repair/delete, F-09 and Section 5.3's gates, and F-80's
licence review.

Everything here runs against a three-file toy manifest and an injected
transport, so nothing needs the network or the real 385 MB weights.  What is
being checked is what the screen *says* and *does not* say: F-04 forbids
hiding a model that cannot run, F-64 forbids ever showing a cancelled
download as ready, F-65 forbids deleting a model in use without a
confirmation and a release, and rule 7 forbids doing any of the byte work on
the GUI thread.  The last one is asserted rather than assumed, because a
threading mistake here works most of the time.

No test shows a window: the offscreen Qt platform has no font database on
Windows, so widgets are polished with ``grab()`` when a layout is needed and
otherwise only queried.  The two modal dialogs are replaced per test --
a ``QMessageBox`` spins an event loop of its own, and nothing outside it can
press its buttons.
"""

from __future__ import annotations

import ast
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from echoact import paths
from echoact.domain import Budget, Gender, Language
from echoact.errors import Code, EchoActError
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
    DownloadProgress,
    ModelRegistry,
    ModelState,
    ProgressCallback,
    RemoteBody,
    VerifyReport,
    resolve_url,
)
from echoact.policy import GIB, TEMPO_MAX, TEMPO_MIN
from echoact.ui import i18n, models_view, theme
from echoact.ui.models_view import ModelCard, ModelsView

MODEL_ID = "test-model"
GRAPH = "onnx/graph.bin"

CONTENT: dict[str, bytes] = {
    GRAPH: b"graph-bytes-" * 2000,
    "onnx/table.json": b'{"table": true}',
    "voice_styles/V1.json": b"voice-one-" * 200,
}

RESTRICTIONS = (
    "(a) In any way that violates any applicable law;",
    "(b) To impersonate others without their consent;",
)

#: Comfortably above the toy model's minimum of 2 GiB and 10%.
ENOUGH = Budget(cpu_percent=20, memory_bytes=4 * GIB, intra_op_threads=2)
#: Below it on both axes, so F-04's reason has two clauses.
TOO_SMALL = Budget(cpu_percent=5, memory_bytes=1 * GIB, intra_op_threads=1)


# ----------------------------------------------------------------------
# Doubles
# ----------------------------------------------------------------------


def _entry(*, acceptance_required: bool = True) -> ModelEntry:
    return ModelEntry(
        model_id=MODEL_ID,
        display_name="Test Model",
        repo_id="org/test-model",
        revision="724fb5abbf5502583fb520898d45929e62f02c0b",
        files=tuple(
            ModelFile(path, hashlib.sha256(body).hexdigest(), len(body))
            for path, body in CONTENT.items()
        ),
        license=LicenseTerms(
            name="Test RAIL",
            restrictions=RESTRICTIONS,
            pass_through_obligation="Paragraph 5 binds your users too.",
            acceptance_required=acceptance_required,
            notes=("The output is yours; the model is not.",),
        ),
        sample_rate=44_100,
        languages=(Language.KO, Language.EN),
        minimum_budget=MinimumBudget(memory_bytes=2 * GIB, cpu_percent=10),
        voices=(
            VoiceEntry("F1", Gender.FEMALE, "Female 1", "a voice, not yet reviewed"),
            VoiceEntry("M1", Gender.MALE, "Male 1", "another voice, not yet reviewed"),
        ),
    )


class FakeFetcher:
    """Serves bytes from a dict and records every request and its offset."""

    def __init__(self, blobs: dict[str, bytes], *, chunk: int = 256, delay: float = 0.0) -> None:
        self.blobs = blobs
        self.chunk = chunk
        self.delay = delay
        self.requests: list[tuple[str, int]] = []
        #: Set once the first chunk of any file has been handed over, so a
        #: test can cancel a transfer that is genuinely under way.
        self.streaming = threading.Event()

    @contextmanager
    def open(self, url: str, *, offset: int = 0) -> Iterator[RemoteBody]:
        self.requests.append((url, offset))
        data = self.blobs[url][offset:]
        yield RemoteBody(chunks=self._chunks(data), resumed=offset > 0, total_bytes=len(data))

    def _chunks(self, data: bytes) -> Iterator[bytes]:
        for i in range(0, len(data), self.chunk):
            if self.delay:
                # A pause the registry can notice its cancel token between,
                # which is how a real transfer behaves and how F-64's cancel
                # is meant to land.
                time.sleep(self.delay)
            yield data[i : i + self.chunk]
            self.streaming.set()

    def urls(self) -> list[str]:
        return [url for url, _ in self.requests]


class GatedRegistry(ModelRegistry):
    """A registry whose deep pass blocks until released.

    Stands in for the 385 MB hash: the point of the deep pass is that it
    takes minutes, and no test can afford to prove responsiveness with real
    minutes.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.gate = threading.Event()
        self.deep_thread: int | None = None
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def verify(  # type: ignore[override]
        self,
        model_id: str,
        *,
        deep: bool = True,
        cancel: object | None = None,
        progress: object | None = None,
        root: Path | None = None,
    ):
        if deep and progress is not None:
            self.deep_thread = threading.get_ident()
            self.gate.wait(10.0)
        return super().verify(
            model_id,
            deep=deep,
            cancel=cancel,  # type: ignore[arg-type]
            progress=progress,  # type: ignore[arg-type]
            root=root,
        )


class StoppedAfterOneFile(ModelRegistry):
    """A registry whose deep pass cancels itself once one file is checked.

    The cancellation that matters is the one that lands *after* the pass
    found something, and no test can press Cancel between two files of a
    three-file toy model on purpose.  Cancelling from inside the progress
    callback lands where a real Cancel does: the registry checks the token
    between files, so the first file's verdict is kept and the rest are
    reported unchecked.
    """

    def verify(  # type: ignore[override]
        self,
        model_id: str,
        *,
        deep: bool = True,
        cancel: CancelToken | None = None,
        progress: ProgressCallback | None = None,
        root: Path | None = None,
    ) -> VerifyReport:
        if deep and progress is not None and cancel is not None:
            progress = _cancel_after_one_tick(progress, cancel)
        return super().verify(model_id, deep=deep, cancel=cancel, progress=progress, root=root)


def _cancel_after_one_tick(report: ProgressCallback, token: CancelToken) -> ProgressCallback:
    def tick(progress: DownloadProgress) -> None:
        report(progress)
        token.cancel()

    return tick


def _blobs(entry: ModelEntry) -> dict[str, bytes]:
    return {resolve_url(entry, f): CONTENT[f.relative_path] for f in entry.files}


def _install(root: Path, *, skip: tuple[str, ...] = (), damage: tuple[str, ...] = ()) -> None:
    """Put a copy of the model on disk, optionally imperfect."""
    for path, body in CONTENT.items():
        if path in skip:
            continue
        if path in damage:
            # Same length, different bytes: only a digest sees this.
            body = bytes((b + 1) % 256 for b in body)
        target = root.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


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
    return ModelRegistry(Manifest((entry,)), fetcher=fetcher, package_cache_dirs={})


@pytest.fixture
def accepted(registry: ModelRegistry) -> ModelRegistry:
    """A registry whose owner has already accepted the terms (N-11)."""
    registry.accept_license(MODEL_ID)
    return registry


def _view(app: QApplication, registry: ModelRegistry, budget: Budget = ENOUGH) -> ModelsView:
    view = ModelsView(theme.LIGHT, registry, budget)
    view.resize(720, 900)
    view.ensurePolished()
    return view


@pytest.fixture
def view(app: QApplication, accepted: ModelRegistry) -> Iterator[ModelsView]:
    made = _view(app, accepted)
    yield made
    made.shutdown()
    made.deleteLater()


@pytest.fixture
def card(view: ModelsView) -> ModelCard:
    return view.card(MODEL_ID)


def pump(app: QApplication, done: Callable[[], bool], timeout: float = 15.0) -> None:
    """Run the GUI event loop until ``done``, the way a user's window does."""
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()
    assert done(), "the screen never reached the expected state"


def finish(app: QApplication, card: ModelCard) -> None:
    pump(app, lambda: not card.busy)


def texts(card: ModelCard) -> str:
    """Everything the card currently says, for a "is this reported at all"
    assertion that does not care which label carries it."""
    return "\n".join(
        [
            card.state_text.text(),
            card.avail_text.text(),
            card.requirements.text(),
            card.result.text(),
            card.progress_text.text(),
            *(lb.text() for lb in card._values.values()),
        ]
    )


# ------------------------------------------------------------------ F-63 ---


def test_a_model_that_cannot_run_is_listed_with_the_reason(
    app: QApplication, accepted: ModelRegistry
) -> None:
    """F-04: never hidden, never silently substituted -- shown, with why."""
    view = _view(app, accepted, TOO_SMALL)
    try:
        card = view.card(MODEL_ID)
        runnable, reason = accepted.can_run(MODEL_ID, TOO_SMALL)
        assert not runnable and reason
        assert card in view.cards
        assert not card.isHidden()
        assert reason in card.avail_text.text()
        # The reason names both the requirement and the current value, so
        # the owner knows which way to move the budget.
        assert "2.0 GiB" in card.avail_text.text()
        assert "1.0 GiB" in card.avail_text.text()
    finally:
        view.shutdown()


def test_a_budget_change_moves_the_model_between_available_and_not(
    app: QApplication, accepted: ModelRegistry
) -> None:
    view = _view(app, accepted, ENOUGH)
    try:
        card = view.card(MODEL_ID)
        assert "Runs within the current budget" in card.avail_text.text()
        view.set_budget(TOO_SMALL)
        assert "Unavailable" in card.avail_text.text()
        view.set_budget(ENOUGH)
        assert "Runs within the current budget" in card.avail_text.text()
    finally:
        view.shutdown()


def test_the_card_reports_version_storage_languages_and_licence(
    card: ModelCard, entry: ModelEntry
) -> None:
    """F-63's per-model facts, resolved against F-84's manifest entry."""
    shown = texts(card)
    assert entry.revision[:12] in shown
    assert card._values["version"].toolTip() == entry.revision
    assert entry.repo_id in shown
    assert "Korean" in shown and "English" in shown
    assert "44.1 kHz mono" in shown
    assert "Test RAIL" in shown
    assert "26.0 kB" in shown  # the manifest total, not what happens to be here


def test_required_storage_and_budget_constraints_are_reported_before_downloading(
    card: ModelCard, fetcher: FakeFetcher
) -> None:
    """F-63: reported *before* downloading, so with nothing yet fetched."""
    assert fetcher.requests == []
    assert card.state is ModelState.NOT_PRESENT
    said = card.requirements.text()
    assert not card.requirements.isHidden()
    assert "26.0 kB" in said  # required storage
    assert "2.0 GiB" in said and "10%" in said  # the model's minimum
    assert "4.0 GiB" in said and "20%" in said  # the budget in force


def test_the_storage_report_narrows_to_what_is_still_missing(
    card: ModelCard, registry: ModelRegistry
) -> None:
    """F-64 reuses sound data, so the total would overstate the next fetch."""
    _install(registry.model_dir(MODEL_ID), skip=(GRAPH,))
    card.reload()
    assert card.state is ModelState.PARTIAL
    assert "still to download" in card.requirements.text()
    assert card.download_button.text() == "Resume download"


def test_the_voice_and_speaking_style_controls_are_listed(card: ModelCard) -> None:
    """F-63 names them among the per-model facts; F-53 wants the voice
    descriptions in front of a person too."""
    card.details_button.setChecked(True)
    assert not card.details.isHidden()
    shown = "\n".join(
        lb.text() for lb in card.details.findChildren(type(card.state_text))  # QLabel
    )
    assert "Female 1" in shown and "Male 1" in shown
    assert "not yet reviewed" in shown
    assert "Natural" in shown and "Narration" in shown
    assert f"{TEMPO_MIN:.2f}x" in shown and f"{TEMPO_MAX:.2f}x" in shown


def test_the_model_cache_total_is_shown_and_follows_a_download(
    app: QApplication, view: ModelsView, card: ModelCard
) -> None:
    assert "Model cache uses" in view.usage.text()
    card.download_button.click()
    finish(app, card)
    assert "26.0 kB" in view.usage.text()


# ------------------------------------------------------------ F-80, N-11 ---


def test_a_download_is_refused_until_the_terms_are_accepted(
    app: QApplication, registry: ModelRegistry, fetcher: FakeFetcher
) -> None:
    """N-11: the restrictions are accepted before first preparation, and
    declining is a decision rather than a failure."""
    view = _view(app, registry)
    try:
        card = view.card(MODEL_ID)
        card.request_licence = lambda: False  # type: ignore[method-assign]
        card.download_button.click()
        assert not card.busy
        assert fetcher.requests == []
        assert "licence was not accepted" in card.result.text()
        assert registry.license_acceptance_required(MODEL_ID)
    finally:
        view.shutdown()


def test_accepting_the_terms_records_them_and_prepares_the_model(
    app: QApplication, registry: ModelRegistry
) -> None:
    view = _view(app, registry)
    try:
        card = view.card(MODEL_ID)
        seen: list[str] = []
        view.licence_accepted.connect(seen.append)
        card.request_licence = lambda: True  # type: ignore[method-assign]
        card.download_button.click()
        finish(app, card)
        assert seen == [MODEL_ID]
        assert not registry.license_acceptance_required(MODEL_ID)
        assert card.state is ModelState.READY
    finally:
        view.shutdown()


def test_the_restrictions_can_be_reviewed_after_they_were_accepted(
    card: ModelCard, entry: ModelEntry, accepted: ModelRegistry
) -> None:
    """F-80: reviewable inside the app, not only at the moment of consent.

    Opening the pane is a read: it must not touch the acceptance record.
    """
    card.licence_button.setChecked(True)
    assert not card.licence_pane.isHidden()
    shown = "\n".join(
        lb.text() for lb in card.licence_pane.findChildren(type(card.state_text))
    )
    for restriction in RESTRICTIONS:
        assert restriction in shown  # quoted, never paraphrased
    assert entry.license.pass_through_obligation in shown
    assert entry.license.notes[0] in shown
    assert not accepted.license_acceptance_required(MODEL_ID)


def test_an_unaccepted_licence_is_named_as_such_on_the_card(
    app: QApplication, registry: ModelRegistry
) -> None:
    view = _view(app, registry)
    try:
        assert "Not accepted yet" in view.card(MODEL_ID)._values["licence"].text()
    finally:
        view.shutdown()


# ------------------------------------------------------------------ F-64 ---


def test_a_download_reports_progress_and_ends_ready(
    app: QApplication, data_root: Path, entry: ModelEntry, accepted: ModelRegistry
) -> None:
    # Deliberately slow: a transfer that finishes before the main thread
    # looks at it would let a screen that reports nothing pass this.
    accepted._fetcher = FakeFetcher(_blobs(entry), chunk=256, delay=0.003)
    view = _view(app, accepted)
    try:
        card = view.card(MODEL_ID)
        prepared: list[str] = []
        view.model_prepared.connect(prepared.append)
        ticks: list[object] = []
        card.download_button.click()
        assert card.task is not None
        card.task.progressed.connect(ticks.append)
        finish(app, card)

        assert ticks, "F-64 requires progress to be visible"
        assert any(t.phase == "downloading" for t in ticks)  # type: ignore[attr-defined]
        assert max(t.fraction for t in ticks) > 0  # type: ignore[attr-defined]
        assert prepared == [MODEL_ID]
        assert card.state is ModelState.READY
        assert card.state_text.text() == "Ready"
        assert card.download_button.isHidden()
        assert card.requirements.isHidden()
        assert card.progress_row.isHidden()
    finally:
        view.shutdown()


def test_a_cancelled_download_is_never_shown_as_ready(
    app: QApplication, data_root: Path, entry: ModelEntry, accepted: ModelRegistry
) -> None:
    """F-64, in as many words.  The card takes the interrupted attempt's own
    report rather than a fresh guess at files nobody finished checking."""
    slow = FakeFetcher(_blobs(entry), chunk=256, delay=0.004)
    accepted._fetcher = slow
    view = _view(app, accepted)
    try:
        card = view.card(MODEL_ID)
        card.download_button.click()
        pump(app, slow.streaming.is_set)
        assert not card.cancel_button.isHidden()
        card.cancel_button.click()
        finish(app, card)

        assert card.state is not ModelState.READY
        assert card.state_text.text() != "Ready"
        assert accepted.quick_state(MODEL_ID) is not ModelState.READY
        assert "canceled" in card.result.text()
        assert card.download_button.text() == "Retry download"
        # The partly transferred bytes are kept, which is what makes the
        # retry below a resume rather than a second full fetch.
        assert (accepted.model_dir(MODEL_ID) / "onnx" / "graph.bin.part").exists()
    finally:
        view.shutdown()


def test_a_retry_resumes_the_part_file_and_finishes(
    app: QApplication, data_root: Path, entry: ModelEntry, accepted: ModelRegistry
) -> None:
    slow = FakeFetcher(_blobs(entry), chunk=256, delay=0.004)
    accepted._fetcher = slow
    view = _view(app, accepted)
    try:
        card = view.card(MODEL_ID)
        card.download_button.click()
        pump(app, slow.streaming.is_set)
        card.cancel_button.click()
        finish(app, card)

        quick = FakeFetcher(_blobs(entry))
        accepted._fetcher = quick
        card.download_button.click()
        finish(app, card)

        assert card.state is ModelState.READY
        graph_url = resolve_url(entry, entry.files[0])
        offsets = [off for url, off in quick.requests if url == graph_url]
        assert offsets and max(offsets) > 0, "the retry re-fetched from the start"
    finally:
        view.shutdown()


def test_a_repair_refetches_only_the_file_that_does_not_match(
    app: QApplication, card: ModelCard, registry: ModelRegistry, fetcher: FakeFetcher, entry
) -> None:
    """F-64's "reuse sound data, re-download what is corrupt", through the
    button F-65 puts it behind.

    The repair is reachable only after the check, because a tampered file is
    the right size and the redraw's cheap pass cannot see it.  That is the
    real sequence and the test walks it rather than reaching past it.
    """
    _install(registry.model_dir(MODEL_ID), damage=(GRAPH,))
    card.reload()
    assert card.state is ModelState.READY  # size-only cannot see tampering
    assert card.repair_button.isHidden()

    card.verify_button.click()
    finish(app, card)
    assert card.state is ModelState.CORRUPT
    assert not card.repair_button.isHidden()

    card.repair_button.click()
    finish(app, card)

    assert card.state is ModelState.READY
    assert fetcher.urls() == [resolve_url(entry, entry.files[0])]


def test_a_failed_download_offers_a_retry_and_says_why(
    app: QApplication, card: ModelCard, registry: ModelRegistry
) -> None:
    class Broken:
        @contextmanager
        def open(self, url: str, *, offset: int = 0) -> Iterator[RemoteBody]:
            raise EchoActError(Code.MODEL_DOWNLOAD_FAILED, "the mirror went away")
            yield  # pragma: no cover - never reached, keeps this a generator

    registry._fetcher = Broken()
    problems: list[EchoActError] = []
    card.problem.connect(problems.append)
    card.download_button.click()
    finish(app, card)

    assert [p.code for p in problems] == [Code.MODEL_DOWNLOAD_FAILED]
    assert "the mirror went away" in card.result.text()
    assert card.download_button.text() == "Retry download"
    assert card.state is not ModelState.READY


# ------------------------------------------------------------------ F-65 ---


def test_checking_the_files_runs_off_the_gui_thread(
    app: QApplication, data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    """Rule 7, and the reason F-65's check is a worker at all: the real pass
    hashes 385 MB, and a frozen window is not a progress report."""
    registry = GatedRegistry(Manifest((entry,)), fetcher=fetcher, package_cache_dirs={})
    registry.accept_license(MODEL_ID)
    _install(registry.model_dir(MODEL_ID))
    view = _view(app, registry)
    try:
        card = view.card(MODEL_ID)
        card.verify_button.click()
        assert card.busy

        # While the hash is blocked, the event loop still delivers work.  A
        # timer that never fires is what a frozen window looks like.
        fired: list[bool] = []
        QTimer.singleShot(0, lambda: fired.append(True))
        pump(app, lambda: bool(fired), timeout=2.0)
        assert card.busy, "the check finished before responsiveness was proven"
        assert not card.verify_button.isEnabled()

        registry.gate.set()
        finish(app, card)
        assert registry.deep_thread not in (None, threading.get_ident())
        assert card.task is None
        assert card.state is ModelState.READY
        assert "match the manifest" in card.result.text()
    finally:
        registry.gate.set()
        view.shutdown()


def test_a_tampered_file_is_reported_as_damaged_rather_than_used(
    app: QApplication, card: ModelCard, registry: ModelRegistry
) -> None:
    """F-84: a model whose files do not match the manifest is corrupted.

    The cheap redraw cannot see this -- the file is the right size -- so the
    deep pass's verdict has to outrank it on the card as well as inside the
    registry.
    """
    _install(registry.model_dir(MODEL_ID), damage=(GRAPH,))
    card.reload()
    assert card.status is not None and card.status.state is not ModelState.CORRUPT

    card.verify_button.click()
    finish(app, card)

    assert card.state is ModelState.CORRUPT
    assert card.state_text.text() == "Damaged"
    assert "do not match the manifest" in card.result.text()


def test_a_cancelled_check_reports_that_it_stopped(
    app: QApplication, data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    registry = GatedRegistry(Manifest((entry,)), fetcher=fetcher, package_cache_dirs={})
    registry.accept_license(MODEL_ID)
    _install(registry.model_dir(MODEL_ID))
    view = _view(app, registry)
    try:
        card = view.card(MODEL_ID)
        card.verify_button.click()
        card.cancel_button.click()
        registry.gate.set()
        finish(app, card)
        assert "stopped" in card.result.text()
        assert "Nothing was changed" in card.result.text()
    finally:
        registry.gate.set()
        view.shutdown()


def test_a_cancelled_check_does_not_call_an_installed_model_missing(
    app: QApplication, data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    """A stopped pass establishes nothing, and the card must not pretend it did.

    Every file a cancelled pass did not reach comes back unchecked, which
    collapses to NOT_PRESENT -- so pinning a cancelled verdict would leave a
    complete, digest-sound model reading "Not downloaded", offering Download
    and quoting the full storage cost, with no redraw able to take it back.
    """
    registry = GatedRegistry(Manifest((entry,)), fetcher=fetcher, package_cache_dirs={})
    registry.accept_license(MODEL_ID)
    _install(registry.model_dir(MODEL_ID))
    view = _view(app, registry)
    try:
        card = view.card(MODEL_ID)
        assert card.state is ModelState.READY

        card.verify_button.click()
        card.cancel_button.click()
        registry.gate.set()
        finish(app, card)

        assert "stopped" in card.result.text()
        # Back to what the files actually support, which is what the card
        # said before the check the user stopped.
        assert card.state is ModelState.READY
        assert card.state_text.text() == "Ready"
        assert "Not downloaded" not in texts(card)
        assert card.download_button.isHidden()
        assert card.repair_button.isHidden()
        assert card.requirements.text() == ""
        assert "Requires" not in texts(card)

        # And it stays that way: a pinned lie would survive all three of
        # these, because the cheap pass agrees with READY and only a cheap
        # pass that disproves READY drops a pin.
        card.reload()
        view.refresh()
        card.set_budget(ENOUGH)
        assert card.state is ModelState.READY
        assert card.state_text.text() == "Ready"
    finally:
        registry.gate.set()
        view.shutdown()


def test_a_check_stopped_after_it_found_damage_keeps_that_verdict(
    app: QApplication, data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    """The other half: what a stopped pass did see still outranks the redraw.

    A tampered file is the right size, so the presence-and-size pass calls
    the model ready; dropping the verdict of every cancelled pass would hand
    F-84's corrupted model straight back to the user as sound.
    """
    registry = StoppedAfterOneFile(Manifest((entry,)), fetcher=fetcher, package_cache_dirs={})
    registry.accept_license(MODEL_ID)
    _install(registry.model_dir(MODEL_ID), damage=(GRAPH,))
    view = _view(app, registry)
    try:
        card = view.card(MODEL_ID)
        assert card.state is ModelState.READY, "the cheap pass cannot see a tampered file"

        card.verify_button.click()
        finish(app, card)

        assert card.state is ModelState.CORRUPT
        assert card.state_text.text() == "Damaged"
        card.reload()
        assert card.state is ModelState.CORRUPT
        assert not card.repair_button.isHidden()
    finally:
        view.shutdown()


def test_deleting_a_model_keeps_documents_audio_and_another_program_cache(
    app: QApplication, data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    """F-65: deletion frees the model cache and nothing else."""
    package = data_root / "someone-elses-cache"
    _install(package)
    registry = ModelRegistry(
        Manifest((entry,)), fetcher=fetcher, package_cache_dirs={MODEL_ID: package}
    )
    registry.accept_license(MODEL_ID)
    _install(registry.model_dir(MODEL_ID))
    paths.audio_dir().mkdir(parents=True, exist_ok=True)
    kept = paths.audio_dir() / "job-1.wav"
    kept.write_bytes(b"RIFF....")

    view = _view(app, registry)
    try:
        card = view.card(MODEL_ID)
        deleted: list[str] = []
        view.model_deleted.connect(deleted.append)
        card.confirm = lambda *_: True  # type: ignore[method-assign]
        card.delete_button.click()

        assert deleted == [MODEL_ID]
        assert not registry.model_dir(MODEL_ID).exists()
        assert kept.read_bytes() == b"RIFF...."
        assert (package / "onnx" / "graph.bin").exists()
        assert "Documents and audio results were kept" in card.result.text()
        # The borrowed copy is still usable and still not ours to delete.
        assert card.status is not None and card.status.using_package_cache
        assert not card.package_note.isHidden()
        assert card.delete_button.isHidden()
    finally:
        view.shutdown()


def test_deleting_a_model_in_use_needs_a_confirmation(
    app: QApplication, view: ModelsView, card: ModelCard, registry: ModelRegistry
) -> None:
    _install(registry.model_dir(MODEL_ID))
    view.set_in_use(MODEL_ID)
    card.reload()
    asked: list[str] = []

    def refuse(title: str, body: str, accept: str) -> bool:
        asked.append(title)
        return False

    card.confirm = refuse  # type: ignore[method-assign]
    card.delete_button.click()

    assert asked == ["A job is using this model."]
    assert (registry.model_dir(MODEL_ID) / "onnx" / "graph.bin").exists()


def test_deleting_a_model_in_use_is_refused_until_the_job_is_released(
    app: QApplication, view: ModelsView, card: ModelCard, registry: ModelRegistry
) -> None:
    """F-65: the confirmation is to cancel the job *first*.  If nobody
    answers the release, the deletion fails with the reason rather than
    silently pulling files out from under a running job."""
    _install(registry.model_dir(MODEL_ID))
    view.set_in_use(MODEL_ID)
    card.confirm = lambda *_: True  # type: ignore[method-assign]
    problems: list[EchoActError] = []
    view.problem.connect(problems.append)

    asked: list[str] = []
    view.release_requested.connect(asked.append)
    card.delete_button.click()

    assert asked == [MODEL_ID]
    assert [p.code for p in problems] == [Code.DELETE_BLOCKED_IN_USE]
    assert (registry.model_dir(MODEL_ID) / "onnx" / "graph.bin").exists()


def test_a_released_model_is_then_deleted(
    app: QApplication, view: ModelsView, card: ModelCard, registry: ModelRegistry
) -> None:
    _install(registry.model_dir(MODEL_ID))
    view.set_in_use(MODEL_ID)
    card.confirm = lambda *_: True  # type: ignore[method-assign]
    # The window's part of F-65: cancel the job, release the model, say so.
    view.release_requested.connect(lambda _: view.set_in_use(None))

    card.delete_button.click()

    assert not registry.model_dir(MODEL_ID).exists()
    assert card.state is ModelState.NOT_PRESENT


# ------------------------------------------------------- F-09, Section 5.3 ---


def test_the_owner_authorises_external_downloads_per_model(
    card: ModelCard, registry: ModelRegistry
) -> None:
    """Section 5.3: without this switch a REST or MCP request cannot start a
    download, and the switch is the GUI's alone."""
    assert not card.authorise.isChecked()
    assert not registry.download_authorised(MODEL_ID)
    card.authorise.setChecked(True)
    assert registry.download_authorised(MODEL_ID)
    card.reload()
    assert card.authorise.isChecked()
    card.authorise.setChecked(False)
    assert not registry.download_authorised(MODEL_ID)


def test_controls_are_disabled_while_work_is_running(
    app: QApplication, data_root: Path, entry: ModelEntry, fetcher: FakeFetcher
) -> None:
    """N-09: an unavailable control looks unavailable, and F-47's one-thing-
    at-a-time applies to the registry too -- a delete during a download is
    what N-23 refuses."""
    registry = GatedRegistry(Manifest((entry,)), fetcher=fetcher, package_cache_dirs={})
    registry.accept_license(MODEL_ID)
    _install(registry.model_dir(MODEL_ID))
    view = _view(app, registry)
    try:
        card = view.card(MODEL_ID)
        card.verify_button.click()
        assert card.busy
        for button in (card.verify_button, card.delete_button, card.download_button):
            assert not button.isEnabled()
        assert not card.cancel_button.isHidden()

        registry.gate.set()
        finish(app, card)
        assert card.verify_button.isEnabled()
        assert card.cancel_button.isHidden()
    finally:
        registry.gate.set()
        view.shutdown()


def test_every_string_this_screen_shows_has_a_korean_translation() -> None:
    """F-86: the display language is Korean or English, and a label that
    ships English-only is a defect nobody notices until a Korean user does.

    The literals are read out of the module rather than off the widgets
    because most of them reach the screen through ``format`` and would no
    longer match their catalogue key by the time they are visible.
    """
    source = Path(models_view.__file__).read_text(encoding="utf-8")
    literals = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tr"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    # The words chosen at runtime from a table, which no literal scan sees.
    literals |= {word for word, _, _ in models_view._STATE_TEXT.values()}
    literals |= set(models_view._LANGUAGE_NAMES.values())
    literals |= set(models_view._STYLE_NAMES.values())

    was = i18n.current()
    try:
        i18n.set_language(i18n.Lang.KO)
        missing = sorted(text for text in literals if i18n.tr(text) == text)
    finally:
        i18n.set_language(was)
    assert missing == []


def test_shutdown_stops_a_running_worker(
    app: QApplication, data_root: Path, entry: ModelEntry
) -> None:
    """N-22: closing the window releases resources rather than waiting out a
    385 MB transfer."""
    slow = FakeFetcher(_blobs(entry), chunk=256, delay=0.004)
    registry = ModelRegistry(Manifest((entry,)), fetcher=slow, package_cache_dirs={})
    registry.accept_license(MODEL_ID)
    view = _view(app, registry)
    card = view.card(MODEL_ID)
    card.download_button.click()
    task = card.task
    assert task is not None
    pump(app, slow.streaming.is_set)

    view.shutdown()

    assert task.isFinished()
    assert registry.quick_state(MODEL_ID) is not ModelState.READY
    view.deleteLater()

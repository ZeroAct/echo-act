"""The whole stack, once, for real.

Every layer has its own tests and most of them use a fake for the layer
below.  That is the right trade almost everywhere and the wrong one
somewhere: a fake that has drifted passes both sides of a broken seam.  So
this drives the composition root exactly as ``python -m echoact`` does --
real settings file, real database, real registry, real worker process, real
Supertonic weights -- and checks the properties that only exist once the
parts are together.

Marked ``engine`` and skipped without the weights.  It takes tens of
seconds; that is what it costs to know.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from echoact import paths
from echoact.app import Application
from echoact.audio import wav
from echoact.audio.player import Player, PlayerState, Timeline
from echoact.domain import JobState, RequestPath, RetentionMode
from echoact.errors import EchoActError
from echoact.jobs.request import JobRequest, estimate
from echoact.models.catalog import MANIFEST, SUPERTONIC_3_ID
from echoact.util import ids

pytestmark = pytest.mark.engine

KO_EN = "에코액트 테스트입니다. This is the second sentence. 세 번째 문장입니다."


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    application = Application()
    if application.registry.state(SUPERTONIC_3_ID).name not in {"READY", "NOT_PRESENT"}:
        pytest.skip("the model cache is in an unexpected state")
    try:
        yield application
    finally:
        application.shutdown()
        paths.data_dir.cache_clear()


def _weights_present() -> bool:
    return (Path.home() / ".cache" / "supertonic3" / "onnx" / "vocoder.onnx").exists()


def _run(app: Application, text: str, *, retain: bool = False, timeout: float = 240.0):
    request = JobRequest(
        text=text,
        settings=app.settings.voice,
        request_path=RequestPath.GUI,
        owner_client_id="owner",
        idempotency_key=ids.request_id(),
        retention=RetentionMode.RETAINED if retain else RetentionMode.ONE_OFF,
    )
    job, created = app.engine.submit(request)
    assert created
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = app.store.get_job(job.job_id)
        if current.state.is_terminal:
            return current
        time.sleep(0.05)
    raise AssertionError("the job never reached a terminal state")


# ------------------------------------------------------------ the licence ---


def test_generation_is_refused_until_the_licence_is_accepted(app: Application) -> None:
    """N-11 gates preparation, and the composition root is where that has
    to actually bite -- the engine's own tests accept it in a fixture."""
    if not _weights_present():
        pytest.skip("no weights to prepare")
    assert app.licence_pending() == SUPERTONIC_3_ID
    with pytest.raises(EchoActError):
        _run(app, "짧은 문장입니다.")
    app.accept_licence(SUPERTONIC_3_ID)
    assert app.licence_pending() is None


# ------------------------------------------------------------- generation ---


def test_a_job_run_through_the_real_stack_is_playable(app: Application) -> None:
    if not _weights_present():
        pytest.skip("no weights")
    app.accept_licence(SUPERTONIC_3_ID)
    done = _run(app, KO_EN)
    assert done.state is JobState.COMPLETE, done.error_message

    info = wav.probe(done.result.relative_path)
    assert info.is_output_format
    assert info.sample_rate == 44100
    assert info.duration_ms > 1000

    segments = app.store.list_segments(done.job_id)
    assert len(segments) >= 3
    assert all(s.ready and s.time is not None for s in segments)

    # The segment table must tile the audio exactly: F-27's mapping is what
    # the highlight follows, and a hole in it is a defect no interface hides.
    at = 0
    for s in segments:
        assert s.time.start_ms == at
        at = s.time.end_ms
    assert abs(at - info.duration_ms) <= 2


def test_the_source_ranges_still_describe_what_the_user_typed(app: Application) -> None:
    """F-27: a segment's range refers to the original text, never to the
    normalised form the engine was given.  Numbers are where that breaks."""
    if not _weights_present():
        pytest.skip("no weights")
    app.accept_licence(SUPERTONIC_3_ID)
    text = "가격은 1,234원입니다. It cost $12.50 on 2026-09-10."
    done = _run(app, text)
    assert done.state is JobState.COMPLETE

    segments = app.store.list_segments(done.job_id)
    rebuilt = "".join(text[s.source.start : s.source.end] for s in segments)
    assert rebuilt == text, "the segment ranges do not reconstruct the source text"
    # And at least one segment was actually normalised, or the test proves
    # nothing about alignment.
    assert any(
        s.spoken_text != text[s.source.start : s.source.end] for s in segments
    ), "nothing was normalised, so alignment was not exercised"


def test_the_second_job_does_not_reload_the_model(app: Application) -> None:
    """F-17: a warm model is reused.  A.5 measured the reload at 0.8 s, so
    this checks the reuse rather than the timing."""
    if not _weights_present():
        pytest.skip("no weights")
    app.accept_licence(SUPERTONIC_3_ID)
    _run(app, "첫 번째 작업입니다.")
    assert not app.engine.would_need_load()
    second = _run(app, "두 번째 작업입니다.")
    assert second.state is JobState.COMPLETE


def test_changing_only_the_voice_keeps_the_model(app: Application) -> None:
    """F-18 spells this out, and the supervisor makes it true by taking the
    voice as an argument to synthesize rather than to load."""
    if not _weights_present():
        pytest.skip("no weights")
    app.accept_licence(SUPERTONIC_3_ID)
    _run(app, "첫 번째 작업입니다.")
    app.update_settings(voice=app.settings.voice.with_(voice_id="M1", gender="male"))
    assert not app.engine.would_need_load(app.settings.voice)


# ------------------------------------------------------------- playback ---


class _Clock:
    def __init__(self, dac: float) -> None:
        self.outputBufferDacTime = dac  # noqa: N815 - PortAudio's own name


def test_the_generated_job_drives_the_player_and_its_clock(app: Application) -> None:
    """The seam this file exists for: segments produced by the engine, fed
    to the player, produce a monotonically advancing position.

    Driven through the callback rather than a device, so it makes no sound
    and needs no speaker.
    """
    if not _weights_present():
        pytest.skip("no weights")
    app.accept_licence(SUPERTONIC_3_ID)
    done = _run(app, KO_EN)
    segments = app.store.list_segments(done.job_id)

    timeline = Timeline(sample_rate=44100)
    for s in segments:
        span = wav.frames_for_ms(s.time.end_ms, 44100) - wav.frames_for_ms(s.time.start_ms, 44100)
        timeline.append(s.index, s.audio_path or "", s.frame_count, max(0, span - s.frame_count))
    timeline.complete = True

    player = Player()
    player.load(timeline)
    player._feeder.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and player._feeder.read_frame < 44100:
        time.sleep(0.005)
    player._state = PlayerState.PLAYING
    try:
        positions = []
        for i in range(60):
            buf = memoryview(bytearray(512 * 2))
            player._fill(buf, 512, _Clock(i * 512 / 44100))
            positions.append(player._played_frames)
        assert positions == sorted(positions)
        assert positions[-1] > 0
        assert timeline.duration_ms == pytest.approx(
            wav.probe(done.result.relative_path).duration_ms, abs=3
        )
    finally:
        player.close()


# ------------------------------------------------------------- estimate ---


def test_the_estimate_is_in_the_right_neighbourhood(app: Application) -> None:
    """F-88 calls the estimate approximate, so this checks it is useful
    rather than exact.  Anything worse than a factor of two would make the
    pre-flight answer misleading instead of approximate."""
    if not _weights_present():
        pytest.skip("no weights")
    app.accept_licence(SUPERTONIC_3_ID)
    est = estimate(KO_EN, app.settings.voice, MANIFEST, slot_free=True, model_ready=True)
    done = _run(app, KO_EN)
    actual = wav.probe(done.result.relative_path).duration_ms
    ratio = est.audio_ms / actual
    assert 0.5 < ratio < 2.0, f"estimated {est.audio_ms} ms against {actual} ms actual"


# ---------------------------------------------------------- retention ---


def test_a_retained_job_survives_a_restart_and_a_one_off_does_not(
    tmp_path, monkeypatch
) -> None:
    """4.1 and N-02 together: retained data is kept until the user deletes
    it, and a one-off result is cleaned up on a normal exit."""
    if not _weights_present():
        pytest.skip("no weights")
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()

    first = Application()
    first.accept_licence(SUPERTONIC_3_ID)
    retained = _run(first, "보관되는 작업입니다.", retain=True)
    one_off = _run(first, "한 번만 쓰는 작업입니다.", retain=False)
    assert retained.state is JobState.COMPLETE
    assert one_off.state is JobState.COMPLETE
    one_off_file = Path(one_off.result.relative_path)
    first.shutdown()

    paths.data_dir.cache_clear()
    second = Application()
    try:
        again = second.store.get_job(retained.job_id)
        assert again.state is JobState.COMPLETE
        assert again.source_text == "보관되는 작업입니다."
        assert Path(again.result.relative_path).exists()
        assert not one_off_file.exists(), "a one-off result outlived a normal exit"
    finally:
        second.shutdown()
        paths.data_dir.cache_clear()

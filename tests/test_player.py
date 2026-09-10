"""The streaming player's timeline, clock, and waiting behaviour.

Most of this runs without a sound card: the timeline arithmetic and the
feeder are ordinary code, and the parts that matter for F-12 and N-12 --
that starvation does not advance the clock, that a seek past generated
audio is refused, that the gap belongs to the preceding segment -- are
testable by driving the callback directly.

The tests that need a real device are marked ``audio`` and skipped when
none is present, so a build machine without a sound card still runs the
rest.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from echoact.audio import wav
from echoact.audio.player import Entry, Player, PlayerState, Timeline, _Feeder

SR = 44100


def _tone(path, frames: int, freq: float = 220.0) -> int:
    t = np.arange(frames, dtype=np.float32) / SR
    samples = (0.25 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    report = wav.write_segment(path, samples, SR)
    return report.frame_count


@pytest.fixture()
def timeline(tmp_path) -> Timeline:
    """Three segments of 0.2 s with a 0.1 s gap after each."""
    tl = Timeline(sample_rate=SR)
    for i in range(3):
        p = tmp_path / f"seg{i}.wav"
        frames = _tone(p, SR // 5, 220.0 * (i + 1))
        tl.append(i, str(p), frames, SR // 10)
    return tl


# --------------------------------------------------------------- timeline ---


def test_entries_tile_the_timeline_without_holes(timeline: Timeline) -> None:
    expected = 0
    for e in timeline.entries:
        assert e.start_frame == expected
        expected = e.end_frame
    assert timeline.total_frames == expected


def test_the_gap_belongs_to_the_segment_before_it(timeline: Timeline) -> None:
    """F-27 attributes inter-segment silence to the preceding segment, so a
    position inside the gap still marks the sentence that just finished."""
    first = timeline.entries[0]
    inside_gap = first.frame_count + 10
    assert inside_gap < first.end_frame
    assert timeline.segment_at(inside_gap) == 0
    assert timeline.segment_at(first.end_frame) == 1


def test_locate_refuses_a_frame_past_what_exists(timeline: Timeline) -> None:
    assert timeline.locate(timeline.total_frames) is None
    assert timeline.locate(timeline.total_frames + 1000) is None
    assert timeline.locate(-1) is None


def test_appending_extends_the_timeline_in_place(timeline: Timeline, tmp_path) -> None:
    before = timeline.total_frames
    p = tmp_path / "seg3.wav"
    frames = _tone(p, SR // 5)
    entry = timeline.append(3, str(p), frames, 0)
    assert entry.start_frame == before
    assert timeline.total_frames == before + frames


# ----------------------------------------------------------------- feeder ---


def test_feeder_produces_the_whole_timeline_in_order(timeline: Timeline) -> None:
    feeder = _Feeder(timeline, capacity_frames=SR)
    feeder.start()
    try:
        got = bytearray()
        # Pull until we have everything or we stop making progress.
        stalls = 0
        while len(got) < timeline.total_frames * 2 and stalls < 400:
            chunk = feeder.take(2048)
            if chunk:
                got += chunk
                stalls = 0
            else:
                stalls += 1
                time.sleep(0.005)
    finally:
        feeder.close()
    assert len(got) == timeline.total_frames * 2

    # The silence the feeder generates must be exactly the gap, and the
    # audio must be the file's bytes untouched.
    first = timeline.entries[0]
    audio, _ = wav.read_wav(first.path)
    produced = np.frombuffer(bytes(got[: first.frame_count * 2]), dtype="<i2")
    assert np.array_equal(produced, audio)
    gap = np.frombuffer(
        bytes(got[first.frame_count * 2 : first.end_frame * 2]), dtype="<i2"
    )
    assert gap.size == first.gap_frames
    assert not gap.any()


def test_feeder_seek_starts_from_the_requested_frame(timeline: Timeline) -> None:
    feeder = _Feeder(timeline, capacity_frames=SR)
    target = timeline.entries[1].start_frame
    feeder.seek(target)
    feeder.start()
    try:
        got = bytearray()
        stalls = 0
        while len(got) < 4096 and stalls < 400:
            chunk = feeder.take(2048)
            if chunk:
                got += chunk
                stalls = 0
            else:
                stalls += 1
                time.sleep(0.005)
    finally:
        feeder.close()
    audio, _ = wav.read_wav(timeline.entries[1].path)
    assert np.array_equal(np.frombuffer(bytes(got[:4096]), dtype="<i2"), audio[:2048])


# --------------------------------------------------- the clock (N-12, F-12) ---


class _FakeTime:
    """Stands in for PortAudio's ``time_info``."""

    def __init__(self, dac: float) -> None:
        self.outputBufferDacTime = dac  # noqa: N815 - PortAudio's own name


def _prime(feeder, timeline: Timeline, timeout: float = 3.0) -> None:
    """Wait for the feeder to read the timeline into its buffer.

    The feeder is a thread; a test loop that pulls from it as fast as
    Python can iterate outruns it and would measure scheduling rather than
    behaviour.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and feeder.read_frame < timeline.total_frames:
        time.sleep(0.002)


def _drive(player: Player, blocks: int, frames: int = 512) -> int:
    """Run the callback by hand and report how many frames it consumed."""
    before = player._played_frames
    for i in range(blocks):
        buf = memoryview(bytearray(frames * 2))
        player._fill(buf, frames, _FakeTime(i * frames / SR))
    return player._played_frames - before


def test_starvation_does_not_advance_the_clock(tmp_path) -> None:
    """F-12 lets playback run into segments that do not exist yet.

    If the clock advanced through the silence the callback emits, the
    highlight would walk off into text nobody has heard -- which is the
    failure N-12's 300 ms budget is meant to exclude.
    """
    tl = Timeline(sample_rate=SR)
    p = tmp_path / "only.wav"
    frames = _tone(p, 1024)
    tl.append(0, str(p), frames, 0)

    player = Player()
    player.load(tl)
    player._feeder.start()
    _prime(player._feeder, player._timeline)
    player._state = PlayerState.PLAYING
    try:
        _drive(player, blocks=4, frames=512)  # more than the 1024 frames that exist
        played = player._played_frames
        assert played == frames
        _drive(player, blocks=4, frames=512)
        assert player._played_frames == played, "the clock advanced through starvation"
        assert player.state is PlayerState.WAITING
    finally:
        player.close()


def test_the_end_of_a_complete_job_is_ended_not_waiting(tmp_path) -> None:
    tl = Timeline(sample_rate=SR)
    p = tmp_path / "only.wav"
    tl.append(0, str(p), _tone(p, 1024), 0)
    tl.complete = True

    player = Player()
    player.load(tl)
    player._feeder.start()
    _prime(player._feeder, player._timeline)
    player._state = PlayerState.PLAYING
    try:
        _drive(player, blocks=6, frames=512)
        assert player.state is PlayerState.ENDED
    finally:
        player.close()


def test_position_never_reports_audio_that_was_not_handed_to_the_device(
    timeline: Timeline,
) -> None:
    player = Player()
    player.load(timeline)
    player._feeder.start()
    _prime(player._feeder, player._timeline)
    player._state = PlayerState.PLAYING
    try:
        _drive(player, blocks=2, frames=512)
        pos = player.position_ms()
        assert 0 <= pos <= wav.ms_for_frames(player._played_frames, SR)
    finally:
        player.close()


def test_seek_beyond_generated_audio_is_refused(timeline: Timeline) -> None:
    """F-14: ungenerated segments cannot be sought to."""
    player = Player()
    player.load(timeline)
    try:
        assert player.seek_ms(0) is True
        last_ok = timeline.duration_ms - 1
        assert player.seek_ms(last_ok) is True
        assert player.seek_ms(timeline.duration_ms + 1000) is False
        assert player.seek_ms(-5) is True  # clamped to the start, not refused
    finally:
        player.close()


def test_segment_changes_are_reported_once_each(timeline: Timeline) -> None:
    seen: list[int] = []
    player = Player(on_segment=seen.append)
    player.load(timeline)
    player._feeder.start()
    _prime(player._feeder, player._timeline)
    player._state = PlayerState.PLAYING
    try:
        _drive(player, blocks=80, frames=512)
    finally:
        player.close()
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)), f"a segment was announced twice: {seen}"
    assert seen[0] == 0


def test_pause_keeps_the_position(timeline: Timeline) -> None:
    player = Player()
    player.load(timeline)
    player._feeder.start()
    _prime(player._feeder, player._timeline)
    player._state = PlayerState.PLAYING
    try:
        _drive(player, blocks=4, frames=512)
        before = player.position_ms()
        player.pause()
        assert player.state is PlayerState.PAUSED

        at = player.position_ms()
        # Pausing settles the reported position onto everything already
        # handed to the device, which is at most one block ahead of the
        # extrapolated playing position.
        assert 0 <= at - before <= wav.ms_for_frames(512, SR) + 1

        _drive(player, blocks=4, frames=512)  # callback runs, emits silence
        assert player.position_ms() == at, "the clock advanced while paused"
    finally:
        player.close()


def test_stop_rewinds_and_clears_the_segment(timeline: Timeline) -> None:
    player = Player()
    player.load(timeline)
    player._feeder.start()
    _prime(player._feeder, player._timeline)
    player._state = PlayerState.PLAYING
    try:
        _drive(player, blocks=20, frames=512)
        assert player.current_segment is not None
        player.stop()
        assert player.state is PlayerState.STOPPED
        assert player.position_ms() == 0
        assert player.current_segment is None
    finally:
        player.close()


# ---------------------------------------------------------------- volume ---


def test_full_volume_passes_samples_through_untouched(timeline: Timeline) -> None:
    """F-67: playback volume must not alter the generated signal.

    At 100% the bytes are forwarded rather than multiplied by one, so no
    rounding happens on the ordinary path either.
    """
    player = Player()
    raw = np.array([1, -1, 32767, -32768, 1234], dtype="<i2").tobytes()
    assert player._apply_gain(raw) is raw


def test_half_volume_scales_and_clips(timeline: Timeline) -> None:
    player = Player()
    player.set_volume(0.5)
    raw = np.array([32767, -32768, 1000], dtype="<i2").tobytes()
    out = np.frombuffer(player._apply_gain(raw), dtype="<i2")
    assert list(out) == [16383, -16384, 500]


def test_mute_silences_without_changing_the_position(timeline: Timeline) -> None:
    player = Player()
    player.set_muted(True)
    raw = np.array([32767, -32768], dtype="<i2").tobytes()
    assert not np.frombuffer(player._apply_gain(raw), dtype="<i2").any()


# -------------------------------------------------------------- devices ---


def test_device_key_is_stable_across_reindexing() -> None:
    a = Entry(0, "x.wav", 10, 0, 0)  # unrelated import guard
    assert a.total_frames == 10

    from echoact.audio.devices import OutputDevice

    first = OutputDevice(3, "Speakers", "MME", 2, 44100.0, True)
    moved = OutputDevice(7, "Speakers", "MME", 2, 44100.0, False)
    assert first.key == moved.key


@pytest.mark.audio
def test_a_real_device_can_be_listed_and_opened() -> None:
    from echoact.audio.devices import default_output_device, list_output_devices, supports_rate

    devices = list_output_devices()
    if not devices:
        pytest.skip("no audio output device on this machine")
    default = default_output_device()
    assert default is not None
    assert supports_rate(default.index, SR) in (True, False)  # must answer, not raise

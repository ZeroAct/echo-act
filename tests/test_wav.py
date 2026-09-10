"""F-82's format and concatenation, and F-16's partial export.

A-21 makes the bit-exactness of the concatenation a release condition, so it
is checked against the raw data chunks rather than against a decoded array:
comparing samples would pass even if the writer had silently changed the
sample width or byte order.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from echoact import paths, policy
from echoact.audio import wav
from echoact.domain import Segment, TextRange
from echoact.errors import Code, EchoActError

RATE = 44_100  # Supertonic 3's native rate (A.5), never resampled per F-82


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test off the real user data directory.

    ``paths.data_dir`` is ``lru_cache``d, so the cache has to be cleared on
    the way in *and* on the way out; a stale entry would otherwise leak this
    tmp_path into whatever runs next.
    """
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(root))
    paths.data_dir.cache_clear()
    yield root
    paths.data_dir.cache_clear()


# ----------------------------------------------------------------- helpers


def tone(frames: int, *, freq: float = 220.0, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(frames, dtype=np.float32) / RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def data_chunk(path: Path) -> bytes:
    """The PCM payload exactly as it sits in the file.

    Parsed here rather than through ``wave`` so the comparison in the
    bit-exactness test does not depend on the same code it is checking.
    """
    raw = path.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE", "not a RIFF/WAVE file"
    pos = 12
    while pos + 8 <= len(raw):
        name = raw[pos : pos + 4]
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        body = raw[pos + 8 : pos + 8 + size]
        if name == b"data":
            return body
        pos += 8 + size + (size & 1)
    raise AssertionError("no data chunk")


def write_tone(path: Path, frames: int, **kw: float) -> wav.WriteReport:
    return wav.write_segment(path, tone(frames, **kw), RATE)


def ready_segment(index: int, path: Path | None, source: tuple[int, int], gap_ms: int) -> Segment:
    return Segment(
        index=index,
        source=TextRange(*source),
        spoken_text="x" if path is not None else "",
        language="ko",
        trailing_silence_ms=gap_ms,
        audio_path=None if path is None else str(path),
        ready=True,
    )


# ------------------------------------------------------------- write_segment


def test_a_segment_is_written_as_mono_16_bit_pcm_at_the_model_rate(tmp_path: Path) -> None:
    report = write_tone(tmp_path / "s.wav", 1000)

    info = wav.probe(tmp_path / "s.wav")
    assert (info.channels, info.sample_width_bits, info.sample_rate) == (1, 16, RATE)
    assert info.format == wav.FORMAT_NAME
    assert info.is_output_format
    assert report.frame_count == 1000
    assert info.frame_count == 1000


def test_full_scale_floats_reach_the_int16_extremes_without_wrapping(tmp_path: Path) -> None:
    wav.write_segment(tmp_path / "s.wav", np.array([1.0, -1.0, 0.0], dtype=np.float32), RATE)

    samples, rate = wav.read_wav(tmp_path / "s.wav")
    assert rate == RATE
    assert list(samples) == [32767, -32767, 0]


def test_samples_past_full_scale_clip_instead_of_wrapping_to_the_other_sign(
    tmp_path: Path,
) -> None:
    report = wav.write_segment(
        tmp_path / "s.wav", np.array([1.5, -1.5, 0.25], dtype=np.float32), RATE
    )

    samples, _ = wav.read_wav(tmp_path / "s.wav")
    assert list(samples) == [32767, -32768, 8192]
    assert report.clipped == 2
    assert report.peak == pytest.approx(1.5)


def test_conversion_rounds_to_nearest_rather_than_toward_zero(tmp_path: Path) -> None:
    quiet = np.array([0.6, 1.4, -0.6, -1.4], dtype=np.float64) / wav.FLOAT_FULL_SCALE

    wav.write_segment(tmp_path / "s.wav", quiet, RATE)

    samples, _ = wav.read_wav(tmp_path / "s.wav")
    assert list(samples) == [1, 1, -1, -1]


def test_int16_input_is_written_through_untouched(tmp_path: Path) -> None:
    given = np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)

    report = wav.write_segment(tmp_path / "s.wav", given, RATE)

    samples, _ = wav.read_wav(tmp_path / "s.wav")
    assert list(samples) == list(given)
    assert report.clipped == 0
    assert report.peak == pytest.approx(1.0)


def test_a_column_shaped_array_is_accepted_as_mono(tmp_path: Path) -> None:
    report = wav.write_segment(tmp_path / "s.wav", tone(64).reshape(1, -1), RATE)
    assert report.frame_count == 64


def test_genuinely_multichannel_audio_is_refused(tmp_path: Path) -> None:
    stereo = np.zeros((10, 2), dtype=np.float32)

    with pytest.raises(EchoActError) as caught:
        wav.write_segment(tmp_path / "s.wav", stereo, RATE)
    assert caught.value.code is Code.INTERNAL


def test_a_segment_with_no_audio_is_a_valid_empty_file(tmp_path: Path) -> None:
    report = wav.write_segment(tmp_path / "s.wav", np.zeros(0, dtype=np.float32), RATE)

    assert report.frame_count == 0
    assert report.peak == 0.0
    assert wav.probe(tmp_path / "s.wav").frame_count == 0


def test_nothing_is_left_on_disk_when_the_samples_cannot_be_converted(tmp_path: Path) -> None:
    with pytest.raises(EchoActError):
        wav.write_segment(tmp_path / "s.wav", np.array([np.nan], dtype=np.float32), RATE)

    assert not (tmp_path / "s.wav").exists()
    assert list(tmp_path.glob("*.part")) == []


# --------------------------------------------------------------------- read


def test_reading_a_stereo_wav_is_refused_rather_than_downmixed(tmp_path: Path) -> None:
    foreign = tmp_path / "stereo.wav"
    with wave.open(str(foreign), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(bytes(400))

    assert wav.probe(foreign).format == "wav_pcm_s16le_stereo"
    with pytest.raises(EchoActError) as caught:
        wav.read_wav(foreign)
    assert caught.value.code is Code.FILE_CORRUPT


def test_reading_an_8_bit_wav_is_refused(tmp_path: Path) -> None:
    foreign = tmp_path / "eight.wav"
    with wave.open(str(foreign), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(RATE)
        writer.writeframes(bytes(400))

    with pytest.raises(EchoActError) as caught:
        wav.read_wav(foreign)
    assert caught.value.code is Code.FILE_CORRUPT


def test_reading_something_that_is_not_a_wav_at_all_is_refused(tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_bytes(b"this is not audio")

    with pytest.raises(EchoActError) as caught:
        wav.probe(junk)
    assert caught.value.code is Code.FILE_CORRUPT


def test_reading_a_missing_file_says_so_and_names_no_home_directory(tmp_path: Path) -> None:
    with pytest.raises(EchoActError) as caught:
        wav.read_wav(tmp_path / "gone.wav")

    assert caught.value.code is Code.FILE_NOT_FOUND
    assert str(tmp_path) not in str(caught.value.detail)


def test_a_truncated_file_is_reported_rather_than_returned_short(tmp_path: Path) -> None:
    path = tmp_path / "s.wav"
    write_tone(path, 500)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) - 200])

    with pytest.raises(EchoActError) as caught:
        wav.read_wav(path)
    assert caught.value.code is Code.FILE_CORRUPT


def test_blocks_stream_the_same_samples_the_whole_read_returns(tmp_path: Path) -> None:
    path = tmp_path / "s.wav"
    write_tone(path, 5_000)

    blocks = list(wav.iter_blocks(path, block_frames=997))
    whole, _ = wav.read_wav(path)

    assert [len(b) for b in blocks][:2] == [997, 997]
    assert np.array_equal(np.concatenate(blocks), whole)


def test_streaming_a_truncated_file_reports_it_instead_of_ending_early(tmp_path: Path) -> None:
    """N-21 sends every large read here, so it must fail as loudly as read_wav.

    A retained result can be short for reasons this app never sees -- a disk
    that filled during an earlier write, a half-restored backup, a sync
    client -- and F-55 forbids streaming one back as the finished result
    while ``probe`` keeps reporting the header's length.
    """
    path = tmp_path / "s.wav"
    write_tone(path, 5_000)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) - 4_000])

    assert wav.probe(path).frame_count == 5_000, "the header still over-declares"
    with pytest.raises(EchoActError) as caught:
        list(wav.iter_blocks(path, block_frames=997))
    assert caught.value.code is Code.FILE_CORRUPT
    with pytest.raises(EchoActError):
        wav.read_wav(path)  # the two paths agree about the same file


def test_a_reader_that_wants_only_a_prefix_is_not_told_the_file_is_damaged(
    tmp_path: Path,
) -> None:
    """Only an exhausted stream claims the whole file; a prefix asked for less."""
    path = tmp_path / "s.wav"
    write_tone(path, 5_000)

    blocks = wav.iter_blocks(path, block_frames=997)
    assert len(next(blocks)) == 997
    blocks.close()


def test_streaming_a_foreign_file_fails_when_the_stream_is_asked_for(tmp_path: Path) -> None:
    foreign = tmp_path / "stereo.wav"
    with wave.open(str(foreign), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(bytes(400))

    with pytest.raises(EchoActError) as caught:
        wav.iter_blocks(foreign)  # not iterated: the check must not be lazy
    assert caught.value.code is Code.FILE_CORRUPT


# -------------------------------------------------------------- concatenate


def test_the_joined_pcm_is_byte_identical_to_its_segments_and_gaps(tmp_path: Path) -> None:
    first, second, third = (tmp_path / f"{i}.wav" for i in range(3))
    write_tone(first, 1_000, freq=180.0)
    write_tone(second, 1_500, freq=300.0)
    write_tone(third, 700, freq=440.0)
    gaps = [250, 400, 0]

    report = wav.concatenate([first, second, third], gaps, tmp_path / "full.wav", RATE)

    silence = [bytes(wav.frames_for_ms(g, RATE) * 2) for g in gaps]
    expected = (
        data_chunk(first)
        + silence[0]
        + data_chunk(second)
        + silence[1]
        + data_chunk(third)
        + silence[2]
    )
    assert data_chunk(tmp_path / "full.wav") == expected
    assert report.frame_count == len(expected) // 2
    assert report.segment_frames == (1_000, 1_500, 700)
    assert report.gap_frames == (11_025, 17_640, 0)


def test_the_join_keeps_one_format_for_the_whole_job(tmp_path: Path) -> None:
    one, two = tmp_path / "1.wav", tmp_path / "2.wav"
    write_tone(one, 100)
    write_tone(two, 100)

    wav.concatenate([one, two], [0, 0], tmp_path / "full.wav", RATE)

    full = wav.probe(tmp_path / "full.wav")
    assert full.is_output_format
    assert full.sample_rate == RATE
    assert full.byte_size == wav.CANONICAL_HEADER_BYTES + 200 * 2


def test_the_gap_belongs_to_the_segment_before_it(tmp_path: Path) -> None:
    one, two = tmp_path / "1.wav", tmp_path / "2.wav"
    write_tone(one, RATE)  # exactly one second
    write_tone(two, RATE // 2)

    report = wav.concatenate([one, two], [250, 0], tmp_path / "full.wav", RATE)

    # F-27: the pause is inside the preceding segment's range, and the ranges
    # tile the audio without a hole.
    assert report.spans[0].start_ms == 0
    assert report.spans[0].end_ms == 1250
    assert report.spans[1].start_ms == 1250
    assert report.spans[1].end_ms == report.duration_ms


def test_mixing_sample_rates_is_refused_instead_of_resampled(tmp_path: Path) -> None:
    native, foreign = tmp_path / "1.wav", tmp_path / "2.wav"
    write_tone(native, 100)
    wav.write_segment(foreign, tone(100), 22_050)

    with pytest.raises(EchoActError) as caught:
        wav.concatenate([native, foreign], [0, 0], tmp_path / "full.wav", RATE)

    assert caught.value.code is Code.INTERNAL
    assert caught.value.detail["found"] == 22_050
    assert not (tmp_path / "full.wav").exists()


def test_a_damaged_segment_leaves_no_half_written_output(tmp_path: Path) -> None:
    good, bad = tmp_path / "1.wav", tmp_path / "2.wav"
    write_tone(good, 100)
    bad.write_bytes(b"RIFF----WAVEjunk")

    with pytest.raises(EchoActError):
        wav.concatenate([good, bad], [0, 0], tmp_path / "full.wav", RATE)

    assert not (tmp_path / "full.wav").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_a_segment_shorter_than_its_header_leaves_no_joined_file(tmp_path: Path) -> None:
    """The size check has to run on the ``.part``, not on the caller's file.

    Run after the rename, it reports a failure the caller can only pass on
    while a complete-looking, playable, short WAV sits at the destination --
    nothing about that file says it is short, which is exactly what F-55
    forbids and what the rename discipline exists to prevent.
    """
    good, short = tmp_path / "1.wav", tmp_path / "2.wav"
    write_tone(good, 1_000)
    write_tone(short, 1_000)
    raw = short.read_bytes()
    short.write_bytes(raw[: len(raw) - 1_000])  # header still declares 1,000 frames

    with pytest.raises(EchoActError) as caught:
        wav.concatenate([good, short], [0, 0], tmp_path / "full.wav", RATE)

    assert caught.value.code is Code.FILE_CORRUPT
    assert not (tmp_path / "full.wav").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_every_segment_needs_its_own_gap(tmp_path: Path) -> None:
    one = tmp_path / "1.wav"
    write_tone(one, 10)

    with pytest.raises(EchoActError) as caught:
        wav.concatenate([one, one], [0], tmp_path / "full.wav", RATE)
    assert caught.value.code is Code.INTERNAL


def test_joining_nothing_reports_that_no_segment_is_ready(tmp_path: Path) -> None:
    with pytest.raises(EchoActError) as caught:
        wav.concatenate([], [], tmp_path / "full.wav", RATE)

    assert caught.value.code is Code.SEGMENT_NOT_READY
    assert caught.value.retryable


def test_a_segment_that_produced_no_audio_still_contributes_its_pause(tmp_path: Path) -> None:
    spoken = tmp_path / "1.wav"
    write_tone(spoken, 100)

    report = wav.concatenate([spoken, None], [0, 200], tmp_path / "full.wav", RATE)

    assert report.segment_frames == (100, 0)
    assert report.frame_count == 100 + wav.frames_for_ms(200, RATE)
    assert report.spans[1].start_ms == report.spans[0].end_ms


def test_joining_never_reads_more_than_one_block_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-21: a two-hour job is ~700 MB, so the join must not hold the file."""
    monkeypatch.setattr(wav, "BLOCK_FRAMES", 1_000)
    one, two = tmp_path / "1.wav", tmp_path / "2.wav"
    write_tone(one, 4_500)
    write_tone(two, 3_200)

    asked: list[int] = []
    original = wave.Wave_read.readframes

    def spy(self: wave.Wave_read, n: int) -> bytes:
        asked.append(n)
        return original(self, n)

    monkeypatch.setattr(wave.Wave_read, "readframes", spy)
    report = wav.concatenate([one, two], [500, 0], tmp_path / "full.wav", RATE)

    assert asked, "the join did not read anything"
    assert max(asked) == 1_000
    assert len(asked) >= 8
    assert report.frame_count == 4_500 + 3_200 + wav.frames_for_ms(500, RATE)


def test_audio_too_large_for_a_32_bit_riff_header_is_refused_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ceiling is ~4 GiB; shrink it rather than write one."""
    one = tmp_path / "1.wav"
    write_tone(one, 1_000)
    monkeypatch.setattr(wav, "MAX_PCM_BYTES", 1_000)

    with pytest.raises(EchoActError) as caught:
        wav.concatenate([one], [0], tmp_path / "full.wav", RATE)

    assert caught.value.code is Code.INTERNAL
    assert not (tmp_path / "full.wav").exists()


def test_the_longest_job_this_app_accepts_fits_a_32_bit_riff_header() -> None:
    """Assert the headroom rather than assume it.

    The worst case F-03 allows is 50,000 code points of Korean -- the slower
    of the two languages by F-81's own estimate -- read at F-07's slowest
    tempo, so the audio is as long as this product can make it.
    """
    seconds = policy.MAX_INPUT_CODEPOINTS / policy.ESTIMATE_CODEPOINTS_PER_SECOND_KO
    seconds /= policy.TEMPO_MIN
    # Plus the largest per-segment pause F-08 defines, on the most segments
    # F-81 can produce from that text.
    segments = policy.MAX_INPUT_CODEPOINTS / policy.SEGMENT_MIN_CODEPOINTS
    pause_ms = max(policy.STYLE_SEGMENT_GAP_MS.values()) + policy.PARAGRAPH_EXTRA_GAP_MS
    seconds += segments * pause_ms / 1000

    frames = int(seconds * RATE)
    assert frames < wav.MAX_FRAMES
    assert frames * wav.SAMPLE_WIDTH_BYTES < wav.MAX_PCM_BYTES // 2, "less than half the ceiling"
    assert wav.max_duration_ms(RATE) > 13 * 60 * 60 * 1000


# ------------------------------------------------------------ probe, digest


def test_probe_reports_what_the_result_entity_records(tmp_path: Path) -> None:
    path = tmp_path / "s.wav"
    write_tone(path, RATE * 2)

    info = wav.probe(path)

    assert info.sample_rate == RATE
    assert info.channels == 1
    assert info.sample_width_bits == 16
    assert info.frame_count == RATE * 2
    assert info.byte_size == path.stat().st_size
    assert info.duration_ms == 2_000


def test_the_digest_covers_the_whole_file(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "s.wav"
    write_tone(path, 3_000)

    assert wav.digest(path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_two_files_with_the_same_audio_share_a_digest(tmp_path: Path) -> None:
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    samples = tone(1_234)
    wav.write_segment(first, samples, RATE)
    wav.write_segment(second, samples, RATE)

    assert wav.digest(first) == wav.digest(second)


def test_digesting_a_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(EchoActError) as caught:
        wav.digest(tmp_path / "gone.wav")
    assert caught.value.code is Code.FILE_NOT_FOUND


# ------------------------------------------------------- partial export F-16


def make_job(tmp_path: Path, count: int, *, ready: int, gap_ms: int = 250) -> list[Segment]:
    segments: list[Segment] = []
    cursor = 0
    for i in range(count):
        path = tmp_path / f"seg{i}.wav"
        if i < ready:
            write_tone(path, 1_000 + i * 100, freq=200.0 + 40 * i)
        segment = ready_segment(i, path, (cursor, cursor + 30), gap_ms)
        segment.ready = i < ready
        segments.append(segment)
        cursor += 30
    return segments


def test_a_partial_export_holds_exactly_the_segments_ready_so_far(tmp_path: Path) -> None:
    segments = make_job(tmp_path, 5, ready=3)

    export = wav.export_partial(
        segments, tmp_path / "partial.wav", sample_rate=RATE, total_codepoints=150
    )

    assert export.segment_count == 3
    assert export.total_segments == 5
    assert not export.complete
    expected = sum(1_000 + i * 100 for i in range(3)) + 3 * wav.frames_for_ms(250, RATE)
    assert export.frame_count == expected


def test_a_partial_export_says_how_much_of_the_source_it_covers(tmp_path: Path) -> None:
    segments = make_job(tmp_path, 5, ready=2)

    export = wav.export_partial(
        segments, tmp_path / "partial.wav", sample_rate=RATE, total_codepoints=150
    )

    assert (export.covered_codepoints, export.total_codepoints) == (60, 150)
    assert export.coverage == pytest.approx(0.4)


def test_a_finished_job_exports_as_complete(tmp_path: Path) -> None:
    segments = make_job(tmp_path, 3, ready=3)

    export = wav.export_partial(
        segments, tmp_path / "full.wav", sample_rate=RATE, total_codepoints=90
    )

    assert export.complete
    assert export.coverage == 1.0
    assert export.segment_count == export.total_segments


def test_exporting_again_later_gives_a_longer_file_with_the_same_prefix(tmp_path: Path) -> None:
    """F-16: the second export is a longer file, never a continuation."""
    segments = make_job(tmp_path, 5, ready=2)
    early = wav.export_partial(
        segments, tmp_path / "early.wav", sample_rate=RATE, total_codepoints=150
    )

    for i in (2, 3):
        write_tone(tmp_path / f"seg{i}.wav", 1_000 + i * 100, freq=200.0 + 40 * i)
        segments[i].ready = True
    later = wav.export_partial(
        segments, tmp_path / "later.wav", sample_rate=RATE, total_codepoints=150
    )

    early_pcm = data_chunk(tmp_path / "early.wav")
    later_pcm = data_chunk(tmp_path / "later.wav")
    assert later_pcm.startswith(early_pcm)
    assert later.frame_count > early.frame_count
    assert later.spans[: len(early.spans)] == early.spans


def test_a_hole_in_the_middle_stops_the_export_at_the_hole(tmp_path: Path) -> None:
    """A segment finished out of order must not be pulled forward.

    Skipping the hole would drop text from the middle of the file and shift
    every later time range away from the job's own table.
    """
    segments = make_job(tmp_path, 5, ready=2)
    write_tone(tmp_path / "seg4.wav", 1_400)
    segments[4].ready = True

    export = wav.export_partial(
        segments, tmp_path / "partial.wav", sample_rate=RATE, total_codepoints=150
    )

    assert export.segment_count == 2
    assert export.covered_codepoints == 60


def test_exporting_before_anything_is_ready_is_refused(tmp_path: Path) -> None:
    segments = make_job(tmp_path, 3, ready=0)

    with pytest.raises(EchoActError) as caught:
        wav.export_partial(
            segments, tmp_path / "partial.wav", sample_rate=RATE, total_codepoints=90
        )

    assert caught.value.code is Code.SEGMENT_NOT_READY
    assert not (tmp_path / "partial.wav").exists()


def test_a_truncated_ready_segment_leaves_nothing_at_the_export_path(tmp_path: Path) -> None:
    """F-16's export is a file the user chose; a short one must never appear there."""
    segments = make_job(tmp_path, 3, ready=2)
    damaged = tmp_path / "seg1.wav"
    raw = damaged.read_bytes()
    damaged.write_bytes(raw[: len(raw) - 800])

    with pytest.raises(EchoActError) as caught:
        wav.export_partial(
            segments, tmp_path / "partial.wav", sample_rate=RATE, total_codepoints=90
        )

    assert caught.value.code is Code.FILE_CORRUPT
    assert not (tmp_path / "partial.wav").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_a_ready_segment_with_spoken_text_but_no_audio_is_a_hard_error(tmp_path: Path) -> None:
    segments = make_job(tmp_path, 2, ready=2)
    segments[1].audio_path = None

    with pytest.raises(EchoActError) as caught:
        wav.export_partial(
            segments, tmp_path / "partial.wav", sample_rate=RATE, total_codepoints=60
        )
    assert caught.value.code is Code.SEGMENT_NOT_READY


def test_an_unspoken_segment_contributes_only_its_silence(tmp_path: Path) -> None:
    """F-27 attaches emoji and whitespace runs to a neighbour, but a segment
    that yields no audio must still keep the time table aligned."""
    segments = make_job(tmp_path, 2, ready=2)
    segments[1].spoken_text = ""
    segments[1].audio_path = None

    export = wav.export_partial(
        segments, tmp_path / "partial.wav", sample_rate=RATE, total_codepoints=60
    )

    gap = wav.frames_for_ms(250, RATE)
    assert export.frame_count == 1_000 + gap + gap
    assert export.spans[1].start_ms == export.spans[0].end_ms


# ------------------------------------------------------- against the engine

_MODEL_ONNX = Path.home() / ".cache" / "supertonic3" / "onnx"


@pytest.mark.engine
@pytest.mark.skipif(not _MODEL_ONNX.is_dir(), reason="Supertonic 3 weights are not cached")
def test_real_engine_output_survives_the_writer_at_the_models_own_rate(tmp_path: Path) -> None:
    """F-82 against the shipped model rather than against synthetic arrays.

    The synthetic tests above choose their own dtype and shape; this one
    takes whatever ``synthesize`` actually returns -- A.5 records it as a
    float array that may carry a leading channel axis -- and checks that the
    file lands at the model's native rate with no resampling.
    """
    supertonic = pytest.importorskip("supertonic")

    tts = supertonic.TTS(
        model="supertonic-3",
        auto_download=False,
        intra_op_num_threads=2,
        inter_op_num_threads=1,
    )
    style = tts.get_voice_style("F1")
    first, _ = tts.synthesize("에코액트입니다.", style, lang="ko", speed=1.0, total_steps=4)
    second, _ = tts.synthesize("두 번째 문장입니다.", style, lang="ko", speed=1.0, total_steps=4)

    one, two = tmp_path / "e1.wav", tmp_path / "e2.wav"
    report_one = wav.write_segment(one, first, RATE)
    wav.write_segment(two, second, RATE)
    joined = wav.concatenate([one, two], [250, 0], tmp_path / "full.wav", RATE)

    assert report_one.frame_count == first.shape[-1]
    assert report_one.peak <= 1.0, "the engine overshot full scale and was clipped"
    assert wav.probe(tmp_path / "full.wav").sample_rate == RATE
    assert data_chunk(tmp_path / "full.wav") == (
        data_chunk(one) + bytes(wav.frames_for_ms(250, RATE) * 2) + data_chunk(two)
    )
    assert joined.frame_count == first.shape[-1] + second.shape[-1] + wav.frames_for_ms(250, RATE)

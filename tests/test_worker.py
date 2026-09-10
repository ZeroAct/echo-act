"""The synthesis child process: protocol behaviour, and one real synthesis.

Almost everything here runs against a stand-in for ``supertonic.TTS``.  That
is not a convenience: the worker's contract is a message loop, a file, and a
frame count, and a fake engine lets a test assert on the arguments the
requirements fix (F-87's thread counts, the chunking that makes one segment
one engine call) which a real run would only hide.  The single
``@pytest.mark.engine`` test at the end covers what a fake cannot: that the
real pipeline loads on CPU alone and that the frames reported are the frames
on disk.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import supertonic

from echoact import paths
from echoact.engine import protocol as proto
from echoact.engine import worker
from echoact.errors import Code
from echoact.policy import ENGINE_MAX_CHUNK_CODEPOINTS, ENGINE_SILENCE_DURATION_S

KOREAN = "에코액트는 문서를 소리내어 읽어 줍니다."
MODEL_DIR = Path.home() / ".cache" / "supertonic3"
HAVE_WEIGHTS = (MODEL_DIR / "onnx" / "vocoder.onnx").exists()


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """No test may touch the real user data directory; ``redact`` reads it."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    yield tmp_path / "data"
    paths.data_dir.cache_clear()


# ----------------------------------------------------------------------
# A stand-in for the engine
# ----------------------------------------------------------------------


class FakeSession:
    def __init__(self, providers: tuple[str, ...]) -> None:
        self._providers = list(providers)

    def get_providers(self) -> list[str]:
        return list(self._providers)


class FakeModel:
    def __init__(self, sample_rate: int, providers: tuple[str, ...], omit: tuple[str, ...]) -> None:
        self.sample_rate = sample_rate
        for name in ("dp_ort", "text_enc_ort", "vector_est_ort", "vocoder_ort"):
            if name not in omit:
                setattr(self, name, FakeSession(providers))


class Recorder:
    """What the worker asked the engine to do."""

    def __init__(self) -> None:
        self.init_kwargs: dict[str, Any] = {}
        self.synth_calls: list[dict[str, Any]] = []
        self.style_requests: list[str] = []


class FakeTTS:
    def __init__(self, rec: Recorder, spec: dict[str, Any]) -> None:
        self._rec = rec
        self._spec = spec
        self.sample_rate = spec["sample_rate"]
        self.voice_style_names = list(spec["voices"])
        self.model = FakeModel(spec["sample_rate"], spec["providers"], spec["omit_sessions"])

    def get_voice_style(self, voice_name: str) -> Any:
        self._rec.style_requests.append(voice_name)
        if voice_name not in self.voice_style_names:
            raise FileNotFoundError(f"Voice style '{voice_name}' not found")
        return {"voice": voice_name}

    def synthesize(self, text: str, voice_style: Any, **kw: Any):
        self._rec.synth_calls.append({"text": text, "style": voice_style, **kw})
        errors = self._spec["synth_errors"]
        if errors:
            failure = errors.pop(0)
            if failure is not None:
                raise failure
        n = self._spec["frames"]
        count = int(n(text) if callable(n) else n)
        wav = np.full((1, count), self._spec["amplitude"], dtype=np.float32)
        return wav, np.array([count / self.sample_rate], dtype=np.float32)


@pytest.fixture
def engine(monkeypatch) -> Recorder:
    """Install the stand-in; tests tune it through ``spec``."""
    rec = Recorder()
    spec: dict[str, Any] = {
        "sample_rate": 44100,
        "voices": ("F1", "F2", "M1"),
        "providers": ("CPUExecutionProvider",),
        "omit_sessions": (),
        "frames": 4410,
        "amplitude": 0.5,
        "synth_errors": [],
        "load_error": None,
    }

    def factory(**kwargs: Any) -> FakeTTS:
        rec.init_kwargs = kwargs
        if spec["load_error"] is not None:
            raise spec["load_error"]
        return FakeTTS(rec, spec)

    monkeypatch.setattr(supertonic, "TTS", factory)
    rec.spec = spec  # type: ignore[attr-defined]
    return rec


# ----------------------------------------------------------------------
# Driving the loop
# ----------------------------------------------------------------------


def drive(*requests: Any) -> list[Any]:
    """Feed requests through one worker run and decode what it wrote."""
    lines = "".join(r if isinstance(r, str) else proto.encode(r) for r in requests)
    out = io.StringIO()
    assert worker.serve(io.StringIO(lines), out) == 0
    return [proto.decode(line) for line in out.getvalue().splitlines() if line.strip()]


def load_request(seq: int = 1, **kw: Any) -> proto.Load:
    fields: dict[str, Any] = {
        "model_id": "supertonic-3",
        "model_dir": str(MODEL_DIR),
        "intra_op_threads": 2,
        "inter_op_threads": 1,
        "allowed_providers": ["CPUExecutionProvider"],
        "seq": seq,
    }
    fields.update(kw)
    return proto.Load(**fields)


def synth_request(out_path: Path, seq: int = 2, **kw: Any) -> proto.Synthesize:
    fields: dict[str, Any] = {
        "job_id": "job_abc",
        "segment_index": 0,
        "text": KOREAN,
        "lang": "ko",
        "voice_id": "F1",
        "speed": 1.0,
        "total_steps": 8,
        "out_path": str(out_path),
        "seq": seq,
    }
    fields.update(kw)
    return proto.Synthesize(**fields)


def only(replies: list[Any], kind: type) -> Any:
    matches = [r for r in replies if isinstance(r, kind)]
    assert len(matches) == 1, f"expected one {kind.__name__}, got {replies}"
    return matches[0]


# ----------------------------------------------------------------------
# Start-up and shutdown
# ----------------------------------------------------------------------


def test_ready_is_announced_before_any_request_is_read(engine):
    replies = drive()
    assert isinstance(replies[0], proto.Ready)
    assert replies[0].pid > 0


def test_closed_stdin_ends_the_worker_so_it_cannot_outlive_its_parent(engine):
    out = io.StringIO()
    assert worker.serve(io.StringIO(""), out) == 0


def test_shutdown_stops_the_loop_and_later_requests_are_never_read(engine, tmp_path):
    replies = drive(
        load_request(),
        proto.Shutdown(seq=9),
        synth_request(tmp_path / "never.wav", seq=10),
    )
    assert not any(isinstance(r, proto.Audio) for r in replies)
    assert not (tmp_path / "never.wav").exists()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_an_interruption_is_never_swallowed_to_keep_the_loop_alive(engine, tmp_path, interruption):
    # N-22's five-second release is enforced by the parent terminating this
    # process.  A worker that caught the signal and carried on would defeat
    # the only mechanism that meets the deadline.
    engine.spec["synth_errors"] = [interruption()]
    lines = proto.encode(load_request()) + proto.encode(synth_request(tmp_path / "a.wav"))
    with pytest.raises(interruption):
        worker.serve(io.StringIO(lines), io.StringIO())


def test_a_line_that_is_not_a_protocol_message_is_an_error_not_a_guess(engine, tmp_path):
    replies = drive("{not json}\n", load_request())
    assert only(replies, proto.Error).code == Code.INTERNAL.value
    assert only(replies, proto.Loaded).model_id == "supertonic-3"


def test_a_reply_arriving_on_the_request_pipe_is_refused(engine):
    replies = drive(proto.Pong(rss_bytes=1, seq=4))
    error = only(replies, proto.Error)
    assert error.code == Code.INTERNAL.value
    assert error.fatal is False


# ----------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------


def test_load_reports_the_rate_voices_and_providers_the_pipeline_actually_has(engine):
    loaded = only(drive(load_request(seq=7)), proto.Loaded)
    assert loaded.sample_rate == 44100
    assert loaded.voices == ["F1", "F2", "M1"]
    assert loaded.providers == ["CPUExecutionProvider"]
    assert loaded.load_seconds >= 0.0
    assert loaded.seq == 7


def test_the_worker_never_downloads_and_uses_the_directory_the_parent_resolved(engine):
    drive(load_request(model_dir=r"C:\some\resolved\dir"))
    assert engine.init_kwargs["auto_download"] is False
    assert engine.init_kwargs["model_dir"] == r"C:\some\resolved\dir"
    assert engine.init_kwargs["model"] == "supertonic-3"


def test_thread_counts_come_from_the_budget_rather_than_the_runtimes_default(engine):
    drive(load_request(intra_op_threads=3, inter_op_threads=2))
    assert engine.init_kwargs["intra_op_num_threads"] == 3
    assert engine.init_kwargs["inter_op_num_threads"] == 2


def test_a_nonsensical_thread_count_becomes_one_not_unbounded(engine):
    drive(load_request(intra_op_threads=0, inter_op_threads=0))
    assert engine.init_kwargs["intra_op_num_threads"] == 1
    assert engine.init_kwargs["inter_op_num_threads"] == 1


def test_a_session_running_on_a_remote_provider_is_a_fatal_refusal(engine, tmp_path):
    engine.spec["providers"] = ("AzureExecutionProvider", "CPUExecutionProvider")
    replies = drive(load_request(), synth_request(tmp_path / "seg.wav"))
    error = replies[1]
    assert error.code == Code.RUNTIME_PROVIDER_REFUSED.value
    assert error.fatal is True
    # The refused model is not left resident: the next segment finds none.
    assert replies[2].code == Code.MODEL_NOT_READY.value
    assert not (tmp_path / "seg.wav").exists()


def test_a_parent_asking_for_more_than_local_cpu_is_refused_before_any_load(engine):
    replies = drive(load_request(allowed_providers=["CUDAExecutionProvider"]))
    error = only(replies, proto.Error)
    assert error.code == Code.RUNTIME_PROVIDER_REFUSED.value
    assert error.fatal is True
    assert engine.init_kwargs == {}


def test_a_pipeline_whose_sessions_cannot_be_found_is_refused_rather_than_trusted(engine):
    engine.spec["omit_sessions"] = ("vocoder_ort",)
    error = only(drive(load_request()), proto.Error)
    assert error.code == Code.RUNTIME_PROVIDER_REFUSED.value
    assert error.fatal is True


def test_missing_weights_are_reported_as_a_model_that_is_not_ready(engine):
    engine.spec["load_error"] = FileNotFoundError(r"C:\Users\someone\.cache\supertonic3\onnx")
    error = only(drive(load_request()), proto.Error)
    assert error.code == Code.MODEL_NOT_READY.value
    assert error.fatal is True
    assert "someone" not in error.message  # N-20: no home path in a message


def test_an_unreadable_model_file_is_reported_as_corrupt(engine):
    engine.spec["load_error"] = ValueError("Model configuration file is malformed")
    error = only(drive(load_request()), proto.Error)
    assert error.code == Code.MODEL_CORRUPT.value
    assert error.fatal is True


def test_a_model_the_engine_has_never_heard_of_is_not_reported_as_a_damaged_one(engine):
    engine.spec["load_error"] = ValueError("Invalid model: 'supertonic-9'")
    error = only(drive(load_request(model_id="supertonic-9")), proto.Error)
    assert error.code == Code.MODEL_UNKNOWN.value
    assert error.fatal is True


def test_loading_twice_releases_the_first_model_first(engine):
    replies = drive(load_request(seq=1), load_request(seq=2))
    assert [r.seq for r in replies if isinstance(r, proto.Loaded)] == [1, 2]


# ----------------------------------------------------------------------
# Synthesize
# ----------------------------------------------------------------------


def test_a_segment_is_written_as_mono_16_bit_pcm_at_the_models_rate(engine, tmp_path):
    out = tmp_path / "seg0.wav"
    replies = drive(load_request(), synth_request(out))
    audio = only(replies, proto.Audio)
    with wave.open(str(out), "rb") as f:
        assert f.getnchannels() == 1
        assert f.getsampwidth() == 2
        assert f.getframerate() == 44100
        assert f.getnframes() == audio.frame_count
    assert audio.frame_count == 4410
    assert audio.sample_rate == 44100
    assert audio.out_path == str(out)
    assert audio.job_id == "job_abc"
    assert audio.peak == pytest.approx(0.5)


def test_the_frame_count_is_read_back_from_the_file_that_was_written(engine, tmp_path):
    engine.spec["frames"] = lambda text: 3 * len(text)
    out = tmp_path / "seg0.wav"
    audio = only(drive(load_request(), synth_request(out)), proto.Audio)
    assert audio.frame_count == 3 * len(KOREAN)
    assert out.stat().st_size == 44 + audio.frame_count * 2


def test_one_segment_is_exactly_one_engine_call(engine, tmp_path):
    # A.5: the engine re-chunks Korean at 120 characters unless told not to,
    # and its own silence is inert once we chunk first (F-82).
    drive(load_request(), synth_request(tmp_path / "seg0.wav"))
    call = engine.synth_calls[0]
    assert call["max_chunk_length"] == ENGINE_MAX_CHUNK_CODEPOINTS
    assert call["silence_duration"] == ENGINE_SILENCE_DURATION_S == 0.0
    assert call["text"] == KOREAN
    assert call["lang"] == "ko"
    assert call["speed"] == 1.0
    assert call["total_steps"] == 8


def test_the_worker_never_inserts_silence_of_its_own(engine, tmp_path):
    engine.spec["frames"] = 1000
    out = tmp_path / "seg0.wav"
    audio = only(drive(load_request(), synth_request(out)), proto.Audio)
    assert audio.frame_count == 1000  # F-82 makes the gaps the parent's job


def test_the_worker_writes_only_the_file_the_parent_named(engine, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    drive(load_request(), synth_request(scratch / "seg0.wav"))
    assert [p.name for p in scratch.iterdir()] == ["seg0.wav"]


def test_a_clipping_waveform_is_reported_honestly_and_stored_in_range(engine, tmp_path):
    engine.spec["amplitude"] = 1.5
    out = tmp_path / "seg0.wav"
    audio = only(drive(load_request(), synth_request(out)), proto.Audio)
    assert audio.peak == pytest.approx(1.5)
    with wave.open(str(out), "rb") as f:
        samples = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2")
    assert samples.max() == 32767


def test_the_voice_style_is_loaded_once_per_voice_not_once_per_segment(engine, tmp_path):
    drive(
        load_request(),
        synth_request(tmp_path / "a.wav", seq=2, segment_index=0),
        synth_request(tmp_path / "b.wav", seq=3, segment_index=1),
        synth_request(tmp_path / "c.wav", seq=4, segment_index=2, voice_id="M1"),
    )
    assert engine.style_requests == ["F1", "M1"]


def test_an_unresolved_language_falls_back_to_the_engines_own_unknown_code(engine, tmp_path):
    drive(load_request(), synth_request(tmp_path / "seg0.wav", lang="auto"))
    assert engine.synth_calls[0]["lang"] == "na"
    assert worker.engine_lang("") == "na"
    assert worker.engine_lang("EN") == "en"


def test_a_failed_segment_is_one_non_fatal_error_and_the_next_one_still_runs(engine, tmp_path):
    engine.spec["synth_errors"] = [RuntimeError("onnx blew up")]
    replies = drive(
        load_request(),
        synth_request(tmp_path / "a.wav", seq=2, segment_index=0),
        synth_request(tmp_path / "b.wav", seq=3, segment_index=1),
    )
    error = only(replies, proto.Error)
    assert error.code == Code.GENERATION_FAILED.value
    assert error.fatal is False
    assert (error.job_id, error.segment_index, error.seq) == ("job_abc", 0, 2)
    assert not (tmp_path / "a.wav").exists()
    assert only(replies, proto.Audio).segment_index == 1


def test_a_failure_message_never_repeats_the_text_that_caused_it(engine, tmp_path):
    # The engine quotes the offending characters in its own message; N-20
    # says none of that may reach a log or a reply.
    engine.spec["synth_errors"] = [ValueError(f"Found unsupported character(s): {KOREAN}")]
    error = only(drive(load_request(), synth_request(tmp_path / "a.wav")), proto.Error)
    assert KOREAN not in error.message
    assert "ValueError" in error.message


def test_running_out_of_memory_mid_segment_is_fatal(engine, tmp_path):
    engine.spec["synth_errors"] = [MemoryError()]
    error = only(drive(load_request(), synth_request(tmp_path / "a.wav")), proto.Error)
    assert error.code == Code.OUT_OF_MEMORY.value
    assert error.fatal is True


def test_a_segment_that_cannot_be_written_names_no_directory_in_its_message(engine, tmp_path):
    missing = tmp_path / "gone" / "a.wav"
    error = only(drive(load_request(), synth_request(missing)), proto.Error)
    assert error.code == Code.GENERATION_FAILED.value
    assert error.fatal is False
    assert str(tmp_path) not in error.message


def test_synthesizing_before_loading_is_refused_without_killing_the_worker(engine, tmp_path):
    replies = drive(synth_request(tmp_path / "a.wav", seq=2), proto.Ping(seq=3))
    error = only(replies, proto.Error)
    assert error.code == Code.MODEL_NOT_READY.value
    assert error.fatal is False
    assert only(replies, proto.Pong).seq == 3


def test_a_segment_with_nothing_to_speak_never_reaches_the_engine(engine, tmp_path):
    replies = drive(load_request(), synth_request(tmp_path / "a.wav", text="   \n "))
    assert only(replies, proto.Error).code == Code.INPUT_EMPTY.value
    assert engine.synth_calls == []


def test_an_unknown_voice_is_the_parents_problem_not_a_fatal_one(engine, tmp_path):
    error = only(
        drive(load_request(), synth_request(tmp_path / "a.wav", voice_id="Z9")), proto.Error
    )
    assert error.code == Code.VOICE_UNKNOWN.value
    assert error.fatal is False


def test_every_reply_echoes_the_sequence_number_of_its_request(engine, tmp_path):
    replies = drive(
        load_request(seq=11),
        synth_request(tmp_path / "a.wav", seq=12),
        proto.Ping(seq=13),
    )
    assert only(replies, proto.Loaded).seq == 11
    assert only(replies, proto.Audio).seq == 12
    assert only(replies, proto.Pong).seq == 13


# ----------------------------------------------------------------------
# Resource reporting and release
# ----------------------------------------------------------------------


def test_ping_reports_resident_memory(engine):
    pong = only(drive(proto.Ping(seq=5)), proto.Pong)
    assert pong.rss_bytes > 0
    assert pong.seq == 5


def test_each_segment_is_followed_by_a_resource_report_so_the_parent_need_not_poll(
    engine, tmp_path
):
    stats = only(drive(load_request(), synth_request(tmp_path / "a.wav", seq=2)), proto.Stats)
    assert stats.rss_bytes > 0
    assert 0.0 <= stats.cpu_percent <= 100.0
    assert stats.seq == 2


def test_cpu_is_reported_against_total_capacity_the_way_the_budget_is_stated(
    engine, tmp_path, monkeypatch
):
    # psutil calls two busy cores 200%; 4.1 states CPU as a share of total
    # logical capacity, which is what F-20's budget is compared against.
    monkeypatch.setattr(worker.psutil, "cpu_count", lambda: 4)
    monkeypatch.setattr(worker.psutil.Process, "cpu_percent", lambda self, interval=None: 200.0)
    stats = only(drive(load_request(), synth_request(tmp_path / "a.wav", seq=2)), proto.Stats)
    assert stats.cpu_percent == 50.0


def test_unload_releases_the_model_without_ending_the_process(engine, tmp_path):
    replies = drive(
        load_request(seq=1),
        proto.Unload(seq=2),
        synth_request(tmp_path / "a.wav", seq=3),
        proto.Ping(seq=4),
    )
    assert only(replies, proto.Stats).seq == 2
    assert only(replies, proto.Error).code == Code.MODEL_NOT_READY.value
    assert only(replies, proto.Pong).seq == 4


def test_a_voice_style_is_reloaded_after_a_release(engine, tmp_path):
    drive(
        load_request(seq=1),
        synth_request(tmp_path / "a.wav", seq=2),
        proto.Unload(seq=3),
        load_request(seq=4),
        synth_request(tmp_path / "b.wav", seq=5),
    )
    assert engine.style_requests == ["F1", "F1"]


# ----------------------------------------------------------------------
# The real engine
# ----------------------------------------------------------------------

@pytest.mark.engine
@pytest.mark.skipif(not HAVE_WEIGHTS, reason=f"Supertonic 3 weights absent from {MODEL_DIR}")
def test_the_real_model_loads_on_cpu_alone_and_reports_the_frames_it_wrote(tmp_path):
    out = tmp_path / "segment.wav"
    replies = drive(
        load_request(seq=1),
        synth_request(out, seq=2, text=KOREAN, lang="ko", voice_id="F1"),
        proto.Shutdown(seq=3),
    )
    loaded = only(replies, proto.Loaded)
    assert loaded.providers == ["CPUExecutionProvider"]  # F-87, N-01
    assert loaded.sample_rate == 44100  # F-82
    assert set(loaded.voices) == {f"{g}{i}" for g in "FM" for i in range(1, 6)}

    audio = only(replies, proto.Audio)
    with wave.open(str(out), "rb") as f:
        assert (f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, 44100)
        assert f.getnframes() == audio.frame_count
    assert audio.frame_count / 44100 > 0.5  # the sentence is not silence
    assert 0.0 < audio.peak <= 1.0

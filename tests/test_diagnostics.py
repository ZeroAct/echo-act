"""What F-72 must contain, and the three things it must never contain.

The exclusion test is the one that matters: it plants a credential, the
user's home path, and a sentence of body text in the places the export
actually reads from -- the log file and the database -- and then asserts
that none of the three survives into the exported text.  Everything else
here exists to keep that test honest: an export with no content would pass
it trivially, so the same fixture also asserts the report says what it is
supposed to say.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from echoact import diagnostics, paths
from echoact.config.settings import Settings
from echoact.db.store import Store
from echoact.domain import (
    Budget,
    Capability,
    Gender,
    Job,
    JobKind,
    JobState,
    Language,
    RequestPath,
    RetentionMode,
    SpeakingStyle,
    VoiceSettings,
)
from echoact.engine.container import ContainerLimits, Enforcement, LimitBasis
from echoact.engine.supervisor import LoadedModel, WorkerUsage
from echoact.errors import Code, EchoActError, Problem
from echoact.models.catalog import MANIFEST, SUPERTONIC_3_ID
from echoact.models.registry import ModelRegistry
from echoact.security.credentials import CredentialStore

T0 = 1_800_000_000.0

# The three things F-72 excludes, as literal strings the test can search for.
SECRET = "eak_toolclient_3Qv8xKz1aBcDeFgHiJkLmNoPqRsTuVwX"
BODY = "이 문장은 사용자의 본문이며 진단 파일에 절대 나타나면 안 됩니다."
BODY_EN = "This sentence is the user's document body and must never be exported."


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Never touch the real user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    paths.ensure_tree()
    yield tmp_path
    paths.data_dir.cache_clear()


def _voice() -> VoiceSettings:
    return VoiceSettings(
        model_id=SUPERTONIC_3_ID,
        language=Language.KO,
        gender=Gender.FEMALE,
        voice_id=MANIFEST.get(SUPERTONIC_3_ID).voices_for(Gender.FEMALE)[0].voice_id,
        style=SpeakingStyle.NATURAL,
        tempo=1.0,
    )


BUDGET = Budget(cpu_percent=20, memory_bytes=4 << 30, intra_op_threads=2)


class FakeSupervisor:
    """Only the four members the export reads, so a test needs no worker."""

    def __init__(self, *, loaded: bool = True, usage: WorkerUsage | None = None) -> None:
        self.budget = BUDGET
        self.limits = ContainerLimits(
            memory=Enforcement.ENFORCED,
            cpu=Enforcement.ENFORCED,
            kill_on_close=Enforcement.ENFORCED,
            memory_basis=LimitBasis.COMMIT,
            memory_bytes=BUDGET.memory_bytes,
            cpu_percent=BUDGET.cpu_percent,
            facility="windows job object, hard CPU cap",
        )
        self.loaded_model = (
            LoadedModel(
                model_id=SUPERTONIC_3_ID,
                model_dir="<data>/models",
                sample_rate=44100,
                voices=("F1", "M1"),
                providers=("CPUExecutionProvider",),
                load_seconds=0.8,
            )
            if loaded
            else None
        )
        self._usage = usage or WorkerUsage(
            rss_bytes=1_200_000_000,
            cpu_percent=41.0,
            peak_rss_bytes=1_300_000_000,
            peak_commit_bytes=1_400_000_000,
            limits=self.limits,
            source="job object",
            age_s=0.4,
        )

    def usage(self) -> WorkerUsage | None:
        return self._usage

    def sample_usage(self) -> WorkerUsage | None:
        return self._usage


@dataclass
class FakeEngine:
    job: Job | None = None
    cancelled: list[str] = field(default_factory=list)

    def current(self) -> Job | None:
        return self.job

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)


@dataclass
class FakeStartup:
    problems: list[Problem] = field(default_factory=list)


class FakeApp:
    """The composition root's surface, as much of it as F-72 reads."""

    def __init__(self, tmp_path: Path, *, settings: Settings | None = None) -> None:
        self.settings = settings or Settings(voice=_voice())
        self.store = Store(tmp_path / "db.sqlite3", audio_root=tmp_path / "audio")
        self.registry = ModelRegistry(MANIFEST, root=tmp_path / "models")
        self.credentials = CredentialStore.load()
        self.supervisor = FakeSupervisor()
        self.engine = FakeEngine()
        self.startup = FakeStartup()
        self.service_running = False


@pytest.fixture()
def app(tmp_path):
    a = FakeApp(tmp_path)
    yield a
    a.store.close()


def _job(
    job_id: str,
    *,
    path: RequestPath = RequestPath.GUI,
    text: str = "hello",
    label: str | None = None,
) -> Job:
    return Job(
        job_id=job_id,
        kind=JobKind.SPEECH,
        request_path=path,
        owner_client_id="owner" if path is RequestPath.GUI else "cli_7",
        state=JobState.ACCEPTED,
        source_text=text,
        settings=_voice(),
        budget=BUDGET,
        retention=RetentionMode.ONE_OFF,
        created_at=T0,
        client_label=label,
    )


def _write_log(lines: list[str]) -> Path:
    path = paths.log_dir() / "echoact.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ F-72 ---


def test_the_export_names_the_app_the_os_the_model_the_service_and_the_budget(app):
    text = diagnostics.export_text(app)

    assert "EchoAct diagnostic report" in text
    assert "APPLICATION" in text
    assert "Python" in text
    assert SUPERTONIC_3_ID in text  # model, with its pinned revision
    assert MANIFEST.get(SUPERTONIC_3_ID).revision[:12] in text
    assert "SERVICE" in text
    assert "127.0.0.1:8765" in text
    assert "BUDGET APPLIED" in text
    assert "CPU 20%" in text
    assert "RECENT ERROR CODES" in text
    assert "LOG" in text


def test_a_planted_credential_home_path_and_body_sentence_are_all_absent(app):
    """The point of F-72, tested against the sources the export reads.

    All three are planted where they would really appear: the credential and
    a home path in the log, and body text both in the log (as a repr, the
    shape it would take if it ever reached a log line) and in the database,
    which is where the user's text genuinely lives.
    """
    home = str(Path.home())
    app.store.create_job(_job("job_leak", text=BODY))
    app.store.save_document(title="Notes", body=BODY_EN)
    _write_log(
        [
            f"2026-09-11T10:00:01 INFO  echoact.service authenticated token={SECRET}",
            f"2026-09-11T10:00:02 INFO  echoact.text opened {home}/Documents/secret-plan.txt",
            f"2026-09-11T10:00:03 DEBUG echoact.text normalised {BODY!r}",
            f"2026-09-11T10:00:04 DEBUG echoact.text normalised {BODY_EN!r}",
        ]
    )

    text = diagnostics.export_text(app)

    assert SECRET not in text
    assert home not in text
    assert BODY not in text
    assert BODY_EN not in text
    # ...and the export is not empty of everything, which would pass the
    # three assertions above for the wrong reason.
    assert "job_leak" in text
    assert "<redacted>" in text
    assert "<home>" in text or "<data>" in text


def test_the_job_snapshot_is_never_even_read(app):
    """Body text is excluded by not fetching it, not by filtering it.

    ``Store.list_jobs`` returns the snapshot only when asked; this asserts
    the export never asks, so no future change to the renderer can start
    printing something the collector holds.
    """
    app.store.create_job(_job("job_snap", text=BODY_EN))
    asked: list[bool] = []
    original = app.store.list_jobs

    def spy(**kw: Any):
        asked.append(bool(kw.get("include_source_text")))
        return original(**kw)

    app.store.list_jobs = spy  # type: ignore[method-assign]
    report = diagnostics.collect(app)

    assert asked == [False]
    assert all(getattr(trace, "source_text", None) is None for trace in report.jobs)


def test_the_host_name_is_left_out_of_the_os_line(app):
    """A personal computer's name is frequently the user's own name, and
    F-72 excludes the home path for exactly that reason."""
    import platform

    node = platform.node()
    text = diagnostics.export_text(app)
    if node and node.lower() not in ("localhost", ""):
        assert node not in text
    assert platform.system() in text


def test_no_network_code_is_present_in_the_module():
    """F-72: nothing is transmitted externally.  Asserted, not reviewed.

    An import scan rather than a runtime check, because the requirement is
    about what the module *can* do: a transmitting export that only fires on
    an unusual path would still be a transmitting export.
    """
    source = Path(diagnostics.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = {
        "socket",
        "ssl",
        "http",
        "httpx",
        "requests",
        "urllib",
        "urllib3",
        "ftplib",
        "smtplib",
        "telnetlib",
        "asyncio",
        "websockets",
        "webbrowser",
        "subprocess",
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    assert not (found & banned), sorted(found & banned)
    for token in ("urlopen", "sendall", "post(", "get(\"http"):
        assert token not in source


# ------------------------------------------------------------------ N-25 ---


def test_a_job_entry_carries_exactly_the_five_things_n25_names():
    fields = set(diagnostics.JobTrace.__dataclass_fields__)
    assert fields == {"job_id", "timestamp", "stage", "error_code", "budget"}


def test_a_failed_job_is_traceable_by_all_five(app):
    app.store.create_job(_job("job_bad", path=RequestPath.REST, label="Tool"))
    app.store.update_job_state("job_bad", JobState.PREPARING_MODEL, at=T0 + 1)
    app.store.record_job_error("job_bad", Code.OUT_OF_MEMORY, at=T0 + 2)

    report = diagnostics.collect(app)
    trace = next(t for t in report.jobs if t.job_id == "job_bad")

    assert trace.stage == JobState.FAILED.value
    assert trace.error_code == Code.OUT_OF_MEMORY.value
    assert trace.budget == BUDGET
    assert trace.timestamp == pytest.approx(T0 + 2)

    line = trace.line()
    assert "job_bad" in line
    assert Code.OUT_OF_MEMORY.value in line
    assert "CPU 20%" in line
    assert "2026" in line or "2027" in line  # a rendered wall-clock stamp


def test_recent_error_codes_come_from_jobs_start_up_and_the_log(app):
    app.store.create_job(_job("job_a"))
    app.store.record_job_error("job_a", Code.GENERATION_FAILED, at=T0 + 1)
    app.startup.problems.append(Problem(Code.SERVICE_PORT_UNAVAILABLE, "port busy"))
    _write_log(["2026-09-11T10:00:05 WARNING echoact.service bind failed SERVICE_PORT_UNAVAILABLE"])

    report = diagnostics.collect(app)
    counts = dict(report.error_codes)

    assert counts[Code.GENERATION_FAILED.value] == 1
    assert counts[Code.SERVICE_PORT_UNAVAILABLE.value] == 2  # start-up problem and log line
    assert "NOTAREALCODE" not in counts


# ------------------------------------------------------- collection rules ---


def test_a_section_that_cannot_be_read_becomes_a_note_not_an_exception(app):
    """A diagnostic that fails when something is broken is worth nothing."""

    def boom(*_a: Any, **_k: Any):
        raise RuntimeError("registry is wedged")

    app.registry.statuses = boom  # type: ignore[method-assign]
    report = diagnostics.collect(app)

    assert report.models == ()
    assert any("models" in note for note in report.notes)
    assert "NOTES" in report.to_text()


def test_the_log_tail_is_bounded_in_lines_and_in_bytes(app):
    _write_log([f"2026-09-11T10:00:00 INFO echoact.jobs line {i}" for i in range(5000)])

    report = diagnostics.collect(app, log_lines=50)

    assert len(report.log_lines) == 50
    assert "line 4999" in report.log_lines[-1]
    assert all(len(line) <= diagnostics.LOG_LINE_MAX_CHARS + 2 for line in report.log_lines)


def test_a_very_long_line_is_truncated_rather_than_carried(app):
    _write_log(["2026-09-11 INFO echoact.jobs " + "x" * 5000])
    report = diagnostics.collect(app)
    assert len(report.log_lines[0]) <= diagnostics.LOG_LINE_MAX_CHARS + 2


def test_the_running_job_is_reported_with_the_client_that_asked_for_it(app):
    app.engine.job = _job("job_live", path=RequestPath.REST, label="Claude Desktop")
    report = diagnostics.collect(app)

    assert report.current_job is not None
    assert report.current_job.job_id == "job_live"
    assert report.current_job_path == RequestPath.REST.value
    assert report.current_job_client == "Claude Desktop"
    assert "Claude Desktop" in report.to_text()


def test_the_three_budgets_are_reported_separately(app):
    """F-78 and N-03: configured, in force, and what is enforced."""
    app.engine.job = _job("job_live")
    report = diagnostics.collect(app)

    assert report.budget.in_force == BUDGET
    assert report.budget.running_job == BUDGET
    assert report.budget.configured is not None
    assert report.budget.memory_enforcement == Enforcement.ENFORCED.value
    assert report.budget.memory_basis == LimitBasis.COMMIT.value
    text = report.to_text()
    assert "windows job object" in text
    assert "commit basis" in text


def test_credentials_are_reported_without_anything_derived_from_the_token(app):
    issued = app.credentials.issue("Tool", frozenset({Capability.GENERATE}), now=T0)
    text = diagnostics.export_text(app)

    assert "Tool" in text
    assert issued.token not in text
    assert issued.ref not in text
    assert issued.client_id in text
    raw = json.dumps(app.credentials.export_public(now=T0))
    assert "verifier" not in raw  # the projection itself carries no key material


# ------------------------------------------------------------- the export ---


def test_save_writes_exactly_the_text_that_was_reviewed(tmp_path, app):
    text = diagnostics.export_text(app)
    target = tmp_path / "out" / diagnostics.default_filename(T0)

    written = diagnostics.save(text, target)

    assert written == target
    assert written.read_text(encoding="utf-8") == text
    assert written.name.startswith("echoact-diagnostics-")


def test_a_write_that_fails_leaves_as_an_echoact_error(tmp_path, app):
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises(EchoActError) as caught:
        diagnostics.save("x", directory)

    assert caught.value.code in (Code.FILE_PERMISSION, Code.INTERNAL)
    assert str(tmp_path) not in json.dumps(caught.value.detail)


def test_the_text_is_stable_enough_to_diff_between_two_exports(app):
    first = diagnostics.render(diagnostics.collect(app, now=T0))
    second = diagnostics.render(diagnostics.collect(app, now=T0))
    assert first == second


# ---------------------------------------------------------------- scrub ----


@pytest.mark.parametrize(
    "line",
    [
        "auth ok token=eak_abc12345_ZZZZZZZZZZZZZZZZ",
        "digest 0123456789abcdef0123456789abcdef01234567",
    ],
)
def test_credential_shaped_text_is_redacted_by_the_logger_s_own_filter(line):
    assert "<redacted>" in diagnostics.scrub(line)


def test_a_windows_user_profile_path_is_replaced_even_when_it_is_not_ours():
    line = r"loaded C:\Users\someone-else\Desktop\book.txt"
    out = diagnostics.scrub(line)
    assert "someone-else" not in out
    assert "<home>" in out


def test_a_posix_home_path_is_replaced():
    out = diagnostics.scrub("loaded /home/someone/book.txt")
    assert "someone" not in out
    assert "<home>" in out


def test_the_data_directory_is_labelled_rather_than_spelled_out():
    line = f"wrote {paths.audio_dir() / 'job_1.wav'}"
    out = diagnostics.scrub(line)
    assert str(paths.data_dir()) not in out
    assert "<data>" in out


def test_hangul_in_a_log_line_is_treated_as_content_even_when_unquoted():
    """Everything this app logs is ASCII English, so Hangul came from the user.

    The quoted-run rule alone would miss it: a Korean sentence carries far
    more meaning per character, and this one is comfortably under the
    forty-character threshold that catches English prose.
    """
    out = diagnostics.scrub("2026-09-11 INFO echoact.text normalised 오늘 회의는 취소되었습니다")
    assert "회의" not in out
    assert "<text omitted>" in out
    assert "echoact.text" in out  # the diagnostic part of the line survives


def test_an_ordinary_english_log_line_is_left_alone():
    """Scrubbing that ate the diagnostics would defeat N-25."""
    line = "2026-09-11T10:00:03 INFO    echoact.jobs job=job_7f2 stage=generating code=BUSY"
    assert diagnostics.scrub(line) == line


def test_usage_is_reported_beside_the_budget_it_is_measured_against(app):
    """N-21: the generation job's figures, not the whole app's."""
    text = diagnostics.export_text(app)
    assert "Generation job usage" in text
    assert "Measured by" in text
    assert "job object" in text
    assert "peak commit" in text

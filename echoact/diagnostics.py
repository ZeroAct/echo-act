"""F-72's diagnostic export: what to include, and what must never be in it.

The requirement names both halves.  Included: the app, OS, and model
versions, the service state, the budget actually applied, recent error
codes, and logs stripped of sensitive information.  Excluded, and this is
the point of the requirement: body text, audio, credentials, and the user's
home path.  Nothing is transmitted anywhere -- there is no network client in
this module, and a test asserts the absence rather than trusting the reading.

Exclusion is arranged so that it holds by construction wherever it can:

* Body text is never *fetched*.  ``Store.list_jobs`` leaves the snapshot out
  unless a caller asks for it, and nothing here asks.  A filter over text we
  had already loaded would be the weaker design, because it would have to
  recognise prose.
* Credentials never leave ``echoact.security`` in the first place: the store
  exports a public projection with no key material, and the log tail is run
  through the same :class:`~echoact.util.logging.SensitiveFilter` the logger
  itself uses, so a credential formatted into a message by accident is
  redacted here too.
* Paths are rendered through :func:`echoact.paths.redact`, and the log tail
  gets a second pass that rewrites the data root and any user-profile
  directory it finds inside a line of free text.

The report is returned as text so it can be shown to the user *before*
anything is written: F-72 makes review a precondition of export, not a
courtesy after it.  :func:`save` writes exactly the string it is given,
which is the string that was on screen.

Everything here is English.  ``echoact.ui.i18n`` is for widgets, and F-86 is
explicit that the display language is a presentation choice; a support file
that changes shape with it is harder to compare against another one.
"""

from __future__ import annotations

import errno
import logging
import os
import platform
import re
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .domain import Budget
from .errors import Code, EchoActError, Problem
from .paths import data_dir, log_dir, redact
from .policy import LIST_PAGE_DEFAULT, REST_HOST
from .util import ids
from .util.logging import SensitiveFilter, get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .app import Application

log = get_logger("diagnostics")

#: How much log to carry.  4.1 caps retention at 7 days and 100 MB, which is
#: far more than a support file should hold; these two bound what is read
#: into memory as well as what is written out (N-21).  They are here rather
#: than in ``echoact.policy`` only because nothing else needs them; if a
#: second caller appears they belong there.
LOG_TAIL_LINES = 200
LOG_LINE_MAX_CHARS = 300

#: A quoted run longer than this is treated as content and dropped.  The one
#: long string in this system is the user's document (see
#: ``util.logging.job_context``), so a long ``%r`` in a log line is the shape
#: body text would take if it ever reached one.
QUOTED_TEXT_MAX_CHARS = 40

#: A run of non-ASCII letters longer than this is treated as content too.
#: Everything this application writes to a log is ASCII English -- the error
#: catalogue is English by N-24, and every helper in ``util.logging`` takes
#: identifiers and lengths -- so Hangul in a log line came from the user, and
#: a Korean sentence is well under ``QUOTED_TEXT_MAX_CHARS`` characters.
NON_ASCII_RUN_MAX_CHARS = 3

#: How many recent jobs get an N-25 entry.  ``LIST_PAGE_DEFAULT`` is the
#: page size the rest of the app reads history in.
RECENT_JOB_LIMIT = LIST_PAGE_DEFAULT

_PACKAGES = (
    "PySide6",
    "onnxruntime",
    "supertonic",
    "numpy",
    "soundfile",
    "sounddevice",
    "fastapi",
    "uvicorn",
    "fastmcp",
    "psutil",
)

_SENSITIVE = SensitiveFilter()

# A user-profile directory in the middle of a line of text, for the case the
# path was never a Path object and so never passed through ``redact``.
_HOME_LIKE = re.compile(
    r"""(?ix)
    (?: [a-z]:[\\/]+users[\\/]+[^\\/\s"'<>|]+       # C:\Users\name
      | /(?:home|Users)/[^/\s"'<>|]+                 # /home/name, /Users/name
    )"""
)

_N = str(QUOTED_TEXT_MAX_CHARS)
_QUOTED = re.compile(
    r"'(?:[^'\\]|\\.){" + _N + r",}'" + r'|"(?:[^"\\]|\\.){' + _N + r',}"',
    re.DOTALL,
)

_NON_ASCII_RUN = re.compile(
    r"[^\x00-\x7f](?:[^\x00-\x7f]|[ ](?=[^\x00-\x7f])){" + str(NON_ASCII_RUN_MAX_CHARS) + r",}"
)

_CODE_TOKEN = re.compile(r"\b[A-Z][A-Z_]{3,}\b")


# ======================================================================
# Scrubbing
# ======================================================================


def scrub(line: str) -> str:
    """Make one line of log safe to hand to someone else.

    Four passes, in an order that matters.  The credential filter runs first
    because a token can contain characters the path rules would chew on; the
    data root is rewritten before the home directory because on Windows the
    first lives inside the second and the more specific label is the more
    useful one; the two content rules run last so they see whatever the path
    rules left behind.

    The content rules are heuristics and are described as such: a long
    quoted run and a run of non-ASCII letters are the two shapes body text
    takes when it reaches a log line.  They are a second line of defence.
    The first is N-20, which keeps body text out of a log in the first
    place -- an English sentence logged unquoted would defeat both rules,
    and nothing here could tell it from a message the app wrote itself.
    """
    text = _strip_credentials(line)
    text = _strip_paths(text)
    text = _QUOTED.sub("'<text omitted>'", text)
    text = _NON_ASCII_RUN.sub("<text omitted>", text)
    text = text.rstrip()
    if len(text) > LOG_LINE_MAX_CHARS:
        text = text[:LOG_LINE_MAX_CHARS] + " …"
    return text


def _strip_credentials(line: str) -> str:
    """Reuse the logger's own filter rather than a second copy of its rule.

    Two regexes for one hazard would be one regex too many: the day the
    logger learns a new credential shape, this must learn it at the same
    instant or the export becomes the leak N-20 closed.
    """
    record = logging.makeLogRecord({"msg": line, "args": ()})
    _SENSITIVE.filter(record)
    return str(record.msg)


def _strip_paths(line: str) -> str:
    text = line
    for root, label in _path_labels():
        text = _replace_path(text, root, label)
    return _HOME_LIKE.sub("<home>", text)


def _path_labels() -> tuple[tuple[str, str], ...]:
    """The roots worth naming, most specific first."""
    roots: list[tuple[str, str]] = []
    for getter, label in ((data_dir, "<data>"), (Path.home, "<home>")):
        try:
            roots.append((str(getter()), label))
        except (OSError, RuntimeError):  # a home directory need not exist
            continue
    return tuple(roots)


def _replace_path(text: str, root: str, label: str) -> str:
    """Replace ``root`` however it was spelled: either separator, either case.

    Windows paths reach a log in both slash directions -- ``pathlib`` writes
    backslashes, a URL or a POSIX-flavoured library writes forward ones --
    and comparing only one form would leave the other in the file.
    """
    if not root:
        return text
    variants = {root, root.replace("\\", "/"), root.replace("/", "\\")}
    for variant in sorted(variants, key=len, reverse=True):
        pattern = re.compile(re.escape(variant), re.IGNORECASE if os.name == "nt" else 0)
        text = pattern.sub(label, text)
    return text


# ======================================================================
# The report
# ======================================================================


@dataclass(frozen=True, slots=True)
class JobTrace:
    """N-25's traceable entry, and deliberately only that.

    Job id, timestamp, stage, error code, applied budget: the five the
    requirement names.  Not the text, not the result, not the voice -- a
    support file is read by someone who is not the user, and everything
    beyond these five would be extra exposure for no diagnostic gain.
    """

    job_id: str
    timestamp: float
    stage: str
    error_code: str | None
    budget: Budget | None

    def line(self) -> str:
        return (
            f"{self.job_id}  {_stamp(self.timestamp)}  {self.stage:<16}"
            f"  {self.error_code or '-':<26}  {_budget_text(self.budget)}"
        )


@dataclass(frozen=True, slots=True)
class ModelLine:
    """F-72's "model versions": the pinned revision, not just a name."""

    model_id: str
    display_name: str
    revision: str
    state: str
    bytes_present: int
    bytes_total: int
    runnable: bool
    unavailable_reason: str | None
    #: True when the files came from the ``supertonic`` package's own cache
    #: rather than this app's.  Worth reporting: F-65's delete does not
    #: touch that directory, so "ready" means something different there.
    using_package_cache: bool = False


@dataclass(frozen=True, slots=True)
class ClientLine:
    """One integration client, from the projection that has no key material.

    The ``ref`` is left out although the public projection carries it: it is
    the readable half of a token's own text, and a support file has no use
    for it that the client id does not already serve.
    """

    client_id: str
    name: str
    status: str
    capabilities: tuple[str, ...]
    expires_at: float | None
    last_access_at: float | None


@dataclass(frozen=True, slots=True)
class ServiceState:
    """F-72's "service state", including the F-79 case where it is off
    because the port would not bind rather than because the owner said so."""

    rest_enabled: bool
    rest_running: bool
    host: str
    port: int
    mcp_enabled: bool
    problems: tuple[str, ...]
    clients: tuple[ClientLine, ...]


@dataclass(frozen=True, slots=True)
class BudgetState:
    """The budget "actually applied", which is three different numbers.

    F-78 separates the configured value from the one a job is running under,
    and N-03 separates both from what the platform will actually enforce.
    Reporting one number would misstate whichever of the three the reader
    happened to need.
    """

    configured: Budget | None
    configured_error: str | None
    in_force: Budget | None
    running_job: Budget | None
    facility: str
    memory_enforcement: str
    cpu_enforcement: str
    memory_basis: str


@dataclass(frozen=True, slots=True)
class UsageLine:
    """F-22's figures for the generation job alone (N-21), with their source.

    ``source`` and ``peak_commit_bytes`` are carried because N-03 permits
    the figure on screen and the figure limits are tested against to differ;
    a report that printed one number would be claiming they do not.
    """

    rss_bytes: int
    cpu_percent: float
    peak_rss_bytes: int
    peak_commit_bytes: int
    source: str
    age_s: float


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """Everything F-72 asks for, as data first and text second.

    Data first because the exclusions are then testable field by field, and
    because the GUI shows the same object it would save.
    """

    generated_at: float
    app_version: str
    python_version: str
    os_description: str
    packages: tuple[tuple[str, str], ...]
    models: tuple[ModelLine, ...]
    service: ServiceState
    budget: BudgetState
    usage: UsageLine | None
    current_job: JobTrace | None
    current_job_path: str | None
    current_job_client: str | None
    jobs: tuple[JobTrace, ...]
    error_codes: tuple[tuple[str, int], ...]
    log_lines: tuple[str, ...]
    notes: tuple[str, ...]

    def to_text(self) -> str:
        return render(self)


# ======================================================================
# Collection
# ======================================================================


def collect(
    app: Application,
    *,
    now: float | None = None,
    job_limit: int = RECENT_JOB_LIMIT,
    log_lines: int = LOG_TAIL_LINES,
) -> DiagnosticReport:
    """Gather the report.  Never raises for a section it cannot read.

    A diagnostic that fails when something is broken is worth nothing: the
    moment a section is unreadable is the moment its absence is itself
    evidence.  Each section is therefore guarded, and what went wrong is
    recorded in ``notes`` rather than propagated.
    """
    at = ids.now() if now is None else now
    notes: list[str] = []

    models = _collect_models(app, notes)
    service = _collect_service(app, at, notes)
    budget = collect_budget(app, notes)
    usage = _collect_usage(app, notes)
    current, current_path, current_client = _collect_current(app, notes)
    jobs = _collect_jobs(app, job_limit, notes)
    lines = _collect_log(log_lines, notes)
    problems = _startup_problem_codes(app)
    codes = _tally_codes(jobs, current, problems, lines)

    return DiagnosticReport(
        generated_at=at,
        app_version=_app_version(),
        python_version=f"{platform.python_version()} ({platform.python_implementation()})",
        os_description=_os_description(),
        packages=_package_versions(),
        models=models,
        service=service,
        budget=budget,
        usage=usage,
        current_job=current,
        current_job_path=current_path,
        current_job_client=current_client,
        jobs=jobs,
        error_codes=codes,
        log_lines=lines,
        notes=tuple(notes),
    )


def _app_version() -> str:
    from . import __version__

    return __version__


def _os_description() -> str:
    """System, release, build, and architecture -- and no host name.

    ``platform.uname()`` would be the shorter call and would carry the
    machine's name, which on a personal computer is frequently the user's
    own.  F-72 excludes the home path for that reason and the host name
    fails the same test.
    """
    return f"{platform.system()} {platform.release()} ({platform.version()}) {platform.machine()}"


def _package_versions() -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for name in _PACKAGES:
        try:
            out.append((name, metadata.version(name)))
        except metadata.PackageNotFoundError:
            continue
    return tuple(out)


def _collect_models(app: Application, notes: list[str]) -> tuple[ModelLine, ...]:
    try:
        statuses = app.registry.statuses(_configured_budget(app)[0])
    except Exception as exc:  # noqa: BLE001 - a report must survive a broken section
        notes.append(f"models: unavailable ({type(exc).__name__})")
        return ()
    out: list[ModelLine] = []
    for status in statuses:
        try:
            revision = app.registry.entry(status.model_id).revision
        except Exception:  # noqa: BLE001
            revision = "unknown"
        out.append(
            ModelLine(
                model_id=status.model_id,
                display_name=status.display_name,
                revision=revision,
                state=str(status.state),
                bytes_present=status.bytes_present,
                bytes_total=status.bytes_total,
                runnable=status.runnable,
                unavailable_reason=status.unavailable_reason,
                using_package_cache=bool(getattr(status, "using_package_cache", False)),
            )
        )
    return tuple(out)


def _collect_service(app: Application, at: float, notes: list[str]) -> ServiceState:
    settings = app.settings
    clients: tuple[ClientLine, ...] = ()
    try:
        clients = tuple(
            ClientLine(
                client_id=str(row["client_id"]),
                name=str(row["name"]),
                status=str(row["status"]),
                capabilities=tuple(str(c) for c in row["effective_capabilities"]),
                expires_at=_as_time(row.get("expires_at")),
                last_access_at=_as_time(row.get("last_access_at")),
            )
            for row in app.credentials.export_public(now=at)
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"clients: unavailable ({type(exc).__name__})")
    running = False
    try:
        running = bool(app.service_running)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"service state: unavailable ({type(exc).__name__})")
    return ServiceState(
        rest_enabled=bool(settings.rest_enabled),
        rest_running=running,
        host=REST_HOST,
        port=int(settings.rest_port),
        mcp_enabled=bool(settings.mcp_enabled),
        problems=_startup_problem_codes(app),
        clients=clients,
    )


def _configured_budget(app: Application) -> tuple[Budget | None, str | None]:
    from .config.budget import resolve_budget_from_system

    try:
        return resolve_budget_from_system(app.settings), None
    except EchoActError as exc:
        return None, f"{exc.code.value}: {exc.message}"
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


def collect_budget(app: Application, notes: list[str] | None = None) -> BudgetState:
    """The three budgets and the enforcement behind them.

    Public because F-69's screen shows exactly this and has no business
    assembling it a second way; a second assembly is how the screen and the
    support file come to disagree about what was applied.
    """
    if notes is None:
        notes = []
    configured, error = _configured_budget(app)
    in_force: Budget | None = None
    running: Budget | None = None
    facility = "none"
    memory = "unavailable"
    cpu = "unavailable"
    basis = "none"
    try:
        in_force = app.supervisor.budget
        limits = app.supervisor.limits
        if limits is not None:
            facility = limits.facility or "none"
            memory = str(limits.memory)
            cpu = str(limits.cpu)
            basis = str(limits.memory_basis)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"worker limits: unavailable ({type(exc).__name__})")
    try:
        job = app.engine.current()
        running = job.budget if job is not None else None
    except Exception as exc:  # noqa: BLE001
        notes.append(f"current job budget: unavailable ({type(exc).__name__})")
    return BudgetState(
        configured=configured,
        configured_error=error,
        in_force=in_force,
        running_job=running,
        facility=facility,
        memory_enforcement=memory,
        cpu_enforcement=cpu,
        memory_basis=basis,
    )


def _collect_usage(app: Application, notes: list[str]) -> UsageLine | None:
    try:
        usage = app.supervisor.usage()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"usage: unavailable ({type(exc).__name__})")
        return None
    if usage is None:
        return None
    return UsageLine(
        rss_bytes=usage.rss_bytes,
        cpu_percent=usage.cpu_percent,
        peak_rss_bytes=usage.peak_rss_bytes,
        peak_commit_bytes=usage.peak_commit_bytes,
        source=usage.source,
        age_s=usage.age_s,
    )


def _collect_current(
    app: Application, notes: list[str]
) -> tuple[JobTrace | None, str | None, str | None]:
    try:
        job = app.engine.current()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"current job: unavailable ({type(exc).__name__})")
        return None, None, None
    if job is None:
        return None, None, None
    trace = JobTrace(
        job_id=job.job_id,
        timestamp=job.started_at or job.created_at,
        stage=str(job.state),
        error_code=job.error_code,
        budget=job.budget,
    )
    return trace, str(job.request_path), job.client_label


def _collect_jobs(app: Application, limit: int, notes: list[str]) -> tuple[JobTrace, ...]:
    """Recent jobs, without their snapshots.

    ``include_source_text`` is left at its default on purpose: F-56 makes
    the summary the default projection and the snapshot a separate,
    separately authorised request, so the export simply never has the text
    to leak.
    """
    try:
        page = app.store.list_jobs(limit=limit)
    except EchoActError as exc:
        notes.append(f"job history: {exc.code.value}")
        return ()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"job history: unavailable ({type(exc).__name__})")
        return ()
    return tuple(
        JobTrace(
            job_id=row.job_id,
            timestamp=row.ended_at or row.started_at or row.created_at,
            stage=str(row.state),
            error_code=row.error_code,
            budget=row.budget,
        )
        for row in page.items
    )


def _collect_log(max_lines: int, notes: list[str]) -> tuple[str, ...]:
    try:
        path = log_dir() / "echoact.log"
        if not path.is_file():
            notes.append("log: no log file")
            return ()
        text = _tail(path, max_lines)
    except OSError as exc:
        notes.append(f"log: unreadable ({exc.__class__.__name__})")
        return ()
    lines = [scrub(line) for line in text.splitlines() if line.strip()]
    return tuple(lines[-max_lines:])


def _tail(path: Path, max_lines: int) -> str:
    """Read the end of a file without loading the whole of it.

    4.1 lets a log reach 100 MB and N-21 forbids a query that loads
    unboundedly, so the read is bounded by bytes as well as by lines and a
    partial first line is discarded rather than shown truncated.
    """
    budget = max_lines * (LOG_LINE_MAX_CHARS + 60)
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - budget))
        raw = fh.read(budget)
    text = raw.decode("utf-8", errors="replace")
    if size > budget and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def _startup_problem_codes(app: Application) -> tuple[str, ...]:
    try:
        problems: list[Problem] = list(app.startup.problems)
    except Exception:  # noqa: BLE001
        return ()
    return tuple(str(p.code) for p in problems)


def _tally_codes(
    jobs: tuple[JobTrace, ...],
    current: JobTrace | None,
    problems: tuple[str, ...],
    log_lines: tuple[str, ...],
) -> tuple[tuple[str, int], ...]:
    """F-72's "recent error codes", from every place one is recorded.

    The log is included because an error that never reached a job row --  a
    failed bind, a damaged settings file -- is exactly the kind the reader
    of a support file is looking for.  Only tokens that are real
    :class:`~echoact.errors.Code` members count, so an upper-case word in a
    message cannot invent a code.
    """
    known = {c.value for c in Code}
    counts: dict[str, int] = {}
    for trace in (*jobs, *(t for t in (current,) if t is not None)):
        if trace.error_code:
            counts[trace.error_code] = counts.get(trace.error_code, 0) + 1
    for code in problems:
        counts[code] = counts.get(code, 0) + 1
    for line in log_lines:
        for token in _CODE_TOKEN.findall(line):
            if token in known:
                counts[token] = counts.get(token, 0) + 1
    return tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _as_time(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


# ======================================================================
# Rendering
# ======================================================================


def _stamp(value: float | None) -> str:
    if value is None:
        return "-"
    try:
        return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return f"{value:.0f}"


def _gib(n: int) -> str:
    gib = n / (1 << 30)
    return f"{gib:.2f} GiB" if gib >= 1 else f"{n / (1 << 20):.0f} MiB"


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def _budget_text(budget: Budget | None) -> str:
    if budget is None:
        return "-"
    return (
        f"CPU {budget.cpu_percent}% / {_gib(budget.memory_bytes)} / "
        f"{budget.intra_op_threads}+{budget.inter_op_threads} threads"
    )


def _rows(pairs: list[tuple[str, str]], indent: str = "  ") -> list[str]:
    if not pairs:
        return []
    width = max(len(name) for name, _ in pairs)
    return [f"{indent}{name.ljust(width)}  {value}" for name, value in pairs]


def render(report: DiagnosticReport) -> str:
    """The text the user reviews and, unchanged, the text that gets saved.

    One rendering rather than a summary on screen and a fuller file: F-72
    makes review the precondition for export, and a review of something
    other than what is exported is not a review.
    """
    out: list[str] = [
        "EchoAct diagnostic report",
        f"Generated {_stamp(report.generated_at)}",
        "",
        "This file is written locally and sent nowhere. It contains no document text,",
        "no audio, no credentials, and no home directory path.",
        "",
        "APPLICATION",
    ]
    out += _rows(
        [
            ("EchoAct", report.app_version),
            ("Python", report.python_version),
            ("Operating system", report.os_description),
            ("Data directory", "<data> (path withheld: F-72)"),
        ]
    )
    if report.packages:
        out += _rows([(name, version) for name, version in report.packages], indent="    ")

    out += ["", "MODELS"]
    if not report.models:
        out.append("  (none reported)")
    for m in report.models:
        state = m.state if m.runnable else f"{m.state}, cannot run"
        if m.using_package_cache:
            state += ", from the package cache"
        out.append(
            f"  {m.model_id} rev {m.revision[:12]}  {state}  "
            f"{_mb(m.bytes_present)} of {_mb(m.bytes_total)}"
        )
        if m.unavailable_reason:
            out.append(f"      {m.unavailable_reason}")

    svc = report.service
    out += ["", "SERVICE"]
    rest = "on" if svc.rest_enabled else "off"
    running = "listening" if svc.rest_running else "not listening"
    out += _rows(
        [
            ("REST", f"{rest}, {running}, {svc.host}:{svc.port}"),
            ("MCP", "enabled" if svc.mcp_enabled else "disabled"),
        ]
    )
    if svc.problems:
        out.append(f"  Start-up problems  {', '.join(svc.problems)}")
    if svc.clients:
        out.append("  Clients")
        for c in svc.clients:
            out.append(
                f"    {c.client_id}  {c.name}  {c.status}  "
                f"[{', '.join(c.capabilities)}]  expires {_stamp(c.expires_at)}  "
                f"last access {_stamp(c.last_access_at)}"
            )

    b = report.budget
    out += ["", "BUDGET APPLIED"]
    pairs = [
        ("Configured", b.configured_error or _budget_text(b.configured)),
        ("Worker in force", _budget_text(b.in_force)),
        ("Running job", _budget_text(b.running_job)),
        (
            "Enforcement",
            f"memory {b.memory_enforcement} ({b.memory_basis} basis), "
            f"CPU {b.cpu_enforcement} — {b.facility}",
        ),
    ]
    if report.usage is not None:
        u = report.usage
        pairs += [
            (
                "Generation job usage",
                f"CPU {u.cpu_percent:.0f}%, RSS {_gib(u.rss_bytes)}, "
                f"peak RSS {_gib(u.peak_rss_bytes)}, "
                f"peak commit {_gib(u.peak_commit_bytes)}",
            ),
            ("Measured by", f"{u.source}, {u.age_s:.1f} s ago"),
        ]
    out += _rows(pairs)

    out += ["", "CURRENT JOB"]
    if report.current_job is None:
        out.append("  (idle)")
    else:
        who = report.current_job_client or "-"
        out.append(f"  requested by {report.current_job_path or '-'} ({who})")
        out.append("  " + report.current_job.line())

    out += ["", "RECENT JOBS  (id · timestamp · stage · error code · applied budget)"]
    if not report.jobs:
        out.append("  (none)")
    for trace in report.jobs:
        out.append("  " + trace.line())

    out += ["", "RECENT ERROR CODES"]
    if not report.error_codes:
        out.append("  (none)")
    out += _rows([(code, str(n)) for code, n in report.error_codes])

    if report.notes:
        out += ["", "NOTES"]
        out += [f"  {note}" for note in report.notes]

    n = len(report.log_lines)
    out += ["", f"LOG  (last {n} {'line' if n == 1 else 'lines'}, stripped)"]
    if not report.log_lines:
        out.append("  (no log lines)")
    out += [f"  {line}" for line in report.log_lines]
    out.append("")
    return "\n".join(out)


# ======================================================================
# Saving
# ======================================================================


def default_filename(now: float | None = None) -> str:
    at = ids.now() if now is None else now
    stamp = datetime.fromtimestamp(at).strftime("%Y%m%d-%H%M%S")
    return f"echoact-diagnostics-{stamp}.txt"


def save(text: str, destination: str | Path) -> Path:
    """Write the reviewed text to a file the user chose.

    Takes the text rather than the report: F-72 exports what the user
    reviewed, so re-rendering here -- with a clock that has moved and a
    usage sample that has changed -- would write a document nobody saw.

    Every failure leaves as an :class:`EchoActError` with the closest code
    the catalogue has, because rule 3 applies to a helper as much as to a
    subsystem and the caller here is a dialog with one message area.
    """
    path = Path(destination)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except PermissionError as exc:
        raise EchoActError(
            Code.FILE_PERMISSION,
            "The diagnostic file could not be written to that location.",
            detail={"path": redact(path)},
            cause=exc,
        ) from exc
    except OSError as exc:
        code = Code.STORAGE_FULL if exc.errno == errno.ENOSPC else Code.INTERNAL
        raise EchoActError(
            code,
            "The diagnostic file could not be written.",
            detail={"path": redact(path)},
            cause=exc,
        ) from exc
    log.info("diagnostic export written to %s (%d bytes)", redact(path), len(text))
    return path


def export_text(app: Application, *, now: float | None = None) -> str:
    """Collect and render in one call, for a caller that only wants the text."""
    return render(collect(app, now=now))


__all__ = [
    "BudgetState",
    "ClientLine",
    "DiagnosticReport",
    "JobTrace",
    "ModelLine",
    "ServiceState",
    "UsageLine",
    "collect",
    "collect_budget",
    "default_filename",
    "export_text",
    "render",
    "save",
    "scrub",
]

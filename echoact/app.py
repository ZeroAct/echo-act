"""Composition root: builds the parts and wires them together once.

Everything that has a lifetime is created here, in an order that follows
from the requirements rather than from convenience:

* Storage first, because F-45's reconciliation has to run before anything
  can create a new job, and N-02's cleanup of what a forced termination
  left behind has to run before anything writes into the scratch tree.
* The engine next, because the GUI, the REST service, and MCP all route
  through the one generation slot F-47 allows.
* The local service last, and separately, because F-79 requires the GUI,
  generation, playback, and the library to stay fully usable when the port
  cannot be bound -- so a service that fails to start is a notice, never
  an exception that reaches this far.

No Qt import appears in this module.  The service and the engine must run
in a headless test, and a GUI import here would make that impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audio.player import Player
from .config.settings import Settings, SettingsModelPreferences, SettingsStore
from .db.backup import BackupScheduler
from .db.store import Store
from .domain import Capability
from .engine.supervisor import WorkerSupervisor
from .errors import Code, EchoActError, Problem
from .jobs.engine import JobEngine, clear_temp_tree, expire_one_off_results
from .models.catalog import MANIFEST
from .models.registry import ModelRegistry
from .paths import audio_dir, db_path, ensure_tree, temp_dir
from .playrequests import PlayRequests
from .security.credentials import CredentialStore, IssuedCredential
from .security.ratelimit import RateLimiter
from .util import ids
from .util.logging import configure, get_logger, prune_old_logs

log = get_logger("app")


@dataclass(slots=True)
class Startup:
    """What happened on the way up, for the first screen to report.

    F-25 wants failures reported rather than swallowed, and F-45 wants an
    interrupted job shown as interrupted.  Both are collected here instead
    of being logged and forgotten.
    """

    problems: list[Problem] = field(default_factory=list)
    interrupted_jobs: tuple[str, ...] = ()
    missing_results: tuple[str, ...] = ()
    temp_entries_removed: int = 0
    expired_results_removed: int = 0
    owner_credential: IssuedCredential | None = None

    @property
    def needs_attention(self) -> bool:
        return bool(self.problems or self.interrupted_jobs or self.missing_results)


class Application:
    """The parts, and their lifetime."""

    def __init__(
        self,
        *,
        data_root: Path | None = None,
        settings_store: SettingsStore | None = None,
        supervisor: WorkerSupervisor | None = None,
    ) -> None:
        configure()
        ensure_tree()
        prune_old_logs(ids.now())

        self.startup = Startup()
        self.settings_store = settings_store or SettingsStore()
        self.settings: Settings = self.settings_store.load()
        self.startup.problems.extend(self.settings_store.problems)

        self.store = Store(
            db_path() if data_root is None else data_root / "echoact.sqlite3",
            audio_root=audio_dir() if data_root is None else data_root / "audio",
            retention_limit_bytes=self.settings.retention_bytes,
        )
        # The owner's two model decisions -- accepted licence and
        # authorised download -- live in the settings file rather than in a
        # second file of the registry's own.  Two homes would mean the F-80
        # policy screen and the registry each reporting a licence the other
        # had never seen.
        self.registry = ModelRegistry(
            MANIFEST, preferences=SettingsModelPreferences(self.settings_store)
        )
        self.credentials = CredentialStore.load()
        self.limiter = RateLimiter()
        self.supervisor = supervisor or WorkerSupervisor()
        self.engine = JobEngine(
            store=self.store,
            supervisor=self.supervisor,
            registry=self.registry,
            manifest=MANIFEST,
            settings=self.settings,
        )
        self.player = Player()
        # F-89: a client may ask for its job to be played here.  The gate
        # needs the player to tell the owner's listening from its own, and
        # the setting to know whether it may at all.
        self.play_requests = PlayRequests(self.player, self.settings)
        self.scheduler = BackupScheduler()
        self.service: Any = None  # set by start_service, if it starts

        self._recover()
        self._ensure_owner_credential()

    # ------------------------------------------------------------------
    # Start-up housekeeping
    # ------------------------------------------------------------------

    def _recover(self) -> None:
        """F-45 and N-02, in that order.

        Reconciliation first: it reads job rows that may still name files
        in the scratch tree, so clearing the tree before reconciling would
        turn "interrupted" into "result missing" for the same job.
        """
        try:
            report = self.store.reconcile_on_start()
            self.startup.interrupted_jobs = tuple(report.interrupted_job_ids)
            self.startup.missing_results = tuple(report.missing_result_ids) + tuple(
                report.corrupt_result_ids
            )
        except EchoActError as exc:
            self.startup.problems.append(Problem(exc.code, exc.message))

        try:
            self.startup.expired_results_removed = expire_one_off_results(self.store, temp_dir())
        except EchoActError as exc:
            self.startup.problems.append(Problem(exc.code, exc.message))

        # 4.1: a one-off GUI result is cleaned up when the app exits
        # normally, and a forced termination leaves the rest behind.  What
        # survives here is only what the sweep above did not claim, so it
        # is scratch by definition.
        self.startup.temp_entries_removed = clear_temp_tree(temp_dir())

    def _ensure_owner_credential(self) -> None:
        """F-71 and N-31.

        The service listens from first launch, so a credential has to exist
        from first launch -- and it must be minted here rather than shipped,
        because a distribution containing one would be a well-known
        credential, which N-31 forbids outright.
        """
        if self.credentials.owner_credential() is not None:
            return
        try:
            issued = self.credentials.issue(
                name="EchoAct (this computer)",
                capabilities={Capability.OWNER},
                days=self.settings.credential_days,
            )
            self.startup.owner_credential = issued
        except EchoActError as exc:
            self.startup.problems.append(Problem(exc.code, exc.message))

    # ------------------------------------------------------------------
    # Scheduled backup (F-74, N-28)
    # ------------------------------------------------------------------

    def run_due_backup(self, now: float | None = None) -> Any:
        """Take the daily backup if one is due.  Returns the outcome or None.

        Called on a timer by whoever owns one, and never on the Qt main
        thread: a backup copies the whole library.  Everything F-74 asks
        for is decided inside the scheduler -- once a day, only while the
        app is running, deferred during generation and during a restore, a
        missed schedule made up once rather than accumulating -- so this is
        only the tick and the two facts the scheduler cannot see for
        itself: whether a job is running, and where the owner wants it.

        N-28 puts a scheduled backup behind the user's generation and
        playback, which is why ``generating`` is passed rather than
        inferred: deferring is the scheduler's decision, but knowing is
        this object's.
        """
        settings = self.settings
        if not settings.scheduled_backup:
            return None
        try:
            return self.scheduler.run_due(
                self.store,
                ids.now() if now is None else now,
                enabled=True,
                location=settings.scheduled_backup_location,
                generating=self.engine.busy,
            )
        except EchoActError as exc:
            # F-70 notifies a backup failure; it is never a reason to stop.
            log.warning("scheduled backup: %s", exc.code.value)
            self.startup.problems.append(Problem(exc.code, exc.message))
            return None

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def update_settings(self, **changes: Any) -> Settings:
        """Change settings and tell everyone who cares.

        F-78: the running job is untouched.  It recorded the budget it
        started under, and nothing here reaches into it.
        """
        self.settings = self.settings.with_(**changes)
        self.settings_store.save(self.settings)
        self.engine.apply_settings(self.settings)
        self.play_requests.apply_settings(self.settings)
        self.store.set_retention_limit(self.settings.retention_bytes)
        return self.settings

    # ------------------------------------------------------------------
    # Model preparation (F-09, N-11, F-80)
    # ------------------------------------------------------------------

    def licence_pending(self, model_id: str | None = None) -> str | None:
        """The model whose terms have not been accepted, if any.

        N-11 makes acceptance a condition of first preparation, so the
        window asks this before it starts a job rather than letting the
        engine refuse one and reporting a code.
        """
        target = model_id or self.settings.voice.model_id
        return target if self.registry.license_acceptance_required(target) else None

    def accept_licence(self, model_id: str) -> None:
        """Record that the owner accepted this model's restrictions.

        Recorded against a fingerprint of the terms themselves, so a
        release that amends them asks again instead of inheriting consent
        given to different words.
        """
        self.registry.accept_license(model_id)
        self.settings = self.settings_store.load()
        self.engine.apply_settings(self.settings)

    # ------------------------------------------------------------------
    # The local service (F-46, F-79, N-31)
    # ------------------------------------------------------------------

    def start_service(self) -> Problem | None:
        """Start the REST service if the owner has it on.

        Returns a problem instead of raising.  F-79 is explicit that a bind
        failure disables the integrations only and must be shown as an
        actionable notice rather than a startup failure, so this cannot be
        allowed to propagate.
        """
        if not self.settings.rest_enabled:
            return None
        try:
            from .service.server import ServiceRunner

            self.service = ServiceRunner(self)
            self.service.start()
            return None
        except EchoActError as exc:
            self.service = None
            problem = Problem(exc.code, exc.message)
            self.startup.problems.append(problem)
            return problem
        except Exception as exc:  # noqa: BLE001 - the GUI must still run
            self.service = None
            problem = Problem(
                Code.SERVICE_PORT_UNAVAILABLE,
                f"The local service could not start ({type(exc).__name__}).",
            )
            self.startup.problems.append(problem)
            return problem

    def stop_service(self) -> None:
        if self.service is not None:
            try:
                self.service.stop()
            finally:
                self.service = None

    @property
    def service_running(self) -> bool:
        return self.service is not None and getattr(self.service, "running", False)

    # ------------------------------------------------------------------
    # Shutdown (F-52, F-77)
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop in the reverse order of construction.

        The service first, so no new request can arrive while the engine is
        being torn down; then the engine, which kills the worker; then
        playback; and the database last, because everything above may want
        to write a final row.
        """
        self.stop_service()
        try:
            self.engine.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("engine shutdown: %s", type(exc).__name__)
        try:
            self.player.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("player shutdown: %s", type(exc).__name__)
        try:
            # 4.1: a GUI one-off result is cleaned up on a normal exit.
            clear_temp_tree(temp_dir())
        except OSError:
            pass
        self.store.close()

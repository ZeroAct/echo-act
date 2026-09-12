"""Everything the app remembers between runs (F-24), and nothing it must not.

F-24 restores the model, language, gender, voice, speaking style, tempo, CPU
and memory settings after a normal exit, and is equally explicit that unsaved
input and the previous playback session are *not* restored.  That second half
is a requirement about what this module may hold, so it is enforced by shape:
there is no field here for draft text, a playback position, or a last-opened
job, and ``test_settings.py`` asserts that no such key reaches a saved file.
A "restore my last session" convenience would be a defect, not a feature.

The rest of Section 4.1's changeable defaults live here too, so that one file
answers "what is configurable, and what does it start as".  Safety limits are
not configurable and stay in ``echoact.policy``; a value read back from disk is
clamped against them rather than trusted, because a hand-edited or truncated
settings file must never widen a limit.

Writing is atomic (a temp file made unique per *call*, fsync, ``os.replace``).
N-14 requires previously sound data to survive a save that fails midway, and
settings are the state the app rewrites most often, so a partial write here is
the likeliest way to lose it.  Two saves running at once must not share a temp
file either: they would interleave into one payload and each rename would
report the other's outcome, which loses an update while calling it a success.
Reading is the mirror image: an unreadable or damaged file yields defaults plus
a reported ``Problem``, never an exception that stops the app launching -- down
to the values JSON can carry but Python's ``int()`` and ``float()`` refuse,
``NaN``, ``Infinity``, and an integer literal too large for a float.

A file stamped with a ``version`` newer than this build writes is not read and
not rewritten (N-15).  Interpreting the keys it happens to share with this
version would apply a v1 meaning to a v2 value, and saving afterwards would
stamp the v1 meaning back over the file -- destroying the newer settings that
N-15 exists to protect.  Such a file yields defaults, a reported ``Problem``,
and a refusal to save until the newer build is used again.
"""

from __future__ import annotations

import errno
import json
import locale
import math
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from ..domain import Budget, Gender, Language, SpeakingStyle, VoiceSettings
from ..errors import Code, EchoActError, Problem
from ..paths import redact, settings_path
from ..policy import (
    AUTOPLAY_DEFAULT,
    AUTOSAVE_DOCUMENTS_DEFAULT,
    CPU_PERCENT_DEFAULT,
    CPU_PERCENT_MAX,
    CPU_PERCENT_MIN,
    CREDENTIAL_DAYS_DEFAULT,
    CREDENTIAL_DAYS_MAX,
    CREDENTIAL_DAYS_MIN,
    EXTERNAL_PLAY_DEFAULT,
    FOLLOW_DEFAULT,
    MCP_ENABLED_DEFAULT,
    MEMORY_CEILING_BYTES,
    MEMORY_FLOOR_BYTES,
    OS_NOTIFICATIONS_DEFAULT,
    PLAYBACK_VOLUME_DEFAULT,
    REST_ENABLED_DEFAULT,
    REST_PORT_DEFAULT,
    RETAIN_HISTORY_DEFAULT,
    RETENTION_DEFAULT_BYTES,
    RETENTION_MAX_BYTES,
    RETENTION_MIN_BYTES,
    SCHEDULED_BACKUP_DEFAULT,
    TEMPO_DEFAULT,
    TEMPO_MAX,
    TEMPO_MIN,
    VOICE_PRESET_MAX,
)

#: The model this release ships (A.3).  ``echoact.models`` owns the manifest
#: that describes it; settings need only a stable identifier to remember, and
#: importing the manifest here would make reading settings depend on the model
#: cache being present.  Kept in step with the manifest's single entry.
DEFAULT_MODEL_ID: Final = "supertonic-3"
#: F-06: five female and five male voices, ``F1``-``F5`` and ``M1``-``M5``.
DEFAULT_VOICE_ID: Final = "F1"
DEFAULT_GENDER: Final = Gender.FEMALE

#: Bumped when the on-disk shape changes in a way a reader must notice.  N-15
#: forbids an older build from destructively rewriting a newer format, so the
#: number is recorded even though this release only ever writes version 1.
SETTINGS_SCHEMA_VERSION: Final = 1

# The operating system's own division between privileged and user ports, not a
# product policy: the REST port is only ever bound on loopback (N-17), and a
# port below this cannot be bound by a standard user account (8.1).
_MIN_USER_PORT: Final = 1024
_MAX_PORT: Final = 65535


class DisplayLanguage(StrEnum):
    """F-86's UI language.

    Deliberately *not* ``domain.Language``: that enum carries ``AUTO`` and
    describes what the narration sounds like, and F-86 states the display
    language is independent of it.  Sharing one type would make "auto" a legal
    answer to "which language is the menu in", which it is not.
    """

    KO = "ko"
    EN = "en"


def os_display_language() -> DisplayLanguage:
    """F-86's default: the operating system language, falling back to English.

    The POSIX environment variables come first because they are what a user
    overrides deliberately, then the Windows UI language, then the process
    locale.  ``locale.getlocale()`` is last because Python does not call
    ``setlocale`` for messages at start-up, so on a fresh interpreter it often
    reports nothing at all and would mask a perfectly good answer above it.
    """
    for var in ("ECHOACT_DISPLAY_LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        tag = os.environ.get(var)
        if tag:
            return _language_of_tag(tag)
    if sys.platform == "win32":  # pragma: no cover - platform-specific branch
        tag = _windows_ui_language()
        if tag:
            return _language_of_tag(tag)
    with suppress(ValueError, TypeError):
        current = locale.getlocale()[0]
        if current:
            return _language_of_tag(current)
    return DisplayLanguage.EN


def _language_of_tag(tag: str) -> DisplayLanguage:
    normalised = tag.replace("_", "-").strip().lower()
    if normalised.startswith(("ko", "korean")):
        return DisplayLanguage.KO
    return DisplayLanguage.EN


def _windows_ui_language() -> str:  # pragma: no cover - platform-specific branch
    try:
        import ctypes

        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return ""
    return locale.windows_locale.get(lcid, "")


@dataclass(frozen=True, slots=True)
class VoicePreset:
    """F-66.  A named combination of model, language, gender, voice, tempo and
    speaking style -- and nothing else, because F-66 forbids a preset from
    moving a resource ceiling or an integration permission."""

    name: str
    voice: VoiceSettings

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "voice": self.voice.to_dict()}

    @classmethod
    def from_dict(
        cls, d: Mapping[str, Any], problems: list[Problem] | None = None
    ) -> VoicePreset:
        """Read one preset, clamped exactly like ``Settings.voice``.

        ``VoiceSettings.from_dict`` is deliberately *not* used: it trusts the
        mapping and never calls ``validate()``, so a hand-edited file could
        park a 99x tempo in a preset and ``apply_preset`` would copy it into
        ``Settings.voice``, past the clamp that guards the voice read from the
        same file.  F-07's range must hold for every route out of the file,
        and a preset is one of them.  Repairs are appended to ``problems``
        when the caller is collecting them for F-25.
        """
        voice = d["voice"]
        if not isinstance(voice, Mapping):
            raise TypeError("a preset's voice must be a table")
        return cls(
            name=str(d["name"]),
            voice=_read_voice(voice, default_voice(), [] if problems is None else problems),
        )


@dataclass(frozen=True, slots=True)
class ResourcePolicyView:
    """F-78's on-screen distinction between what is set and what is in force.

    A change made while a job runs applies to the *next* job, so the two values
    legitimately disagree for the length of a job.  Showing only one of them is
    what F-78 forbids, and ``applied is None`` says plainly that no job holds a
    budget rather than implying the configured value is currently in force.
    """

    configured_cpu_percent: int
    #: ``None`` means "derive it from this machine's RAM", per F-21.
    configured_memory_bytes: int | None
    applied: Budget | None

    @property
    def differs(self) -> bool:
        if self.applied is None:
            return False
        if self.applied.cpu_percent != self.configured_cpu_percent:
            return True
        return (
            self.configured_memory_bytes is not None
            and self.applied.memory_bytes != self.configured_memory_bytes
        )


def output_device_key(host_api: str, name: str) -> str:
    """The one spelling of a remembered output device (F-67).

    ``echoact.audio.devices`` owns this format -- ``OutputDevice.key`` builds
    it and ``devices.resolve`` compares against it exactly -- and settings
    only stores it.  It is a function here rather than a sentence in a
    docstring so that a caller holding a device cannot invent a second
    spelling: a bare device name resolves to no device, which ``resolve``
    defines as "the remembered device is gone", so F-67 would pause playback
    on a speaker that is present and working, every launch.
    ``tests/test_settings.py`` pins this against the real ``OutputDevice``.
    """
    return f"{host_api}::{name}"


def _default_display_language() -> DisplayLanguage:
    return os_display_language()


def default_voice() -> VoiceSettings:
    """The settings a request takes when it names none (F-54).

    Built from the manifest constants rather than from a stored ``Settings``,
    so that an automated caller's output never depends on what the owner last
    selected in the GUI.
    """
    return VoiceSettings(
        model_id=DEFAULT_MODEL_ID,
        language=Language.AUTO,
        gender=DEFAULT_GENDER,
        voice_id=DEFAULT_VOICE_ID,
        style=SpeakingStyle.NATURAL,
        tempo=TEMPO_DEFAULT,
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """Every value the owner can change and the app remembers.

    Frozen because one settings object is handed to the job engine, the
    service, and the GUI at once; F-10 fixes a job's settings at creation, and
    a shared mutable object would make that promise depend on nobody holding a
    reference for too long.  ``with_`` returns a new instance instead.
    """

    voice: VoiceSettings = field(default_factory=default_voice)

    # -- resources (F-20, F-21) -------------------------------------------
    cpu_percent: int = CPU_PERCENT_DEFAULT
    #: ``None`` means the F-21 default -- roughly 25% of total RAM within 2-6
    #: GiB -- which cannot be a constant because it depends on the machine.
    #: Storing the derived number instead would freeze one machine's answer
    #: into a settings file that may be restored onto another.
    memory_bytes: int | None = None

    # -- playback and reading surface (F-83, F-30, F-67) -------------------
    autoplay: bool = AUTOPLAY_DEFAULT
    follow: bool = FOLLOW_DEFAULT
    volume: float = PLAYBACK_VOLUME_DEFAULT
    muted: bool = False
    #: The remembered output device's key: exactly the string
    #: ``echoact.audio.devices.OutputDevice.key`` produces, built by
    #: :func:`output_device_key`.  Not a PortAudio index -- indices are
    #: reassigned whenever a device appears or disappears, so remembering one
    #: would make the app open a *different* speaker after a reboot, exactly
    #: the unconfirmed switch F-67 forbids -- and not a bare device name
    #: either, because ``devices.resolve`` matches this string against
    #: ``OutputDevice.key`` and nothing else: a name alone would match no
    #: device, and a present speaker would be reported as gone and playback
    #: paused for good.  ``None`` is the system default.
    output_device: str | None = None

    # -- presentation (F-86, 4.1) -----------------------------------------
    display_language: DisplayLanguage = field(default_factory=_default_display_language)
    os_notifications: bool = OS_NOTIFICATIONS_DEFAULT

    # -- integrations (4.1, F-46) -----------------------------------------
    rest_enabled: bool = REST_ENABLED_DEFAULT
    rest_port: int = REST_PORT_DEFAULT
    mcp_enabled: bool = MCP_ENABLED_DEFAULT
    #: F-89.  An integration switch rather than a playback one, even though
    #: it decides whether a speaker makes a sound: what it governs is what a
    #: client may ask for, and F-46 is where the owner reviews that.
    external_play: bool = EXTERNAL_PLAY_DEFAULT
    credential_days: int = CREDENTIAL_DAYS_DEFAULT

    # -- storage (4.1, F-42, F-44) ----------------------------------------
    autosave_documents: bool = AUTOSAVE_DOCUMENTS_DEFAULT
    retain_history: bool = RETAIN_HISTORY_DEFAULT
    retention_bytes: int = RETENTION_DEFAULT_BYTES
    scheduled_backup: bool = SCHEDULED_BACKUP_DEFAULT
    scheduled_backup_location: str | None = None

    # -- model preparation (F-09 with 5.3, F-80 with N-11) -----------------
    #: Models the owner has authorised for download.  5.3 refuses a download
    #: triggered by REST or MCP for anything not listed, so the default is
    #: empty: an integration can never provoke a large download on its own.
    authorised_downloads: tuple[str, ...] = ()
    #: model id -> the licence identifier the user accepted.  N-11 makes an
    #: unaccepted licence a bar to first preparation, and recording *which*
    #: licence was accepted means changed terms ask again instead of
    #: inheriting consent that was given to different terms.
    accepted_licences: Mapping[str, str] = field(default_factory=dict)

    # -- F-66 -------------------------------------------------------------
    presets: tuple[VoicePreset, ...] = ()

    #: Keys a future version wrote that this one does not understand.  Kept so
    #: that running an older build once does not silently discard the newer
    #: build's settings (N-15).
    extra: Mapping[str, Any] = field(default_factory=dict)

    #: The schema version of the document these values came from.  Equal to
    #: ``SETTINGS_SCHEMA_VERSION`` for anything this build read or built
    #: itself; larger only for a file a newer build wrote, which
    #: ``save_settings`` then refuses to overwrite (N-15).  Carried on the
    #: value rather than checked at the file, because the refusal has to
    #: follow the settings through the ``with_`` copies the GUI makes.
    schema_version: int = SETTINGS_SCHEMA_VERSION

    @property
    def from_a_newer_version(self) -> bool:
        """N-15: this document is one this build must not interpret or write."""
        return self.schema_version > SETTINGS_SCHEMA_VERSION

    # -- convenience -------------------------------------------------------

    def with_(self, **kw: Any) -> Settings:
        return replace(self, **kw)

    def policy_view(self, applied: Budget | None) -> ResourcePolicyView:
        """F-78.  Settings hold the configured value only; the ``Budget`` on a
        running ``Job`` is the applied one, and this pairs them for display."""
        return ResourcePolicyView(
            configured_cpu_percent=self.cpu_percent,
            configured_memory_bytes=self.memory_bytes,
            applied=applied,
        )

    def may_download(self, model_id: str) -> bool:
        """5.3: only a model the owner authorised in advance may be downloaded
        for a REST or MCP request."""
        return model_id in self.authorised_downloads

    def licence_accepted(self, model_id: str, licence_id: str) -> bool:
        """N-11 / F-80: the terms must be accepted before first preparation,
        and acceptance is tied to the exact licence the manifest records."""
        return self.accepted_licences.get(model_id) == licence_id

    def accept_licence(self, model_id: str, licence_id: str) -> Settings:
        accepted = dict(self.accepted_licences)
        accepted[model_id] = licence_id
        return replace(self, accepted_licences=accepted)

    def authorise_download(self, model_id: str, allowed: bool = True) -> Settings:
        remaining = [m for m in self.authorised_downloads if m != model_id]
        if allowed:
            remaining.append(model_id)
        return replace(self, authorised_downloads=tuple(remaining))

    # -- presets (F-66) ----------------------------------------------------

    def preset(self, name: str) -> VoicePreset | None:
        for p in self.presets:
            if p.name == name:
                return p
        return None

    def save_preset(self, name: str, voice: VoiceSettings) -> Settings:
        """Add or overwrite a preset, keeping list order stable.

        4.1 says a duplicate name is *confirmed* and then overwritten, so the
        confirmation belongs to the caller and arriving here means it was
        given.  Refusing the overwrite instead would leave the GUI no way to
        honour a confirmation the user has already made.
        """
        clean = name.strip()
        if not clean:
            raise EchoActError(Code.INPUT_EMPTY, "A preset needs a name.")
        voice.validate()
        existing = self.preset(clean)
        if existing is None and len(self.presets) >= VOICE_PRESET_MAX:
            raise EchoActError(
                Code.RETENTION_LIMIT_REACHED,
                f"The limit of {VOICE_PRESET_MAX} voice presets has been reached.",
                detail={"limit": VOICE_PRESET_MAX},
            )
        replacement = VoicePreset(name=clean, voice=voice)
        if existing is None:
            return replace(self, presets=(*self.presets, replacement))
        return replace(
            self,
            presets=tuple(replacement if p.name == clean else p for p in self.presets),
        )

    def rename_preset(self, old: str, new: str) -> Settings:
        clean = new.strip()
        if not clean:
            raise EchoActError(Code.INPUT_EMPTY, "A preset needs a name.")
        target = self.preset(old)
        if target is None:
            raise EchoActError(Code.NOT_FOUND, "No preset by that name.")
        # Renaming onto an occupied name is the same overwrite F-66 allows for
        # a save: the renamed entry keeps its own position, the collision goes.
        position = [p.name for p in self.presets].index(old)
        kept = [p for p in self.presets if p.name not in (old, clean)]
        kept.insert(min(position, len(kept)), VoicePreset(name=clean, voice=target.voice))
        return replace(self, presets=tuple(kept))

    def delete_preset(self, name: str) -> Settings:
        if self.preset(name) is None:
            raise EchoActError(Code.NOT_FOUND, "No preset by that name.")
        return replace(self, presets=tuple(p for p in self.presets if p.name != name))

    def apply_preset(self, name: str) -> Settings:
        """F-66: applying a preset moves the voice settings and nothing else."""
        target = self.preset(name)
        if target is None:
            raise EchoActError(Code.NOT_FOUND, "No preset by that name.")
        return replace(self, voice=target.voice)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        known: dict[str, Any] = {
            # The document's own version, not this build's: a newer marker is
            # preserved rather than stamped over.  ``save_settings`` refuses
            # to write such a document at all (N-15), so the only number that
            # ever reaches a file from here is one this build can read back.
            "version": self.schema_version,
            "voice": self.voice.to_dict(),
            "cpu_percent": self.cpu_percent,
            "memory_bytes": self.memory_bytes,
            "autoplay": self.autoplay,
            "follow": self.follow,
            "volume": round(self.volume, 4),
            "muted": self.muted,
            "output_device": self.output_device,
            "display_language": self.display_language.value,
            "os_notifications": self.os_notifications,
            "rest_enabled": self.rest_enabled,
            "rest_port": self.rest_port,
            "mcp_enabled": self.mcp_enabled,
            "external_play": self.external_play,
            "credential_days": self.credential_days,
            "autosave_documents": self.autosave_documents,
            "retain_history": self.retain_history,
            "retention_bytes": self.retention_bytes,
            "scheduled_backup": self.scheduled_backup,
            "scheduled_backup_location": self.scheduled_backup_location,
            "authorised_downloads": list(self.authorised_downloads),
            "accepted_licences": dict(self.accepted_licences),
            "presets": [p.to_dict() for p in self.presets],
        }
        # Unknown keys first, so a key from a future build can never shadow one
        # this build understands and knows how to clamp.
        merged = dict(self.extra)
        merged.update(known)
        return merged

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> tuple[Settings, tuple[Problem, ...]]:
        """Read a settings mapping, repairing rather than rejecting.

        A settings file the app cannot fully understand must not stop it from
        launching, so each unreadable field falls back to its own default and
        reports a ``Problem`` for F-25 to show.  Nothing here raises.
        """
        problems: list[Problem] = []
        defaults = cls()

        version = _read_version(d.get("version"), problems)
        if version > SETTINGS_SCHEMA_VERSION:
            # N-15.  Every key is left unread: this build cannot know which of
            # the names it recognises still mean what they meant in version 1,
            # and a value read under the wrong meaning would be applied to a
            # job and then written back over the newer file.  The unknown keys
            # are still carried so nothing is lost if the value is ever saved
            # by a build that does understand them.
            problems.append(
                Problem(
                    code=Code.FILE_UNSUPPORTED,
                    message=(
                        "Your settings were saved by a newer version of EchoAct; "
                        "this version is using its defaults and will not change them."
                    ),
                    remedies=(
                        "Use the newer version to change these settings.",
                        "Settings changed here cannot be saved until then.",
                    ),
                )
            )
            return (
                cls(
                    schema_version=version,
                    extra={k: v for k, v in d.items() if k not in _KNOWN_KEYS},
                ),
                tuple(problems),
            )

        memory = _read_memory(d.get("memory_bytes"), problems)

        return (
            cls(
                voice=_read_voice(d.get("voice"), defaults.voice, problems),
                cpu_percent=_read_int(
                    d.get("cpu_percent", defaults.cpu_percent),
                    default=defaults.cpu_percent,
                    low=CPU_PERCENT_MIN,
                    high=CPU_PERCENT_MAX,
                    name="cpu_percent",
                    problems=problems,
                ),
                memory_bytes=memory,
                autoplay=_read_bool(d.get("autoplay"), defaults.autoplay, "autoplay", problems),
                follow=_read_bool(d.get("follow"), defaults.follow, "follow", problems),
                volume=_read_float(
                    d.get("volume", defaults.volume),
                    default=defaults.volume,
                    low=0.0,
                    high=1.0,
                    name="volume",
                    problems=problems,
                ),
                muted=_read_bool(d.get("muted"), defaults.muted, "muted", problems),
                output_device=_read_optional_str(d.get("output_device"), "output_device", problems),
                display_language=_read_display_language(
                    d.get("display_language"), defaults.display_language, problems
                ),
                os_notifications=_read_bool(
                    d.get("os_notifications"),
                    defaults.os_notifications,
                    "os_notifications",
                    problems,
                ),
                rest_enabled=_read_bool(
                    d.get("rest_enabled"), defaults.rest_enabled, "rest_enabled", problems
                ),
                rest_port=_read_int(
                    d.get("rest_port", defaults.rest_port),
                    default=defaults.rest_port,
                    low=_MIN_USER_PORT,
                    high=_MAX_PORT,
                    name="rest_port",
                    problems=problems,
                ),
                mcp_enabled=_read_bool(
                    d.get("mcp_enabled"), defaults.mcp_enabled, "mcp_enabled", problems
                ),
                external_play=_read_bool(
                    d.get("external_play"), defaults.external_play, "external_play", problems
                ),
                credential_days=_read_int(
                    d.get("credential_days", defaults.credential_days),
                    default=defaults.credential_days,
                    low=CREDENTIAL_DAYS_MIN,
                    high=CREDENTIAL_DAYS_MAX,
                    name="credential_days",
                    problems=problems,
                ),
                autosave_documents=_read_bool(
                    d.get("autosave_documents"),
                    defaults.autosave_documents,
                    "autosave_documents",
                    problems,
                ),
                retain_history=_read_bool(
                    d.get("retain_history"), defaults.retain_history, "retain_history", problems
                ),
                retention_bytes=_read_int(
                    d.get("retention_bytes", defaults.retention_bytes),
                    default=defaults.retention_bytes,
                    low=RETENTION_MIN_BYTES,
                    high=RETENTION_MAX_BYTES,
                    name="retention_bytes",
                    problems=problems,
                ),
                scheduled_backup=_read_bool(
                    d.get("scheduled_backup"),
                    defaults.scheduled_backup,
                    "scheduled_backup",
                    problems,
                ),
                scheduled_backup_location=_read_optional_str(
                    d.get("scheduled_backup_location"), "scheduled_backup_location", problems
                ),
                authorised_downloads=_read_str_tuple(
                    d.get("authorised_downloads"), "authorised_downloads", problems
                ),
                accepted_licences=_read_str_map(
                    d.get("accepted_licences"), "accepted_licences", problems
                ),
                presets=_read_presets(d.get("presets"), problems),
                extra={k: v for k, v in d.items() if k not in _KNOWN_KEYS},
            ),
            tuple(problems),
        )


_KNOWN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "voice",
        "cpu_percent",
        "memory_bytes",
        "autoplay",
        "follow",
        "volume",
        "muted",
        "output_device",
        "display_language",
        "os_notifications",
        "rest_enabled",
        "rest_port",
        "mcp_enabled",
        "external_play",
        "credential_days",
        "autosave_documents",
        "retain_history",
        "retention_bytes",
        "scheduled_backup",
        "scheduled_backup_location",
        "authorised_downloads",
        "accepted_licences",
        "presets",
    }
)


# ======================================================================
# Field readers.  Each repairs one field and records why.
# ======================================================================


def _problem(code: Code, message: str) -> Problem:
    return Problem(
        code=code,
        message=message,
        remedies=("Check that setting; everything else was kept as saved.",),
    )


def _read_bool(raw: Any, default: bool, name: str, problems: list[Problem]) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    problems.append(
        _problem(Code.FILE_CORRUPT, f"Setting {name!r} was not true or false; using {default}.")
    )
    return default


def _as_number(raw: int | float) -> float | None:
    """A JSON number as a float that can be compared, or ``None`` if it cannot.

    JSON as Python parses it is wider than the numbers ``int()`` and
    ``float()`` accept: ``json.loads`` reads the JavaScript spellings ``NaN``,
    ``Infinity`` and ``-Infinity`` by default, and an integer literal has no
    size limit.  ``int(nan)`` raises ``ValueError``; ``int(inf)`` and
    ``float(10**400)`` raise ``OverflowError``.  Every field reader goes
    through here first, because load must never raise: F-24's guarantee is
    that a corrupt settings file costs the user their settings, not their
    launch, and rule 3 forbids a ``ValueError`` crossing this boundary anyway.

    An infinity keeps its sign, so a range check clamps it to the near end of
    the range like any other out-of-range number.  ``NaN`` is the one value
    with no order at all, and gets ``None``: there is no end of the range it
    is nearer to, so its reader falls back to the field's default instead.

    Going through ``float`` costs nothing here: every range in this module is
    far below 2**53, so any integer that could be *kept* converts exactly,
    and one large enough to lose precision is one about to be clamped.
    """
    try:
        value = float(raw)
    except OverflowError:  # an integer literal with no float to represent it
        return math.inf if raw > 0 else -math.inf
    return None if math.isnan(value) else value


def _read_version(raw: Any, problems: list[Problem]) -> int:
    """The document's schema version (N-15).

    An absent or unreadable marker reads as this build's own version: a file
    with no version at all is one this build wrote before the marker existed,
    and treating a damaged marker as "from the future" would lock the user
    out of their own settings over a single bad byte.  A marker that is
    legibly larger is the case N-15 is about, and is honoured.
    """
    if raw is None:
        return SETTINGS_SCHEMA_VERSION
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        problems.append(
            _problem(Code.FILE_CORRUPT, "The settings file's version marker was unreadable.")
        )
        return SETTINGS_SCHEMA_VERSION
    value = _as_number(raw)
    if value is None or math.isinf(value):
        problems.append(
            _problem(Code.FILE_CORRUPT, "The settings file's version marker was unreadable.")
        )
        return SETTINGS_SCHEMA_VERSION
    return int(value)


def _read_memory(raw: Any, problems: list[Problem]) -> int | None:
    """``None`` is not a missing value here but F-21's automatic default, so an
    unreadable one returns to automatic rather than to the 2 GiB floor -- the
    floor is what F-23 refuses below, not what a user meant to ask for."""
    if raw is None:
        return None
    # ``NaN`` is literally not a number, so it takes this branch rather than
    # the clamp below: there is no size it is closest to.
    if isinstance(raw, bool) or not isinstance(raw, int | float) or _as_number(raw) is None:
        problems.append(
            _problem(
                Code.FILE_CORRUPT,
                "Setting 'memory_bytes' was not a number; sizing it from this computer instead.",
            )
        )
        return None
    return _read_int(
        raw,
        default=MEMORY_FLOOR_BYTES,
        low=MEMORY_FLOOR_BYTES,
        high=MEMORY_CEILING_BYTES,
        name="memory_bytes",
        problems=problems,
    )


def _read_int(
    raw: Any, *, default: int, low: int, high: int, name: str, problems: list[Problem]
) -> int:
    if raw is None:  # an explicit null reads the same as an absent key
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        problems.append(
            _problem(Code.FILE_CORRUPT, f"Setting {name!r} was not a number; using {default}.")
        )
        return default
    value = _as_number(raw)
    if value is None:  # NaN: nothing to clamp towards
        problems.append(
            _problem(Code.FILE_CORRUPT, f"Setting {name!r} was not a number; using {default}.")
        )
        return default
    if value < low or value > high:
        # Clamped from the bound, not from ``value``: an infinity has no
        # ``int()``, and the bound is the answer either way.
        clamped = low if value < low else high
        problems.append(
            _problem(
                Code.FILE_CORRUPT,
                f"Setting {name!r} was outside its allowed range; using {clamped}.",
            )
        )
        return clamped
    return int(value)


def _read_float(
    raw: Any, *, default: float, low: float, high: float, name: str, problems: list[Problem]
) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        problems.append(
            _problem(Code.FILE_CORRUPT, f"Setting {name!r} was not a number; using {default}.")
        )
        return default
    value = _as_number(raw)
    if value is None or value < low or value > high:  # NaN has no order to clamp
        clamped = default if value is None else (low if value < low else high)
        problems.append(
            _problem(
                Code.FILE_CORRUPT,
                f"Setting {name!r} was outside its allowed range; using {clamped}.",
            )
        )
        return clamped
    return value


def _read_optional_str(raw: Any, name: str, problems: list[Problem]) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw or None
    problems.append(_problem(Code.FILE_CORRUPT, f"Setting {name!r} was not text; ignoring it."))
    return None


def _read_str_tuple(raw: Any, name: str, problems: list[Problem]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str | bytes | Mapping) or not isinstance(raw, Iterable):
        problems.append(_problem(Code.FILE_CORRUPT, f"Setting {name!r} was not a list; ignoring."))
        return ()
    items = list(raw)
    kept = [item for item in items if isinstance(item, str) and item]
    if len(kept) != len(items):
        problems.append(
            _problem(Code.FILE_CORRUPT, f"Some entries of {name!r} were not text and were dropped.")
        )
    return tuple(dict.fromkeys(kept))


def _read_str_map(raw: Any, name: str, problems: list[Problem]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        problems.append(_problem(Code.FILE_CORRUPT, f"Setting {name!r} was not a table; ignoring."))
        return {}
    kept = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    if len(kept) != len(raw):
        problems.append(
            _problem(Code.FILE_CORRUPT, f"Some entries of {name!r} were unreadable and dropped.")
        )
    return kept


def _read_display_language(
    raw: Any, default: DisplayLanguage, problems: list[Problem]
) -> DisplayLanguage:
    if raw is None:
        return default
    try:
        return DisplayLanguage(str(raw))
    except ValueError:
        problems.append(
            _problem(
                Code.LANGUAGE_UNKNOWN,
                f"Display language {raw!r} is not one this version has; using {default.value}.",
            )
        )
        return default


def _read_voice(raw: Any, default: VoiceSettings, problems: list[Problem]) -> VoiceSettings:
    """F-24's core: model, language, gender, voice, style, and tempo.

    Each field falls back on its own rather than the block falling back as a
    whole, so a file naming a voice this build no longer ships still restores
    the user's tempo, language, and style.
    """
    if raw is None:
        return default
    if not isinstance(raw, Mapping):
        problems.append(
            _problem(Code.FILE_CORRUPT, "The saved voice settings were unreadable; using defaults.")
        )
        return default

    model_id = raw.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        model_id = default.model_id
        problems.append(_problem(Code.MODEL_UNKNOWN, f"No saved model; using {default.model_id}."))

    voice_id = raw.get("voice_id")
    if not isinstance(voice_id, str) or not voice_id:
        voice_id = default.voice_id
        problems.append(_problem(Code.VOICE_UNKNOWN, f"No saved voice; using {default.voice_id}."))

    try:
        language = Language(str(raw.get("language", default.language.value)))
    except ValueError:
        language = default.language
        problems.append(
            _problem(
                Code.LANGUAGE_UNKNOWN,
                "The saved narration language is not one this version has; using automatic.",
            )
        )

    try:
        gender = Gender(str(raw.get("gender", default.gender.value)))
    except ValueError:
        gender = default.gender
        problems.append(
            _problem(
                Code.FILE_CORRUPT,
                f"The saved voice gender was unreadable; using {default.gender.value}.",
            )
        )

    try:
        style = SpeakingStyle(str(raw.get("style", default.style.value)))
    except ValueError:
        style = default.style
        problems.append(
            _problem(
                Code.STYLE_UNKNOWN,
                "The saved speaking style is not one this version has; using natural.",
            )
        )

    return VoiceSettings(
        model_id=model_id,
        language=language,
        gender=gender,
        voice_id=voice_id,
        style=style,
        tempo=_read_tempo(raw.get("tempo", default.tempo), default.tempo, problems),
    )


def _read_tempo(raw: Any, default: float, problems: list[Problem]) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        problems.append(_problem(Code.FILE_CORRUPT, "The saved tempo was not a number; using 1.00x."))
        return default
    tempo = _as_number(raw)
    if tempo is None:  # NaN
        problems.append(_problem(Code.TEMPO_OUT_OF_RANGE, "The saved tempo was not a number."))
        return TEMPO_DEFAULT
    if not TEMPO_MIN <= tempo <= TEMPO_MAX:
        clamped = TEMPO_MIN if tempo < TEMPO_MIN else TEMPO_MAX
        problems.append(
            _problem(
                Code.TEMPO_OUT_OF_RANGE,
                f"The saved tempo was outside {TEMPO_MIN:.2f}x-{TEMPO_MAX:.2f}x; "
                f"using {clamped:.2f}x.",
            )
        )
        return clamped
    return tempo


def _read_presets(raw: Any, problems: list[Problem]) -> tuple[VoicePreset, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str | bytes | Mapping) or not isinstance(raw, Iterable):
        problems.append(_problem(Code.FILE_CORRUPT, "The saved presets were unreadable; ignoring."))
        return ()
    kept: list[VoicePreset] = []
    seen: set[str] = set()
    dropped = 0
    for item in raw:
        if not isinstance(item, Mapping):
            dropped += 1
            continue
        # The name is settled before the voice is read, so that a duplicate
        # this loop is about to discard does not report repairs for itself.
        name = item.get("name")
        if not isinstance(name, str) or not name or name in seen:
            dropped += 1
            continue
        try:
            preset = VoicePreset.from_dict(item, problems)
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        seen.add(preset.name)
        kept.append(preset)
    if len(kept) > VOICE_PRESET_MAX:
        dropped += len(kept) - VOICE_PRESET_MAX
        kept = kept[:VOICE_PRESET_MAX]
    if dropped:
        problems.append(
            _problem(Code.FILE_CORRUPT, f"{dropped} saved voice preset(s) could not be read.")
        )
    return tuple(kept)


# ======================================================================
# Reading and writing the file
# ======================================================================


def load_settings(path: Path | None = None) -> tuple[Settings, tuple[Problem, ...]]:
    """Read the settings file, or return defaults and say what went wrong.

    Deliberately free of side effects: it does not create the file, and it does
    not move a damaged one aside.  A first launch has to be indistinguishable
    from a launch that found nothing to read, and quarantining the file here
    would destroy the only evidence of what went wrong before anyone saw it.
    """
    target = path or settings_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings(), ()
    except PermissionError:
        return Settings(), (
            Problem(
                code=Code.FILE_PERMISSION,
                message="The settings file could not be read; defaults are in use.",
                remedies=(f"Check the permissions on {redact(target)}.",),
            ),
        )
    except (OSError, UnicodeDecodeError):
        return Settings(), (_unreadable(target, "The settings file could not be read"),)

    try:
        data = json.loads(text)
    except ValueError:
        return Settings(), (_unreadable(target, "The settings file is damaged"),)
    if not isinstance(data, Mapping):
        return Settings(), (_unreadable(target, "The settings file does not contain settings"),)
    return Settings.from_dict(data)


def _unreadable(target: Path, what: str) -> Problem:
    return Problem(
        code=Code.FILE_CORRUPT,
        message=f"{what}; defaults are in use.",
        remedies=(
            f"{redact(target)} is rewritten the next time a setting changes.",
            "Move it aside first if you want to keep a copy.",
        ),
    )


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write the settings file so that a crash cannot corrupt it (N-14).

    A temp file of its own in the same directory, flush, ``fsync``, then
    ``os.replace``.  The rename is atomic on both supported platforms, so a
    reader sees either the whole old file or the whole new one.  Writing in
    place would leave a truncated file if the process died between the
    truncate and the write, and N-14 requires previously sound data to survive
    a save that fails midway.

    The temp name comes from ``mkstemp`` rather than from the process id: two
    saves at once in one process would otherwise share a single temp file,
    interleave their payloads in it, and race their renames -- landing one
    save's content while telling the *other* caller it succeeded.  Lost data
    reported as a success is precisely what N-14 forbids.  ``SettingsStore``
    serialises its own saves on top of this; a unique temp file is what makes
    two unserialised ones merely ordered rather than corrupting.
    """
    if settings.from_a_newer_version:
        raise EchoActError(
            Code.FILE_UNSUPPORTED,
            "These settings were saved by a newer version of EchoAct and were not changed.",
            detail={
                "file_version": settings.schema_version,
                "this_version": SETTINGS_SCHEMA_VERSION,
            },
        )
    target = path or settings_path()
    try:
        # ``allow_nan`` off: ``json.dumps`` would happily write the JavaScript
        # spellings ``NaN`` and ``Infinity``, which are legal for the parser
        # to read back but are not a CPU percentage or a byte count.  Refusing
        # here keeps the last sound file in place instead of writing one this
        # app could only ever repair, and turns a caller's bad value into the
        # one exception type rule 3 allows out of a module.
        payload = (
            json.dumps(
                settings.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n"
        )
    except (ValueError, TypeError) as exc:
        raise EchoActError(
            Code.INTERNAL,
            "The settings could not be saved; your previous settings are unchanged.",
            cause=exc,
        ) from exc

    tmp: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".tmp")
        tmp = Path(name)
        with open(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except OSError as exc:
        if tmp is not None:
            with suppress(OSError):
                tmp.unlink()
        raise EchoActError(
            _save_failure_code(exc),
            "The settings could not be saved; your previous settings are unchanged.",
            cause=exc,
        ) from exc


def _save_failure_code(exc: OSError) -> Code:
    if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        return Code.STORAGE_FULL
    if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return Code.FILE_PERMISSION
    return Code.INTERNAL


def _fsync_dir(directory: Path) -> None:
    """Make the rename itself durable where the OS allows it.

    Windows has no directory handle to sync -- ``os.open`` on a directory fails
    there -- so this is a no-op on Windows.  The atomicity N-14 needs comes
    from ``os.replace`` either way; only the ordering of the rename against a
    power loss is left unguaranteed.
    """
    if sys.platform == "win32":
        return
    with suppress(OSError):  # pragma: no cover - platform-specific branch
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class SettingsStore:
    """The app's one handle on the settings file.

    Holds the last value successfully read or written, so that a failed save
    leaves the in-memory state matching what is actually on disk: N-14's
    "previously sound data is preserved" is only true if the app agrees with
    the file about which version survived.

    One lock covers reading, writing, and the read-modify-write in
    ``update``.  The GUI, the REST service and the MCP server all change
    settings, on three different threads; without it two ``update`` calls both
    read the same starting value and the second silently drops the first
    caller's change, while the store's in-memory value ends up describing
    whichever save happened to rename last rather than what the file holds.
    It is an ``RLock`` because ``current`` may load inside a held lock.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings_path()
        self._settings = Settings()
        self._problems: tuple[Problem, ...] = ()
        self._loaded = False
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def problems(self) -> tuple[Problem, ...]:
        """Whatever the last load had to repair.  F-25 reports these once."""
        with self._lock:
            return self._problems

    @property
    def current(self) -> Settings:
        with self._lock:
            if not self._loaded:
                self.load()
            return self._settings

    def load(self) -> Settings:
        with self._lock:
            self._settings, self._problems = load_settings(self._path)
            self._loaded = True
            return self._settings

    def save(self, settings: Settings) -> None:
        with self._lock:
            save_settings(settings, self._path)
            self._settings = settings
            self._loaded = True

    def update(self, **kw: Any) -> Settings:
        with self._lock:
            updated = self.current.with_(**kw)
            self.save(updated)
            return updated

    def mutate(self, change: Callable[[Settings], Settings]) -> Settings:
        """Save a change computed from the current value, atomically.

        ``update`` covers "set these fields"; this covers the changes that
        have to see the old value to work out the new one -- adding a preset,
        recording a licence acceptance -- which is exactly where a read and a
        separate write let a concurrent save in between and lose one of them.
        """
        with self._lock:
            updated = change(self.current)
            self.save(updated)
            return updated


class SettingsModelPreferences:
    """``echoact.models.registry.ModelPreferences``, backed by the settings file.

    N-11 puts licence acceptance in settings and 5.3 puts the download
    pre-authorisation there too, so both belong in the one file this module
    owns -- ``registry.JsonModelPreferences`` says as much and exists only
    until there is something here to hand it.  This is that thing: wire it
    into ``ModelRegistry(preferences=SettingsModelPreferences(store))`` and
    the owner's two decisions have a single home, one that N-14's atomic save
    and N-15's version check already protect.  Two homes would be worse than
    either: the F-80 policy screen reads settings, the registry reads its own
    file, and each would report a licence the other had never seen.

    The protocol's spellings are kept exactly -- ``license_accepted`` and its
    ``fingerprint``, both American, both the registry's -- because a protocol
    is only satisfied by the caller's names.  The translation to this
    module's ``licence`` and to a new frozen ``Settings`` happens here, where
    it is one adapter rather than a rule every caller has to remember.
    """

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store if store is not None else SettingsStore()

    @property
    def store(self) -> SettingsStore:
        return self._store

    def license_accepted(self, model_id: str, fingerprint: str) -> bool:
        return self._store.current.licence_accepted(model_id, fingerprint)

    def record_license_acceptance(self, model_id: str, fingerprint: str) -> None:
        self._store.mutate(lambda s: s.accept_licence(model_id, fingerprint))

    def download_authorised(self, model_id: str) -> bool:
        return self._store.current.may_download(model_id)

    def set_download_authorised(self, model_id: str, allowed: bool) -> None:
        self._store.mutate(lambda s: s.authorise_download(model_id, allowed))


__all__ = [
    "DEFAULT_MODEL_ID",
    "DEFAULT_VOICE_ID",
    "SETTINGS_SCHEMA_VERSION",
    "DisplayLanguage",
    "ResourcePolicyView",
    "Settings",
    "SettingsModelPreferences",
    "SettingsStore",
    "VoicePreset",
    "default_voice",
    "load_settings",
    "os_display_language",
    "output_device_key",
    "save_settings",
]

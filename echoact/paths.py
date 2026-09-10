"""Where EchoAct keeps things on disk.

One module so that F-72's diagnostic export can state every location, F-73
can size each of them separately, and N-20 can keep the user's home path out
of logs by rendering paths relative to these roots.

An ``ECHOACT_DATA_DIR`` override exists for tests and for running two builds
side by side; nothing in the product writes outside the returned tree except
files the user explicitly chooses (exported WAV, backups).
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

APP_NAME = "EchoAct"


@lru_cache(maxsize=1)
def data_dir() -> Path:
    """Per-user application data: database, audio, settings, logs."""
    override = os.environ.get("ECHOACT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def db_path() -> Path:
    return data_dir() / "echoact.sqlite3"


def audio_dir() -> Path:
    """Retained job audio and full-result WAV files."""
    return data_dir() / "audio"


def temp_dir() -> Path:
    """Segment audio for in-flight jobs and one-off results.

    N-02 requires whatever a forced termination leaves here to be cleaned up
    on relaunch, so nothing durable may live in this tree.
    """
    return data_dir() / "temp"


def log_dir() -> Path:
    return data_dir() / "logs"


def model_cache_dir() -> Path:
    """Model weights.  Kept apart from user data so F-73 can size it, F-65
    can delete it, and F-76 can offer it as a separate deletion scope."""
    override = os.environ.get("ECHOACT_MODEL_DIR")
    if override:
        return Path(override).expanduser()
    return data_dir() / "models"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def lock_path() -> Path:
    """F-85's single-instance marker."""
    return data_dir() / "instance.lock"


def ensure_tree() -> None:
    """Create every directory the app writes to.  Safe to call repeatedly."""
    for p in (data_dir(), audio_dir(), temp_dir(), log_dir(), model_cache_dir()):
        p.mkdir(parents=True, exist_ok=True)


def redact(path: str | Path) -> str:
    """Render a path for a log or a diagnostic export.

    N-20 and F-72 exclude the user's home path.  Anything under the data
    directory becomes ``<data>/...``; anything else becomes its basename, so
    a filename the user chose never leaks its directory.
    """
    p = Path(path)
    try:
        return "<data>/" + p.resolve().relative_to(data_dir().resolve()).as_posix()
    except (ValueError, OSError):
        return "<path>/" + p.name

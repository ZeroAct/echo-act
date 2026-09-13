"""EchoAct — local Korean/English speech generation."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Single source of truth is pyproject.toml's ``version`` — read it from
# the installed package metadata so release bumps can't drift (0.5.3
# shipped with a stale hand-maintained string here). The fallback only
# fires when the package isn't installed at all (e.g. sources vendored
# without ``uv sync``).
try:
    __version__ = _pkg_version("echoact")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0.dev0"

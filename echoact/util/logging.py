"""Logging that cannot leak what N-20 forbids.

Default logs carry no body text, no audio, no credential, and no home path.
That is not a review rule here but a filter: ``SensitiveFilter`` rejects a
record whose message or arguments look like a credential, and every helper
in this module takes identifiers and lengths rather than content.

Retention follows 4.1: 7 days, 100 MB, whichever is reached first.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any

from ..paths import log_dir, redact
from ..policy import LOG_MAX_BYTES, LOG_RETENTION_DAYS

_CONFIGURED = False

# Anything that looks like one of our credentials, in case a caller ever
# formats one into a message by accident.
_SECRET = re.compile(r"\b(eak_[A-Za-z0-9_\-]{8,}|[A-Fa-f0-9]{40,})\b")


class SensitiveFilter(logging.Filter):
    """Redacts credential-shaped text rather than dropping the record.

    Dropping would hide a diagnostic; N-25 wants the problem traceable.
    Redacting keeps the stage, the code, and the job id, which is what
    tracing actually needs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if _SECRET.search(msg):
            record.msg = _SECRET.sub("<redacted>", msg)
            record.args = ()
        return True


def configure(level: int = logging.INFO, *, to_file: bool = True) -> None:
    """Install handlers once.  Safe to call from any entry point."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger("echoact")
    root.setLevel(level)
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    sensitive = SensitiveFilter()

    stderr = logging.StreamHandler()
    stderr.setFormatter(fmt)
    stderr.addFilter(sensitive)
    root.addHandler(stderr)

    if to_file:
        try:
            d = log_dir()
            d.mkdir(parents=True, exist_ok=True)
            # Size-based rotation bounds the 100 MB limit; the age limit is
            # applied by ``prune_old_logs`` at start-up, because a rotating
            # handler alone cannot express "whichever is reached first".
            fh = logging.handlers.RotatingFileHandler(
                d / "echoact.log",
                maxBytes=LOG_MAX_BYTES // 5,
                backupCount=4,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            fh.addFilter(sensitive)
            root.addHandler(fh)
        except OSError:
            # A log we cannot write is never a reason not to start; F-79's
            # spirit applies to every ancillary facility.
            root.warning("log file unavailable; logging to stderr only")


def prune_old_logs(now_s: float) -> int:
    """Delete log files older than 4.1's retention window.  Returns the count."""
    cutoff = now_s - LOG_RETENTION_DAYS * 86400
    removed = 0
    try:
        for p in log_dir().glob("echoact.log*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"echoact.{name}")


def job_context(job_id: str, stage: str, **extra: Any) -> str:
    """N-25's traceable line: job id, stage, code, budget -- never text.

    Values are rendered with ``repr`` for strings only after checking they
    are short; anything long is reported by length, because the one long
    string in this system is the user's document.
    """
    parts = [f"job={job_id}", f"stage={stage}"]
    for k, v in extra.items():
        if isinstance(v, str) and len(v) > 64:
            parts.append(f"{k}=<{len(v)} chars>")
        elif isinstance(v, Path):
            parts.append(f"{k}={redact(v)}")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)

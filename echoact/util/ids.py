"""Identifier minting.

Job, segment, result, client, and request identifiers are opaque to every
caller.  N-19 is explicit that knowing one must not by itself grant access,
so these are not required to be unguessable -- authorisation is checked
regardless -- but they are random anyway, because a guessable id invites the
mistake N-19 forbids.

Credential *secrets* are not minted here; those live in
``echoact.security.credentials``, which has different requirements.
"""

from __future__ import annotations

import secrets
import time


def _token(nbytes: int = 12) -> str:
    return secrets.token_hex(nbytes)


def job_id() -> str:
    return "job_" + _token()


def segment_id() -> str:
    return "seg_" + _token(8)


def result_id() -> str:
    return "res_" + _token()


def document_id() -> str:
    return "doc_" + _token()


def client_id() -> str:
    return "cli_" + _token(8)


def request_id() -> str:
    """F-57 puts one of these in every error so a user's report can be
    matched to a log line without the log carrying their text."""
    return "req_" + _token(8)


def backup_id() -> str:
    return "bak_" + _token(8)


def now() -> float:
    """Wall-clock seconds.  Used for created/expiry timestamps that must
    survive a restart and be shown to the user."""
    return time.time()


def monotonic() -> float:
    """For durations and deadlines.  Never mixed with ``now()``: a clock
    adjustment must not extend a bounded wait or a retry-after."""
    return time.monotonic()

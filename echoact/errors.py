"""Error catalogue shared by every access path.

N-24 requires the GUI, REST, and MCP to share one set of error semantics, and
F-57 requires each error to carry a stable code, a user-facing message, whether
a retry can succeed, and a request identifier.  This module is the single place
those facts are recorded; no surface invents a code of its own.

Messages here are the English text.  The GUI localises through
``echoact.ui.i18n`` keyed on ``code``; REST and MCP always answer in English so
that F-86's display-language setting never changes an API response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Code(StrEnum):
    """Stable error codes.  Values are part of the external contract."""

    # -- input validation (F-03, F-32 to F-37) ---------------------------
    INPUT_EMPTY = "INPUT_EMPTY"
    INPUT_TOO_LONG = "INPUT_TOO_LONG"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_UNSUPPORTED = "FILE_UNSUPPORTED"
    FILE_NOT_TEXT = "FILE_NOT_TEXT"
    FILE_ENCODING = "FILE_ENCODING"
    FILE_CORRUPT = "FILE_CORRUPT"
    FILE_ENCRYPTED = "FILE_ENCRYPTED"
    FILE_PERMISSION = "FILE_PERMISSION"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"

    # -- request shape (F-54, F-49, F-57) ---------------------------------
    #: The body could not be read at all -- malformed JSON, a multipart
    #: body whose parts do not parse.  Distinct from a body that parsed
    #: and then failed validation, because a client can act on the
    #: difference: one is a bug in how it encoded the request, the other
    #: is a wrong value in it.
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    #: A field this version does not define.  F-54 forbids substituting an
    #: unknown option, and silently dropping one is a substitution the
    #: caller cannot see.
    UNKNOWN_OPTION = "UNKNOWN_OPTION"
    #: The same fault inside the voice settings, which Section 2.10 gives a
    #: different status.  A separate code rather than an override on the
    #: one above: the status is part of what a code means here, and a
    #: per-call escape hatch would make that stop being true.
    VOICE_SETTINGS_INVALID = "VOICE_SETTINGS_INVALID"
    JOB_KIND_MISSING = "JOB_KIND_MISSING"
    JOB_KIND_UNKNOWN = "JOB_KIND_UNKNOWN"
    IDEMPOTENCY_KEY_MISSING = "IDEMPOTENCY_KEY_MISSING"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    INPUT_AMBIGUOUS = "INPUT_AMBIGUOUS"

    # -- voice settings (F-04 to F-08) -----------------------------------
    MODEL_UNKNOWN = "MODEL_UNKNOWN"
    VOICE_UNKNOWN = "VOICE_UNKNOWN"
    LANGUAGE_UNKNOWN = "LANGUAGE_UNKNOWN"
    STYLE_UNKNOWN = "STYLE_UNKNOWN"
    TEMPO_OUT_OF_RANGE = "TEMPO_OUT_OF_RANGE"
    VOICE_GENDER_MISMATCH = "VOICE_GENDER_MISMATCH"

    # -- model preparation (F-09, F-63 to F-65, F-84) --------------------
    MODEL_NOT_READY = "MODEL_NOT_READY"
    MODEL_CORRUPT = "MODEL_CORRUPT"
    MODEL_DOWNLOAD_FORBIDDEN = "MODEL_DOWNLOAD_FORBIDDEN"
    MODEL_DOWNLOAD_FAILED = "MODEL_DOWNLOAD_FAILED"
    MODEL_LICENSE_NOT_ACCEPTED = "MODEL_LICENSE_NOT_ACCEPTED"
    MODEL_OVER_BUDGET = "MODEL_OVER_BUDGET"

    # -- execution (F-23, F-47, N-23) ------------------------------------
    BUSY = "BUSY"
    RATE_LIMITED = "RATE_LIMITED"
    INSUFFICIENT_RESOURCES = "INSUFFICIENT_RESOURCES"
    OUT_OF_MEMORY = "OUT_OF_MEMORY"
    GENERATION_FAILED = "GENERATION_FAILED"
    WORKER_LOST = "WORKER_LOST"
    RUNTIME_PROVIDER_REFUSED = "RUNTIME_PROVIDER_REFUSED"

    # -- results (F-55, F-42, 4.2) ---------------------------------------
    RESULT_NOT_READY = "RESULT_NOT_READY"
    RESULT_EXPIRED = "RESULT_EXPIRED"
    RESULT_MISSING = "RESULT_MISSING"
    SEGMENT_NOT_READY = "SEGMENT_NOT_READY"

    # -- access control (N-17, N-19, F-50, F-61) -------------------------
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"
    ORIGIN_NOT_ALLOWED = "ORIGIN_NOT_ALLOWED"
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
    AUTH_LOCKED_OUT = "AUTH_LOCKED_OUT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"

    # -- storage (N-14 to N-16, F-43, F-44) ------------------------------
    STORAGE_FULL = "STORAGE_FULL"
    RETENTION_LIMIT_REACHED = "RETENTION_LIMIT_REACHED"
    DB_LOCKED = "DB_LOCKED"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    BACKUP_INVALID = "BACKUP_INVALID"
    BACKUP_INCOMPATIBLE = "BACKUP_INCOMPATIBLE"
    BACKUP_TOO_LARGE = "BACKUP_TOO_LARGE"
    DELETE_BLOCKED_IN_USE = "DELETE_BLOCKED_IN_USE"

    # -- service lifecycle (F-52, F-79, N-31) ----------------------------
    SERVICE_OFF = "SERVICE_OFF"
    SERVICE_PORT_UNAVAILABLE = "SERVICE_PORT_UNAVAILABLE"
    APP_NOT_RUNNING = "APP_NOT_RUNNING"
    MCP_DISABLED = "MCP_DISABLED"
    SHUTTING_DOWN = "SHUTTING_DOWN"

    # -- audio (F-67, F-68) ----------------------------------------------
    OUTPUT_DEVICE_LOST = "OUTPUT_DEVICE_LOST"
    OUTPUT_DEVICE_UNAVAILABLE = "OUTPUT_DEVICE_UNAVAILABLE"
    PLAYBACK_NOT_ALLOWED = "PLAYBACK_NOT_ALLOWED"
    PLAYBACK_BUSY = "PLAYBACK_BUSY"

    INTERNAL = "INTERNAL"


# code -> (HTTP status, retryable in principle, English message)
#
# "Retryable" is F-57's flag: the identical request may succeed later without
# the caller changing anything.  Every retryable rejection carries a
# retry-after hint at the surface.
_CATALOGUE: dict[Code, tuple[int, bool, str]] = {
    Code.INPUT_EMPTY: (422, False, "The text is empty or contains only whitespace."),
    Code.INPUT_TOO_LONG: (413, False, "The text exceeds the 50,000-character limit."),
    Code.FILE_TOO_LARGE: (413, False, "The file exceeds the 2,000,000-byte limit."),
    Code.FILE_UNSUPPORTED: (415, False, "This file format cannot be read as text."),
    Code.FILE_NOT_TEXT: (415, False, "The file does not appear to contain text."),
    Code.FILE_ENCODING: (415, False, "The file's text encoding could not be determined."),
    Code.FILE_CORRUPT: (415, False, "The file appears to be damaged."),
    Code.FILE_ENCRYPTED: (415, False, "The file is encrypted."),
    Code.FILE_PERMISSION: (403, False, "The file cannot be read with the current permissions."),
    Code.FILE_NOT_FOUND: (404, False, "The file no longer exists."),
    Code.MALFORMED_REQUEST: (400, False, "The request body could not be read."),
    Code.UNKNOWN_OPTION: (400, False, "The request names a field this version does not define."),
    Code.VOICE_SETTINGS_INVALID: (422, False, "The voice settings are not valid."),
    Code.JOB_KIND_MISSING: (400, False, "The request must state a job kind."),
    Code.JOB_KIND_UNKNOWN: (400, False, "That job kind is not supported by this version."),
    Code.IDEMPOTENCY_KEY_MISSING: (400, False, "A duplicate-prevention key is required."),
    Code.IDEMPOTENCY_KEY_CONFLICT: (
        409,
        False,
        "That key was already used with different content.",
    ),
    Code.INPUT_AMBIGUOUS: (400, False, "Provide either inline text or an upload, not both."),
    Code.MODEL_UNKNOWN: (422, False, "That model is not one this version supports."),
    Code.VOICE_UNKNOWN: (422, False, "That voice is not available for the selected model."),
    Code.LANGUAGE_UNKNOWN: (422, False, "That language is not supported."),
    Code.STYLE_UNKNOWN: (422, False, "That speaking style is not supported."),
    Code.TEMPO_OUT_OF_RANGE: (422, False, "Tempo must be between 0.70x and 1.50x."),
    Code.VOICE_GENDER_MISMATCH: (422, False, "That voice does not belong to the selected gender."),
    Code.MODEL_NOT_READY: (409, False, "The model has not been prepared on this machine."),
    Code.MODEL_CORRUPT: (409, False, "The model's files do not match the manifest."),
    Code.MODEL_DOWNLOAD_FORBIDDEN: (
        403,
        False,
        "The owner has not authorised downloading this model.",
    ),
    Code.MODEL_DOWNLOAD_FAILED: (503, True, "The model download did not complete."),
    Code.MODEL_LICENSE_NOT_ACCEPTED: (
        403,
        False,
        "The model's licence terms have not been accepted.",
    ),
    Code.MODEL_OVER_BUDGET: (422, False, "The model cannot run within the current resource budget."),
    Code.BUSY: (409, True, "A generation job is already running."),
    Code.RATE_LIMITED: (429, True, "Too many requests."),
    Code.INSUFFICIENT_RESOURCES: (503, True, "There is not enough free memory to start."),
    Code.OUT_OF_MEMORY: (503, True, "The job was halted because it exceeded its memory budget."),
    Code.GENERATION_FAILED: (500, False, "Speech generation failed."),
    Code.WORKER_LOST: (500, True, "The synthesis worker stopped unexpectedly."),
    Code.RUNTIME_PROVIDER_REFUSED: (
        500,
        False,
        "The inference runtime offered a non-local execution provider.",
    ),
    Code.RESULT_NOT_READY: (409, True, "The result is not finished yet."),
    Code.RESULT_EXPIRED: (410, False, "The result's lifetime has passed."),
    Code.RESULT_MISSING: (410, False, "The result file is missing."),
    Code.SEGMENT_NOT_READY: (409, True, "That segment has not been generated yet."),
    Code.UNAUTHENTICATED: (401, False, "Authentication is required."),
    Code.FORBIDDEN: (403, False, "This credential does not carry that permission."),
    Code.NOT_FOUND: (404, False, "No such item."),
    Code.HOST_NOT_ALLOWED: (400, False, "Unexpected Host header."),
    Code.ORIGIN_NOT_ALLOWED: (400, False, "Unexpected Origin header."),
    Code.CREDENTIAL_EXPIRED: (401, False, "The credential has expired."),
    Code.CREDENTIAL_REVOKED: (401, False, "The credential was revoked."),
    Code.AUTH_LOCKED_OUT: (429, True, "Too many authentication failures."),
    Code.PAYLOAD_TOO_LARGE: (413, False, "The request body exceeds 2,000,000 bytes."),
    Code.STORAGE_FULL: (507, False, "There is not enough free disk space."),
    Code.RETENTION_LIMIT_REACHED: (507, False, "The retention space limit has been reached."),
    Code.DB_LOCKED: (503, True, "The local database is busy."),
    Code.DB_UNAVAILABLE: (503, True, "The local database cannot be opened."),
    Code.BACKUP_INVALID: (422, False, "The backup is damaged."),
    Code.BACKUP_INCOMPATIBLE: (422, False, "The backup was made by an incompatible version."),
    Code.BACKUP_TOO_LARGE: (413, False, "The backup exceeds the restore limits."),
    Code.DELETE_BLOCKED_IN_USE: (409, True, "That item is in use by a running job."),
    Code.SERVICE_OFF: (503, False, "The local service is turned off."),
    Code.SERVICE_PORT_UNAVAILABLE: (503, False, "The local service could not bind its port."),
    Code.APP_NOT_RUNNING: (503, False, "EchoAct is not running."),
    Code.MCP_DISABLED: (503, False, "MCP is not enabled in EchoAct."),
    Code.SHUTTING_DOWN: (503, False, "EchoAct is shutting down."),
    Code.OUTPUT_DEVICE_LOST: (503, True, "The audio output device disappeared."),
    Code.OUTPUT_DEVICE_UNAVAILABLE: (503, True, "No audio output device is available."),
    # F-89: the owner turned external playback off, so no amount of waiting
    # changes the answer -- which is why this one is not retryable and, by
    # N-23, may not carry a hint.
    Code.PLAYBACK_NOT_ALLOWED: (
        403,
        False,
        "EchoAct is not set to play requests from an app out loud.",
    ),
    Code.PLAYBACK_BUSY: (409, True, "Someone is listening to something else right now."),
    Code.INTERNAL: (500, False, "An internal error occurred."),
}


def http_status(code: Code) -> int:
    return _CATALOGUE[code][0]


def is_retryable(code: Code) -> bool:
    return _CATALOGUE[code][1]


def default_message(code: Code) -> str:
    return _CATALOGUE[code][2]


class EchoActError(Exception):
    """The one exception type that crosses a module boundary.

    Anything raised out of a package module is either this or a programming
    error.  Surfaces translate it; they never guess a status from its text.
    """

    def __init__(
        self,
        code: Code,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        retry_after_s: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.code = code
        self.message = message or default_message(code)
        self.detail = detail or {}
        # Only a retryable code may carry a hint; N-23 forbids inviting a
        # retry that cannot succeed.
        self.retry_after_s = retry_after_s if is_retryable(code) else None
        super().__init__(f"{code.value}: {self.message}")
        if cause is not None:
            self.__cause__ = cause

    @property
    def http_status(self) -> int:
        return http_status(self.code)

    @property
    def retryable(self) -> bool:
        return is_retryable(self.code)

    def to_payload(self, request_id: str) -> dict[str, Any]:
        """F-57's wire shape.  Never carries a path, a credential, or body text."""
        body: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": request_id,
        }
        if self.detail:
            body["detail"] = self.detail
        if self.retry_after_s is not None:
            body["retry_after_s"] = round(self.retry_after_s, 3)
        return body


@dataclass(frozen=True, slots=True)
class Problem:
    """A non-fatal report shown to the user, e.g. a file that would not open."""

    code: Code
    message: str
    remedies: tuple[str, ...] = field(default_factory=tuple)

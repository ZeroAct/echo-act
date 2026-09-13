"""F-57's error envelope, and the one place an exception becomes a response.

Every rejection this service produces goes through :func:`error_response`.
That matters for three separate promises:

* F-57 fixes the shape -- a stable code, a user message, whether a retry can
  succeed, and a request identifier -- and ``EchoActError.to_payload`` already
  produces exactly that, so no route builds a body of its own.
* Section 2.10 fixes the status for each situation and ``echoact.errors``
  already maps every ``Code`` to one.  Statuses are therefore never written at
  a call site; a route raises the code that describes what happened and the
  status follows.
* N-23 requires the three retryable rejections -- busy, rate limited,
  insufficient resources -- to carry a retry-after hint, and rule 8 forbids a
  permanent one from carrying it.  ``EchoActError`` drops a hint attached to a
  permanent code, so the header written here can simply follow the payload.

Nothing here ever renders a path, a token, or body text.  ``EchoActError``'s
``detail`` is the only free-form field and every raise site in this package
puts identifiers and numbers in it.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..errors import Code, EchoActError
from ..util import ids
from ..util.logging import get_logger

log = get_logger("service.errors")

#: Where the request identifier F-57 requires is echoed.  It is a response
#: header as well as a body field so that a caller can correlate a streamed
#: audio response, which has no JSON body to put it in.
REQUEST_ID_HEADER = "X-Request-Id"

#: 4.2's leaf names inside a voice-settings object, each mapped to the code
#: Section 2.10 wants for it.  Used only when a body fails schema validation
#: before any of this package's own checks could name the problem.
_VOICE_FIELD_CODES: dict[str, Code] = {
    "model_id": Code.MODEL_UNKNOWN,
    "voice_id": Code.VOICE_UNKNOWN,
    "language": Code.LANGUAGE_UNKNOWN,
    "gender": Code.VOICE_UNKNOWN,
    "style": Code.STYLE_UNKNOWN,
    "tempo": Code.TEMPO_OUT_OF_RANGE,
}


def request_id_of(request: Request) -> str:
    """The identifier minted for this request, or a fresh one.

    A fallback is minted rather than omitted: F-57 says every error carries
    one, and an error raised before the gate stored it is exactly the kind a
    user reports.
    """
    existing = request.scope.get("state", {}).get("request_id")
    return existing if isinstance(existing, str) and existing else ids.request_id()


def error_response(exc: EchoActError, request_id: str) -> JSONResponse:
    """The wire form of a refusal.

    ``Retry-After`` is written only when the payload carries a hint, and HTTP
    fixes it as whole seconds, so a sub-second hint rounds *up*: rounding down
    would invite a retry that is still too early, which is the one thing the
    header exists to prevent.
    """
    payload = exc.to_payload(request_id)
    headers = {REQUEST_ID_HEADER: request_id}
    if exc.retry_after_s is not None:
        headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after_s)))
    return JSONResponse(payload, status_code=exc.http_status, headers=headers)


def validation_error(exc: RequestValidationError, path: str) -> EchoActError:
    """Turn a schema failure into a coded refusal.

    Section 2.10 gives malformed requests 400 and invalid voice settings 422,
    and insists a detailed code accompanies each.  ``echoact.errors`` has no
    generic "the body could not be parsed" code, so the code is derived from
    *which* field failed, and the last resort is chosen per endpoint from what
    a body arriving there must state at minimum: a job request that fails to
    parse has, among other things, stated no job kind, and an estimate request
    that fails to parse has supplied no text.  Both are true statements about
    the body rather than a stand-in for one.
    """
    errors = exc.errors()
    field_names = [str(part) for err in errors for part in err.get("loc", ())]
    unknown = sorted(
        {
            str(err["loc"][-1])
            for err in errors
            if err.get("type") == "extra_forbidden" and err.get("loc")
        }
    )
    if unknown:
        return _unknown_field_error(unknown, field_names)
    if "voice" in field_names:
        for name in field_names:
            if name in _VOICE_FIELD_CODES:
                return EchoActError(_VOICE_FIELD_CODES[name], detail={"field": name})
        return EchoActError(Code.VOICE_SETTINGS_INVALID, detail={"field": "voice"})
    if "kind" in field_names:
        return EchoActError(Code.JOB_KIND_MISSING, detail={"field": "kind"})
    if "idempotency_key" in field_names:
        return EchoActError(Code.IDEMPOTENCY_KEY_MISSING, detail={"field": "idempotency_key"})
    if "text" in field_names:
        return EchoActError(Code.INPUT_EMPTY, detail={"field": "text"})
    if path.endswith("/estimate"):
        return EchoActError(Code.INPUT_EMPTY, detail={"field": "text"})
    return EchoActError(Code.MALFORMED_REQUEST, detail={"fields": field_names})


def _unknown_field_error(unknown: list[str], field_names: list[str]) -> EchoActError:
    """F-54: an option this version does not define is refused, by name.

    The catalogue has no code for "unrecognised field", so the code is the
    same last resort the rest of the translation uses and carries the status
    Section 2.10 fixes for where the field sat -- 422 inside the voice
    settings, 400 for the body itself -- while ``detail`` names every field
    that was not understood.  Naming them is the point: a client that is not
    a person cannot compare its request against a document, and "something in
    your body is wrong" costs it the round trip without telling it what to
    change.
    """
    listed = ", ".join(unknown)
    if "voice" in field_names:
        # 422 rather than 400 because Section 2.10 fixes that status for
        # the voice settings.  The status says where the fault is; the
        # code says what it is, and they are allowed to differ.
        return EchoActError(
            Code.VOICE_SETTINGS_INVALID,
            f"The voice settings name options this version does not define: {listed}.",
            detail={"field": "voice", "unknown_fields": unknown},
        )
    return EchoActError(
        Code.UNKNOWN_OPTION,
        f"The request names fields this version does not define: {listed}.",
        detail={"unknown_fields": unknown},
    )


def http_exception_error(exc: StarletteHTTPException) -> EchoActError:
    """A routing-layer refusal, given a code.

    An unknown path and an unsupported method both answer NOT_FOUND.  405
    would tell a caller that the path exists and only the verb is wrong, and
    on a service whose whole contract is a dozen fixed operations that is a
    map of the surface for no benefit to a legitimate client.
    """
    if exc.status_code in (404, 405):
        return EchoActError(Code.NOT_FOUND, "No such endpoint.")
    if exc.status_code == 413:
        return EchoActError(Code.PAYLOAD_TOO_LARGE)
    if exc.status_code == 401:
        return EchoActError(Code.UNAUTHENTICATED)
    if exc.status_code == 403:
        return EchoActError(Code.FORBIDDEN)
    if exc.status_code == 400:
        # Starlette raises a bare 400 for a body its parsers refuse.
        # Falling through to INTERNAL reported a server fault for a
        # client's mistake, and 500 is the one status a client must not
        # be told to stop retrying on.
        return EchoActError(Code.MALFORMED_REQUEST)
    return EchoActError(Code.INTERNAL)


def install_error_handlers(app: FastAPI) -> None:
    """Route every exception class to the one envelope.

    The unexpected-exception handler answers INTERNAL and logs the type only.
    An exception's text can quote a file path or a fragment of the caller's
    own body, and F-57 keeps internal paths out of a response while N-20 keeps
    body text out of a log, so neither the client nor the log file gets it.
    """

    async def on_echoact_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, EchoActError)
        request_id = request_id_of(request)
        _log_refusal(request, exc, request_id)
        return error_response(exc, request_id)

    async def on_validation_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        translated = validation_error(exc, request.scope.get("path", ""))
        request_id = request_id_of(request)
        _log_refusal(request, translated, request_id)
        return error_response(translated, request_id)

    async def on_http_exception(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        translated = http_exception_error(exc)
        request_id = request_id_of(request)
        _log_refusal(request, translated, request_id)
        return error_response(translated, request_id)

    async def on_unexpected(request: Request, exc: Exception) -> Response:
        request_id = request_id_of(request)
        log.exception(
            "unhandled %s in %s (request=%s)",
            type(exc).__name__,
            request.scope.get("path", ""),
            request_id,
        )
        return error_response(EchoActError(Code.INTERNAL), request_id)

    app.add_exception_handler(EchoActError, on_echoact_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_exception)
    app.add_exception_handler(Exception, on_unexpected)


def _log_refusal(request: Request, exc: EchoActError, request_id: str) -> None:
    """N-25's traceable line for a refusal: identifiers and a code, nothing else.

    The path is logged without its query string.  Nothing in this contract
    puts a token in a URL (N-17), but a log line that would print one if it
    ever appeared is a defect waiting for a future endpoint.
    """
    log.info(
        "%s %s -> %d %s (request=%s)",
        request.scope.get("method", "?"),
        request.scope.get("path", "?"),
        exc.http_status,
        exc.code.value,
        request_id,
    )


def error_document(code: Code) -> dict[str, Any]:
    """One example error body, for the OpenAPI document F-57 requires."""
    return EchoActError(code).to_payload("req_example")

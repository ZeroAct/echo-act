"""Assembling the ASGI application, and the specification F-57 requires.

Three deliberate absences are worth naming, because each is a default that
had to be turned off rather than something forgotten:

* **No CORS middleware.**  N-17 disallows CORS by default, and the way to
  disallow it is to emit no ``Access-Control-Allow-*`` header at all.  The
  Origin check in :mod:`echoact.service.deps` refuses a cross-origin request
  outright; nothing here would have let a browser read a reply anyway.
* **No interactive docs.**  Swagger UI and ReDoc load their JavaScript from a
  public CDN.  N-01 says the app makes no automatic external communication
  beyond model downloads and update checks the user asked for, and a docs
  page that fetches from a CDN the moment it is opened would be exactly that.
  The machine-readable document remains, which is what F-57 actually asks for.
* **No lifespan hooks.**  The parts already exist: ``Application`` builds the
  store, the engine, and the credentials before the service is started, and
  F-79 requires the GUI to survive a service that never starts.  Wiring
  construction into the ASGI lifespan would put the app's lifetime inside the
  server's, which is the inversion A.2 rules out.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from .. import __version__
from ..errors import Code
from ..policy import API_PREFIX, API_VERSION
from .deps import ServiceContext, ServiceGate
from .errors import error_document, install_error_handlers
from .routes import build_router
from .schemas import CreateJobRequest, ErrorBody

_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

#: The refusals a caller most needs to have seen before it meets one, rendered
#: as F-57's "request/response examples".  Busy and rate-limited are here
#: because F-47 calls contention the expected case rather than an edge case,
#: and a client that has never seen the hint will not honour it.
_ERROR_EXAMPLES: tuple[Code, ...] = (
    Code.UNAUTHENTICATED,
    Code.FORBIDDEN,
    Code.NOT_FOUND,
    Code.BUSY,
    Code.IDEMPOTENCY_KEY_CONFLICT,
    Code.RATE_LIMITED,
    Code.INSUFFICIENT_RESOURCES,
)

#: The whole external contract, exactly as Section 2.10's table lists it, as
#: (method, path) pairs.  Written down here rather than derived from the
#: router so that the test asserting "these and no others" checks the code
#: against the document instead of against itself.
#:
#: Thirteen operations over twelve paths: creating a job and listing history
#: are two methods on ``/api/v1/jobs``, which is one path to OpenAPI and two
#: rows in the contract table.
CONTRACT_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("GET", f"{API_PREFIX}/status"),
    ("GET", f"{API_PREFIX}/models"),
    ("POST", f"{API_PREFIX}/estimate"),
    ("POST", f"{API_PREFIX}/jobs"),
    ("GET", f"{API_PREFIX}/jobs"),
    ("GET", f"{API_PREFIX}/jobs/{{job_id}}"),
    ("GET", f"{API_PREFIX}/jobs/{{job_id}}/text"),
    ("POST", f"{API_PREFIX}/jobs/{{job_id}}/cancel"),
    ("POST", f"{API_PREFIX}/jobs/{{job_id}}/play"),
    ("GET", f"{API_PREFIX}/jobs/{{job_id}}/segments"),
    ("GET", f"{API_PREFIX}/jobs/{{job_id}}/segments/{{segment_id}}/audio"),
    ("GET", f"{API_PREFIX}/jobs/{{job_id}}/audio"),
    ("GET", f"{API_PREFIX}/jobs/{{job_id}}/result"),
)

CONTRACT_PATHS: tuple[str, ...] = tuple(dict.fromkeys(path for _, path in CONTRACT_OPERATIONS))

_DESCRIPTION = """\
EchoAct's local generation service.  It listens on the loopback address only,
requires authentication on every path including the specification itself, and
exists only while the desktop application is running.

Authenticate with `Authorization: Bearer <token>`, using a credential issued
in the application.  Tokens never travel in a URL.

Errors carry a stable `code`, a `message`, `retryable`, and a `request_id`.
The three refusals that can succeed later -- busy, rate limited, and
insufficient resources -- also carry `retry_after_s` and a `Retry-After`
header; a permanent refusal carries neither.
"""


def create_app(context: ServiceContext) -> FastAPI:
    """Build the ASGI application for one service context."""
    app = FastAPI(
        title="EchoAct local service",
        version=__version__,
        description=_DESCRIPTION,
        openapi_url="/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    install_error_handlers(app)
    app.include_router(build_router(context), prefix=API_PREFIX)
    # Added last so it wraps everything, routing and the specification route
    # included: N-31's "no request succeeds without a valid credential" has to
    # cover the paths that are not operations as well as the ones that are.
    app.add_middleware(ServiceGate, context=context)
    app.openapi = lambda: _specification(app)  # type: ignore[method-assign]
    app.state.echoact = context
    return app


def _specification(app: FastAPI) -> dict[str, Any]:
    """F-57's machine-readable contract, cached on the app as FastAPI does.

    ``CreateJobRequest`` is merged in by hand.  POST /api/v1/jobs declares two
    alternative bodies -- inline JSON and a multipart upload -- which FastAPI
    cannot express from a signature, so the operation supplies its own
    ``requestBody`` and the model it refers to would otherwise never reach
    ``components.schemas``.  A specification that references a schema it does
    not define is not machine-readable, which is the whole point of F-57.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema["info"]["x-api-version"] = API_VERSION
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    body = CreateJobRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    for name, definition in body.pop("$defs", {}).items():
        components.setdefault(name, definition)
    components.setdefault("CreateJobRequest", body)
    components.setdefault("ErrorBody", ErrorBody.model_json_schema())
    _attach_error_responses(schema)
    for unused in ("HTTPValidationError", "ValidationError"):
        components.pop(unused, None)
    app.openapi_schema = schema
    return schema


def _attach_error_responses(schema: dict[str, Any]) -> None:
    """Give every operation F-57's error envelope, with examples.

    Attached here rather than declared on each route because the envelope is
    the same for all of them and Section 2.10 fixes the statuses centrally: a
    per-route list would be one chance per route for the document and
    ``echoact.errors`` to disagree about what a refusal looks like.
    """
    examples = {
        code.value: {"summary": code.value, "value": error_document(code)}
        for code in _ERROR_EXAMPLES
    }
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() not in _METHODS:
                continue
            responses = operation.setdefault("responses", {})
            # FastAPI adds a 422 of its own shape to every operation with a
            # parameter.  This service never answers one: a malformed request
            # is 400 with a code, and the only 422 it produces is invalid
            # voice settings, in the same envelope as every other refusal.
            # Leaving FastAPI's in would document a response that cannot
            # occur, in a schema nothing here returns.
            responses.pop("422", None)
            responses["default"] = {
                "description": (
                    "A refusal. Carries a stable code, a message, whether a retry can "
                    "succeed, and a request identifier; the retryable ones also carry "
                    "retry_after_s and a Retry-After header."
                ),
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorBody"},
                        "examples": examples,
                    }
                },
            }

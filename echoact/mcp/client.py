"""The MCP server's half of the loopback conversation.

F-58 makes the MCP server a client of the local REST service and nothing
else: no model, no database, no job state.  So this is the whole of its
machinery, and its one interesting job is translating the ways the
conversation can fail into the distinct, non-retrying errors F-52 names:

* EchoAct is not running -- the connection is refused.  Distinct because
  the remedy is to start EchoAct, and because F-52 forbids this process
  from starting it.
* REST is turned off -- there is no listener, or one that says so.
* MCP is disabled in EchoAct -- the service answers, but the owner has
  not enabled this integration.
* The credential has expired or been revoked -- authentication fails for
  a reason the user can act on, which is not the same as being wrong.

Every other failure keeps the service's own error code, because N-24
makes MCP a projection of REST and a behaviour difference between them a
defect.  Re-deriving a code here would be exactly that difference.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import Code, EchoActError, is_retryable
from .config import Connection

#: Codes that mean "this process cannot work until something changes",
#: as opposed to "try again".  F-52 wants these reported as non-retrying.
TERMINAL_CODES = frozenset(
    {
        Code.APP_NOT_RUNNING,
        Code.SERVICE_OFF,
        Code.MCP_DISABLED,
        Code.CREDENTIAL_EXPIRED,
        Code.CREDENTIAL_REVOKED,
        Code.UNAUTHENTICATED,
        Code.HOST_NOT_ALLOWED,
    }
)


class RestClient:
    """A thin, synchronous client.  Synchronous on purpose: this process
    serves one stdio conversation and has nothing to overlap with."""

    def __init__(self, connection: Connection, transport: httpx.BaseTransport | None = None):
        self.connection = connection
        self._http = httpx.Client(
            base_url=connection.api_root,
            headers=connection.headers(),
            timeout=connection.timeout_s,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RestClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._json("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._json("POST", path, json=body)

    def get_bytes(self, path: str) -> tuple[bytes, str]:
        """Fetch a result's audio.  Returns (bytes, media type).

        The MCP server resolves a resource URI on the client's behalf
        (F-60), so the bytes pass through this process rather than the
        client being handed an address -- which is what lets §2.11 keep
        local paths and tokenised URLs out of the protocol entirely.
        """
        response = self._send("GET", path, headers={"Accept": "audio/wav"})
        return response.content, response.headers.get("content-type", "audio/wav")

    # ------------------------------------------------------------------

    def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        response = self._send(method, path, **kw)
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise EchoActError(
                Code.INTERNAL, "EchoAct returned something that is not JSON.", cause=exc
            ) from exc
        return body if isinstance(body, dict) else {"result": body}

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kw)
        except httpx.ConnectError as exc:
            # The refused connection is the signal, and it is the one case
            # this process must never try to fix by starting anything.
            raise EchoActError(
                Code.APP_NOT_RUNNING,
                "EchoAct is not running, or its local service is turned off. "
                "Start EchoAct and check Settings; this server cannot start it.",
                cause=exc,
            ) from exc
        except httpx.TimeoutException as exc:
            raise EchoActError(
                Code.APP_NOT_RUNNING,
                "EchoAct did not answer on the loopback address.",
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise EchoActError(
                Code.INTERNAL, f"The local request failed ({type(exc).__name__}).", cause=exc
            ) from exc

        if response.status_code >= 400:
            raise self._error(response)
        return response

    @staticmethod
    def _error(response: httpx.Response) -> EchoActError:
        """Carry the service's own code across, rather than inventing one."""
        payload: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            pass

        raw = str(payload.get("code", "")).strip()
        try:
            code = Code(raw)
        except ValueError:
            code = _code_for_status(response.status_code)

        message = str(payload.get("message", "")) or None
        retry_after = payload.get("retry_after_s")
        if retry_after is None and "Retry-After" in response.headers:
            try:
                retry_after = float(response.headers["Retry-After"])
            except ValueError:
                retry_after = None
        detail = {k: v for k, v in payload.items() if k not in {"code", "message", "retry_after_s"}}
        return EchoActError(
            code,
            message,
            detail=detail or None,
            retry_after_s=float(retry_after) if retry_after is not None else None,
        )


def _code_for_status(status: int) -> Code:
    """Only reached when the answer carried no code of its own.

    F-57 requires every EchoAct error to carry one, so this is the case
    where something *else* is listening on the port.  Statuses whose
    meaning is unambiguous are mapped; the rest stay ``INTERNAL`` rather
    than being guessed into a precise-looking code that would then be
    wrong -- 422 in particular means "invalid voice settings" in this
    contract and nothing at all in a stranger's.
    """
    return {
        401: Code.UNAUTHENTICATED,
        403: Code.FORBIDDEN,
        404: Code.NOT_FOUND,
        410: Code.RESULT_EXPIRED,
        413: Code.PAYLOAD_TOO_LARGE,
        429: Code.RATE_LIMITED,
        503: Code.SERVICE_OFF,
        507: Code.STORAGE_FULL,
    }.get(status, Code.INTERNAL)


def describe(error: EchoActError) -> dict[str, Any]:
    """What a tool returns when the call failed.

    Keeps the service's code and adds the one thing an automated caller
    most needs and F-57 already decided: whether trying again can work.
    """
    return {
        "error": {
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable and error.code not in TERMINAL_CODES,
            **({"retry_after_s": error.retry_after_s} if error.retry_after_s else {}),
            **({"detail": error.detail} if error.detail else {}),
        }
    }


def is_terminal(error: EchoActError) -> bool:
    return error.code in TERMINAL_CODES or not is_retryable(error.code)

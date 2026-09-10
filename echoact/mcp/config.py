"""How the MCP server is told where EchoAct is and who it is.

F-58 puts the connection details in the MCP *client's* configuration and
has the app hand them to the user, so this process reads them from its
environment and its arguments and holds nothing of its own.  There is no
config file to find, no discovery, and no fallback that would let it work
without a credential -- N-31's fail-closed rule reaches here too.

The credential is read from the environment rather than from a command
line by preference: an argument is visible in the process list to every
program on the machine, and N-17 keeps tokens out of places they can be
read from.  A ``--token`` argument is still accepted, because some MCP
client configurations cannot set an environment variable, and the
docstring is the place to say which is worse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..errors import Code, EchoActError
from ..policy import REST_HOST, REST_PORT_DEFAULT

ENV_URL = "ECHOACT_URL"
ENV_TOKEN = "ECHOACT_TOKEN"
ENV_TIMEOUT = "ECHOACT_TIMEOUT_S"

DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class Connection:
    """Everything this process needs, and nothing it could invent."""

    base_url: str
    token: str
    timeout_s: float = DEFAULT_TIMEOUT_S

    @property
    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1"

    def headers(self) -> dict[str, str]:
        # The Host header is what the service validates before it looks at
        # the credential (N-17), so it has to be the loopback name the
        # service expects rather than whatever httpx would infer.
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }


def default_url() -> str:
    return f"http://{REST_HOST}:{REST_PORT_DEFAULT}"


def from_environment(argv: list[str] | None = None) -> Connection:
    """Read the connection, or refuse to start.

    A missing credential is a configuration error the user must fix, not a
    condition to retry: F-52 wants distinct, non-retrying errors for the
    ways this process can be unusable, and starting anonymously is not an
    option N-31 leaves open.
    """
    args = list(argv or [])
    token = os.environ.get(ENV_TOKEN, "")
    url = os.environ.get(ENV_URL, "") or default_url()

    for i, arg in enumerate(args):
        if arg == "--token" and i + 1 < len(args):
            token = args[i + 1]
        elif arg.startswith("--token="):
            token = arg.split("=", 1)[1]
        elif arg == "--url" and i + 1 < len(args):
            url = args[i + 1]
        elif arg.startswith("--url="):
            url = arg.split("=", 1)[1]

    if not token:
        raise EchoActError(
            Code.UNAUTHENTICATED,
            f"No EchoAct credential. Set {ENV_TOKEN} in this server's configuration; "
            "EchoAct shows the value once, under Settings.",
        )
    if not _is_loopback(url):
        # N-17 and Section 7: EchoAct is never reachable off this machine,
        # so a URL that points elsewhere is a misconfiguration to report
        # rather than an address to try.
        raise EchoActError(
            Code.HOST_NOT_ALLOWED,
            f"EchoAct only listens on the loopback address; {url!r} is not one.",
        )

    timeout = DEFAULT_TIMEOUT_S
    raw = os.environ.get(ENV_TIMEOUT)
    if raw:
        try:
            timeout = max(1.0, float(raw))
        except ValueError:
            pass
    return Connection(base_url=url, token=token, timeout_s=timeout)


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}

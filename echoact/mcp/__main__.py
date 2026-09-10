"""Entry point for the MCP server process.

Started by the MCP client, per F-58.  It never starts EchoAct -- F-52 is
explicit -- and it exits with a message rather than waiting when the app
is not there to talk to.

Diagnostics go to stderr, which the revision this implements requires:
its logging feature is deprecated and §2.11 says so.
"""

from __future__ import annotations

import sys

from ..errors import EchoActError
from .client import RestClient
from .config import from_environment
from .server import build, preflight


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        connection = from_environment(args)
    except EchoActError as exc:
        print(f"[echoact-mcp] {exc.code.value}: {exc.message}", file=sys.stderr, flush=True)
        return 2

    rest = RestClient(connection)
    try:
        preflight(rest)
    except EchoActError as exc:
        # Distinct and non-retrying, per F-52.  Exiting rather than
        # serving is the honest answer: every tool would fail the same
        # way, and a client that saw tools listed would reasonably expect
        # them to work.
        print(f"[echoact-mcp] {exc.code.value}: {exc.message}", file=sys.stderr, flush=True)
        rest.close()
        return 3

    server = build(connection, client=rest)
    try:
        server.run(transport="stdio", show_banner=False)
    finally:
        rest.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

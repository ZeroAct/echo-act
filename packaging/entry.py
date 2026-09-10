"""The frozen executable's entry point.

One executable with three jobs, because a PyInstaller bundle cannot do
what the development tree does.  ``python -m echoact.engine.worker``
works from a checkout; inside a bundle ``sys.executable`` is the frozen
binary and ``-m`` means nothing to it.  So the binary re-invokes itself
with a flag and dispatches here:

    EchoAct                 the application (F-85 makes it single-instance)
    EchoAct --worker        the synthesis child process (A.2's process layout)
    EchoAct --mcp           the stdio MCP server, started by an MCP client

The flags are not a public interface and are not documented to users;
they exist because the process layout in A.2 needs three processes and a
bundle gives us one executable to make them from.
"""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]

    if argv and argv[0] == "--worker":
        from echoact.engine.worker import main as worker_main

        return worker_main()

    if argv and argv[0] == "--mcp":
        from echoact.mcp.__main__ import main as mcp_main

        return mcp_main(argv[1:])

    from echoact.__main__ import main as app_main

    return app_main(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())

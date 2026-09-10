"""The local REST service: Section 2.10's twelve operations, and nothing else.

Import order matters here only in that ``server`` pulls in uvicorn, which the
GUI does not need until the owner actually starts the service; everything the
routes need is reachable without it.
"""

from __future__ import annotations

from .app import CONTRACT_OPERATIONS, CONTRACT_PATHS, create_app
from .deps import ServiceContext
from .server import ServiceRunner

__all__ = [
    "CONTRACT_OPERATIONS",
    "CONTRACT_PATHS",
    "ServiceContext",
    "ServiceRunner",
    "create_app",
]

"""Pinning the inference runtime to local CPU execution (F-87, N-01, N-05).

The installed ONNX Runtime on the reference machine reports

    ['AzureExecutionProvider', 'CPUExecutionProvider']

and selects a provider by the order of the list it is given.  N-01's promise
that no text is sent to an external speech service therefore does not follow
from "we never asked for Azure": it follows only from an explicit allow-list
plus a check of what the constructed session actually runs on.  Both halves
live here.

Two decisions are worth stating, because the obvious alternatives are wrong:

* **Allow-list, not deny-list.**  Naming the providers we refuse would leave
  every provider a future runtime build adds silently permitted.  The names
  in ``_REMOTE_MARKERS`` and ``_ACCELERATOR_MARKERS`` below classify a
  refusal for the diagnostic message only; refusal itself is decided by
  ``ALLOWED_PROVIDERS``, so an unrecognised provider is refused without any
  list needing to know it exists.
* **Check after construction, not before.**  A session is free to fall back
  to, or add, a provider that was not requested; the requested list is an
  intention and ``get_providers()`` is the fact.  ``assert_local_only`` reads
  the fact, in the process that holds the session.

Thread counts also come from here rather than from the runtime's own
defaults: F-87 requires them to be derived from F-20's CPU budget, and A.5
measured twenty threads running about twice as slow as two, so the cap is
not only a limit but the faster setting.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import onnxruntime as ort

from ..domain import Budget
from ..errors import Code, EchoActError

#: F-87's allow-list.  Exactly one entry: local CPU execution.
ALLOWED_PROVIDERS: Final[tuple[str, ...]] = ("CPUExecutionProvider",)

# Substrings used only to say *why* a provider was refused.  Nothing here
# grants or denies anything.
_REMOTE_MARKERS: Final[tuple[str, ...]] = ("azure", "cloud", "remote")
_ACCELERATOR_MARKERS: Final[tuple[str, ...]] = (
    "cuda",
    "tensorrt",
    "rocm",
    "migraphx",
    "dml",
    "directml",
    "coreml",
    "openvino",
    "nnapi",
    "qnn",
    "vitis",
    "cann",
    "webgpu",
    "xnnpack",
    "armnn",
    "acl",
)


@dataclass(frozen=True, slots=True)
class RuntimeReport:
    """What F-53 advertises and F-72 exports about execution providers.

    ``refused`` is the interesting field: it records that the installed
    runtime offered something we declined, which is the evidence A-23 asks
    for.  An empty ``refused`` list on another machine is equally correct.
    """

    onnxruntime_version: str
    allowed: tuple[str, ...]
    available: tuple[str, ...]
    refused: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "onnxruntime_version": self.onnxruntime_version,
            "allowed_providers": list(self.allowed),
            "available_providers": list(self.available),
            "refused_providers": list(self.refused),
        }


def available_providers() -> list[str]:
    """Everything the installed runtime offers, allowed or not."""
    return list(ort.get_available_providers())


def refused_providers() -> list[str]:
    """Offered but outside the allow-list.  Reported, never used."""
    return [p for p in available_providers() if p not in ALLOWED_PROVIDERS]


def requested_providers() -> list[str]:
    """The provider list to hand to ``InferenceSession``.

    Allow-list order is preserved, because the runtime picks by order and an
    allowed provider must never sit behind one we would refuse.
    """
    offered = set(available_providers())
    usable = [p for p in ALLOWED_PROVIDERS if p in offered]
    if not usable:
        raise EchoActError(
            Code.RUNTIME_PROVIDER_REFUSED,
            "The inference runtime offers no local CPU execution provider.",
            detail={"allowed": list(ALLOWED_PROVIDERS), "available": available_providers()},
        )
    return usable


def provider_kind(name: str) -> str:
    """A word for a refusal message: ``local``, ``remote``, ``accelerator``,
    or ``unrecognised``.  Classification never decides the refusal."""
    if name in ALLOWED_PROVIDERS:
        return "local"
    lowered = name.lower()
    if any(m in lowered for m in _REMOTE_MARKERS):
        return "remote"
    if any(m in lowered for m in _ACCELERATOR_MARKERS):
        return "accelerator"
    return "unrecognised"


def normalize_threads(intra_op_threads: int, inter_op_threads: int) -> tuple[int, int]:
    """Clamp a pair of thread counts to at least one each.

    Zero is ONNX Runtime's "decide for yourself", which is exactly what F-87
    forbids, so a nonsensical budget becomes one thread rather than an
    uncapped one.
    """
    return max(1, int(intra_op_threads)), max(1, int(inter_op_threads))


def session_options_for_threads(intra_op_threads: int, inter_op_threads: int) -> ort.SessionOptions:
    """Session options with the thread counts pinned.

    Sequential execution matches the pipeline's shape -- its four models run
    one after another -- so inter-op parallelism would buy nothing while
    making the CPU share in F-20 harder to hold.
    """
    intra, inter = normalize_threads(intra_op_threads, inter_op_threads)
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.intra_op_num_threads = intra
    opts.inter_op_num_threads = inter
    return opts


def session_options(budget: Budget) -> ort.SessionOptions:
    """F-87's thread cap, taken from the budget actually in force (F-78)."""
    return session_options_for_threads(budget.intra_op_threads, budget.inter_op_threads)


def assert_local_only(session: Any, *, component: str = "session") -> list[str]:
    """Refuse a session that runs anywhere but the allow-list.

    Reads ``get_providers()`` on the constructed session, which is what the
    runtime will actually execute on; the list passed to the constructor is
    only what was asked for.  A session that cannot say -- no method, or an
    empty list -- is refused too, because an unverifiable session is exactly
    the case N-01 cannot afford to wave through.
    """
    getter = getattr(session, "get_providers", None)
    if not callable(getter):
        raise EchoActError(
            Code.RUNTIME_PROVIDER_REFUSED,
            f"The {component} cannot report its execution providers.",
            detail={"component": component},
        )
    providers = [str(p) for p in getter()]
    if not providers:
        raise EchoActError(
            Code.RUNTIME_PROVIDER_REFUSED,
            f"The {component} reports no execution provider.",
            detail={"component": component},
        )
    refused = [p for p in providers if p not in ALLOWED_PROVIDERS]
    if refused:
        kinds = ", ".join(f"{p} ({provider_kind(p)})" for p in refused)
        raise EchoActError(
            Code.RUNTIME_PROVIDER_REFUSED,
            f"The {component} runs on a provider outside the local-only allow-list: {kinds}.",
            detail={
                "component": component,
                "refused": refused,
                "allowed": list(ALLOWED_PROVIDERS),
            },
        )
    return providers


def verify_sessions(sessions: Mapping[str, Any]) -> list[str]:
    """Check every session a loaded pipeline holds and report what is in use.

    The return value is what F-53 publishes and F-72 exports: the providers
    actually running, in first-seen order.  Verifying nothing is refused --
    a pipeline whose sessions we failed to find would otherwise pass this
    check by having no sessions to fail it.
    """
    if not sessions:
        raise EchoActError(
            Code.RUNTIME_PROVIDER_REFUSED,
            "No inference session was available to verify.",
        )
    in_use: list[str] = []
    for name, session in sessions.items():
        for provider in assert_local_only(session, component=name):
            if provider not in in_use:
                in_use.append(provider)
    return in_use


def providers_in_use(sessions: Iterable[Any]) -> list[str]:
    """``verify_sessions`` for a bare sequence, when the caller has no names."""
    return verify_sessions({f"session[{i}]": s for i, s in enumerate(sessions)})


def runtime_report() -> RuntimeReport:
    """The F-72 diagnostic record for this process's runtime."""
    available = tuple(available_providers())
    return RuntimeReport(
        onnxruntime_version=str(ort.__version__),
        allowed=ALLOWED_PROVIDERS,
        available=available,
        refused=tuple(p for p in available if p not in ALLOWED_PROVIDERS),
    )


__all__ = [
    "ALLOWED_PROVIDERS",
    "RuntimeReport",
    "assert_local_only",
    "available_providers",
    "normalize_threads",
    "provider_kind",
    "providers_in_use",
    "refused_providers",
    "requested_providers",
    "runtime_report",
    "session_options",
    "session_options_for_threads",
    "verify_sessions",
]

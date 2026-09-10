"""F-87, N-01, N-05: the runtime is pinned to local CPU by allow-list.

None of this needs the 385 MB weights: the allow-list, the refusal, and the
thread cap are all decisions about session construction, and a stand-in that
reports providers is enough to exercise them.  The one test that touches the
installed runtime asserts the measured fact this whole module exists for --
that a provider we refuse is genuinely on offer here.
"""

from __future__ import annotations

import onnxruntime as ort
import pytest

from echoact.domain import Budget
from echoact.engine import runtime
from echoact.errors import Code, EchoActError


class StubSession:
    """Anything with ``get_providers`` is enough; ONNX Runtime's own session
    is the same shape and the real one is exercised in test_worker.py."""

    def __init__(self, *providers: str) -> None:
        self._providers = list(providers)

    def get_providers(self) -> list[str]:
        return list(self._providers)


def test_the_allow_list_is_exactly_local_cpu():
    assert runtime.ALLOWED_PROVIDERS == ("CPUExecutionProvider",)


def test_the_installed_runtime_offers_a_provider_the_allow_list_refuses():
    # A.5 measured ['AzureExecutionProvider', 'CPUExecutionProvider'] here.
    # The assertion is written so that a machine offering only CPU still
    # passes: what must hold is that anything offered outside the allow-list
    # is reported as refused, never quietly used.
    available = runtime.available_providers()
    assert "CPUExecutionProvider" in available
    assert set(runtime.refused_providers()) == set(available) - set(runtime.ALLOWED_PROVIDERS)
    assert runtime.requested_providers() == ["CPUExecutionProvider"]


def test_the_list_handed_to_construction_is_the_allow_list_not_a_default():
    # F-87 pins "by an explicit allow-list ..., never by relying on a
    # default", so this list exists to be *given* to whatever builds the
    # sessions.  A caller may narrow it; the result is still an explicit
    # list and still in allow-list order.
    assert runtime.requested_providers(("CPUExecutionProvider",)) == ["CPUExecutionProvider"]
    with pytest.raises(EchoActError) as caught:
        runtime.requested_providers(("QuantumTeapotExecutionProvider",))
    assert caught.value.code is Code.RUNTIME_PROVIDER_REFUSED


def test_a_request_wider_than_the_product_allows_is_refused_and_says_why():
    with pytest.raises(EchoActError) as caught:
        runtime.resolve_allow_list(["CPUExecutionProvider", "CUDAExecutionProvider"])
    assert caught.value.code is Code.RUNTIME_PROVIDER_REFUSED
    assert caught.value.detail["refused"] == ["CUDAExecutionProvider"]
    assert "accelerator" in caught.value.message


def test_a_request_naming_nothing_we_allow_is_a_refusal_not_a_fall_back():
    # An empty list must not quietly mean "the full allow-list": the field
    # is the caller's statement of what it will accept.
    with pytest.raises(EchoActError) as caught:
        runtime.resolve_allow_list([])
    assert caught.value.code is Code.RUNTIME_PROVIDER_REFUSED
    assert runtime.resolve_allow_list(None) == runtime.ALLOWED_PROVIDERS


def test_a_caller_may_narrow_the_allow_list_and_is_then_held_to_it(monkeypatch):
    # Written against a hypothetical two-entry product list, because the
    # narrowing has to work whatever ALLOWED_PROVIDERS grows to; today's
    # single entry cannot tell "honoured" from "ignored".
    monkeypatch.setattr(
        runtime, "ALLOWED_PROVIDERS", ("CPUExecutionProvider", "XnnpackExecutionProvider")
    )
    narrowed = runtime.resolve_allow_list(["CPUExecutionProvider"])
    assert narrowed == ("CPUExecutionProvider",)

    # Allowed by the product, refused by the caller: the narrower list wins.
    session = StubSession("XnnpackExecutionProvider")
    assert runtime.assert_local_only(session) == ["XnnpackExecutionProvider"]
    with pytest.raises(EchoActError) as caught:
        runtime.assert_local_only(session, allowed=narrowed)
    assert caught.value.detail["allowed"] == ["CPUExecutionProvider"]
    with pytest.raises(EchoActError):
        runtime.verify_sessions({"vocoder_ort": session}, allowed=narrowed)
    with pytest.raises(EchoActError):
        runtime.providers_in_use([session], allowed=narrowed)


def test_a_remote_provider_is_refused_even_though_the_session_also_runs_on_cpu():
    session = StubSession("AzureExecutionProvider", "CPUExecutionProvider")
    with pytest.raises(EchoActError) as caught:
        runtime.assert_local_only(session, component="vocoder")
    assert caught.value.code is Code.RUNTIME_PROVIDER_REFUSED
    assert caught.value.detail["refused"] == ["AzureExecutionProvider"]
    assert "vocoder" in caught.value.message


def test_an_accelerator_provider_is_refused_even_when_offered():
    # N-05: the app must not occupy a GPU that happens to be present.
    for name in ("CUDAExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider"):
        with pytest.raises(EchoActError) as caught:
            runtime.assert_local_only(StubSession(name))
        assert caught.value.code is Code.RUNTIME_PROVIDER_REFUSED
        assert runtime.provider_kind(name) == "accelerator"


def test_a_provider_nobody_has_heard_of_is_refused_without_being_listed_anywhere():
    # The point of an allow-list: a runtime build shipping a new provider
    # needs no change here to be refused.
    with pytest.raises(EchoActError):
        runtime.assert_local_only(StubSession("QuantumTeapotExecutionProvider"))
    assert runtime.provider_kind("QuantumTeapotExecutionProvider") == "unrecognised"


def test_a_session_that_cannot_report_its_providers_is_refused():
    class Opaque:
        pass

    with pytest.raises(EchoActError) as caught:
        runtime.assert_local_only(Opaque())
    assert caught.value.code is Code.RUNTIME_PROVIDER_REFUSED


def test_a_session_reporting_no_provider_at_all_is_refused():
    with pytest.raises(EchoActError) as caught:
        runtime.assert_local_only(StubSession())
    assert caught.value.code is Code.RUNTIME_PROVIDER_REFUSED


def test_a_cpu_only_session_passes_and_reports_what_it_runs_on():
    assert runtime.assert_local_only(StubSession("CPUExecutionProvider")) == [
        "CPUExecutionProvider"
    ]


def test_every_session_of_a_pipeline_is_checked_not_just_the_first():
    sessions = {
        "dp_ort": StubSession("CPUExecutionProvider"),
        "vocoder_ort": StubSession("AzureExecutionProvider"),
    }
    with pytest.raises(EchoActError) as caught:
        runtime.verify_sessions(sessions)
    assert caught.value.detail["component"] == "vocoder_ort"


def test_providers_in_use_are_reported_once_each_for_f53_and_f72():
    sessions = {f"s{i}": StubSession("CPUExecutionProvider") for i in range(4)}
    assert runtime.verify_sessions(sessions) == ["CPUExecutionProvider"]
    assert runtime.providers_in_use(sessions.values()) == ["CPUExecutionProvider"]


def test_verifying_nothing_is_a_refusal_not_a_pass():
    with pytest.raises(EchoActError) as caught:
        runtime.verify_sessions({})
    assert caught.value.code is Code.RUNTIME_PROVIDER_REFUSED


def test_session_options_take_their_thread_counts_from_the_budget():
    budget = Budget(cpu_percent=20, memory_bytes=4 << 30, intra_op_threads=2, inter_op_threads=1)
    opts = runtime.session_options(budget)
    assert opts.intra_op_num_threads == 2
    assert opts.inter_op_num_threads == 1
    assert opts.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL


def test_a_zero_thread_count_becomes_one_rather_than_the_runtimes_own_default():
    # Zero means "decide for yourself" to ONNX Runtime, which is precisely
    # what F-87 refuses to leave to the runtime.
    assert runtime.normalize_threads(0, 0) == (1, 1)
    opts = runtime.session_options_for_threads(0, -3)
    assert opts.intra_op_num_threads == 1
    assert opts.inter_op_num_threads == 1


def test_the_diagnostic_report_names_the_refused_providers_for_f72():
    report = runtime.runtime_report()
    assert report.allowed == ("CPUExecutionProvider",)
    assert set(report.refused).isdisjoint(runtime.ALLOWED_PROVIDERS)
    payload = report.to_dict()
    assert payload["onnxruntime_version"] == ort.__version__
    assert payload["refused_providers"] == list(report.refused)

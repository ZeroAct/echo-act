"""F-58's registration information, as something a user can act on.

The snippets are generated rather than written down, so the things worth
testing are the ones a typo would break silently: that the command
matches how this installation actually starts its worker, that every
client's own key name is used, and that a credential nobody has issued
is a visible placeholder rather than something that looks real.
"""

from __future__ import annotations

import json
import sys

import pytest
from PySide6.QtWidgets import QApplication

from echoact import paths
from echoact.ui.mcp_setup import (
    TOKEN_PLACEHOLDER,
    McpSetupView,
    guides,
    launch_for_this_install,
)


@pytest.fixture(scope="session")
def qt() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def view(tmp_path, monkeypatch, qt) -> McpSetupView:
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    from echoact.ui import theme

    theme.apply(qt, theme.Mode.LIGHT)
    widget = McpSetupView(
        theme.LIGHT, port=8765, mcp_enabled=True, rest_enabled=True, credentials=[]
    )
    widget.resize(880, 700)
    widget.grab()
    try:
        yield widget
    finally:
        paths.data_dir.cache_clear()


# ------------------------------------------------------------- the command ---


def test_the_command_matches_how_this_install_starts_its_worker() -> None:
    """A bundle's sys.executable is the application and -m means nothing
    to it, so a snippet that prints one form for both would fail in
    exactly the case the user cannot debug."""
    from echoact.engine.supervisor import default_worker_command

    launch = launch_for_this_install(8765)
    worker = default_worker_command()
    # Compared after normalising separators: the snippet writes the path
    # with forward slashes, which Windows accepts and which keeps a JSON
    # block free of doubled backslashes.
    assert launch.command == sys.executable.replace(chr(92), "/")
    assert launch.command == worker[0].replace(chr(92), "/")
    assert "\\" not in launch.command
    if getattr(sys, "frozen", False):
        assert launch.args == ("--mcp",)
    else:
        assert launch.args == ("-m", "echoact.mcp")
        # From a checkout the client starts the process from its own
        # directory, so the package has to be findable from anywhere.
        assert "PYTHONPATH" in launch.env


def test_the_url_is_the_loopback_address_and_the_configured_port() -> None:
    launch = launch_for_this_install(9001)
    assert launch.env["ECHOACT_URL"] == "http://127.0.0.1:9001"


# -------------------------------------------------------------- the snippets ---


def _by_key(token: str) -> dict[str, object]:
    return {g.key: g for g in guides(launch_for_this_install(8765), token)}


def test_every_listed_client_gets_its_own_key_name() -> None:
    """The differences are not cosmetic: the same block under the wrong
    key is silently ignored by the client."""
    g = _by_key("T")
    assert json.loads(g["claude-desktop"].body).keys() == {"mcpServers"}
    assert json.loads(g["vscode"].body).keys() == {"servers"}
    assert json.loads(g["cursor"].body).keys() == {"mcpServers"}
    assert g["codex"].body.startswith("[mcp_servers.echoact]")
    import tomllib

    # Parsed rather than pattern-matched: it is pasted into a file Codex
    # reads, so a quoting mistake would be a support question rather than
    # anything anyone sees here.
    parsed = tomllib.loads(g["codex"].body)
    assert parsed["mcp_servers"]["echoact"]["env"]["ECHOACT_TOKEN"] == "T"
    assert g["claude-code"].body.startswith("claude mcp add echoact")


def test_vs_code_is_told_the_transport_and_the_others_infer_it() -> None:
    g = _by_key("T")
    vscode = json.loads(g["vscode"].body)["servers"]["echoact"]
    assert vscode["type"] == "stdio"
    desktop = json.loads(g["claude-desktop"].body)["mcpServers"]["echoact"]
    assert "type" not in desktop


def test_every_snippet_carries_the_credential_and_the_url() -> None:
    for guide in guides(launch_for_this_install(8765), "SECRET-VALUE"):
        assert "SECRET-VALUE" in guide.body, guide.key
        assert "ECHOACT_URL" in guide.body, guide.key


def test_the_json_snippets_are_valid_json() -> None:
    """They are pasted into a file the client parses, so a trailing comma
    would be a support question rather than an error anyone sees here."""
    g = _by_key("T")
    for key in ("claude-desktop", "vscode", "cursor"):
        json.loads(g[key].body)


def test_a_windows_path_survives_json_encoding() -> None:
    r"""A Windows command is full of backslashes, and a snippet that
    pasted C:\path\EchoAct.exe raw would produce invalid JSON."""
    launch = launch_for_this_install(8765)
    body = _by_key("T")["claude-desktop"].body
    assert json.loads(body)["mcpServers"]["echoact"]["command"] == launch.command


# ------------------------------------------------------------- the credential ---


def test_without_an_issued_credential_the_placeholder_is_obvious(view) -> None:
    """F-71 keeps only a verifier, so an existing credential cannot be
    put in a snippet. Saying so beats a plausible-looking fake that gets
    copied as-is."""
    assert view.token == TOKEN_PLACEHOLDER
    assert "PASTE" in TOKEN_PLACEHOLDER


def test_issuing_puts_the_value_into_every_tab(view) -> None:
    view.set_token("eak_real_value_123")
    for guide in guides(launch_for_this_install(8765), view.token):
        assert "eak_real_value_123" in guide.body
    assert "not be shown again" in view.credential_note.text()


def test_the_view_asks_the_window_to_issue_rather_than_issuing_itself(view) -> None:
    """The screen holds no credential store: F-71's issuing, revoking and
    expiry live in one place, and a second caller would be a second
    place to keep the rules."""
    asked: list[str] = []
    view.credential_requested.connect(asked.append)
    view.clients.setEditText("Claude Desktop")
    view.issue.click()
    assert asked == ["Claude Desktop"]


# ------------------------------------------------------------------ state ---


def test_it_says_when_mcp_is_off(qt) -> None:
    from echoact.ui import theme

    view = McpSetupView(theme.LIGHT, port=8765, mcp_enabled=False, rest_enabled=True)
    assert "off" in view.state.text().lower()
    assert view.settings_button.isVisibleTo(view)


def test_rest_being_off_is_reported_as_the_cause(qt) -> None:
    """F-46: turning REST off also makes MCP unavailable, and the
    consequence is stated where it is felt."""
    from echoact.ui import theme

    view = McpSetupView(theme.LIGHT, port=8765, mcp_enabled=True, rest_enabled=False)
    assert "local service is off" in view.state.text()


def test_when_both_are_on_it_names_the_address(qt) -> None:
    from echoact.ui import theme

    view = McpSetupView(theme.LIGHT, port=8765, mcp_enabled=True, rest_enabled=True)
    assert "127.0.0.1" in view.state.text()
    assert "8765" in view.state.text()
    assert not view.settings_button.isVisibleTo(view)


def test_there_is_a_tab_for_each_client_and_a_fallback(view) -> None:
    titles = {view.tabs.tabText(i) for i in range(view.tabs.count())}
    assert {"Claude Desktop", "Claude Code", "Codex CLI", "VS Code", "Cursor"} <= titles
    assert view.tabs.count() == 6, "a client without a tab has nowhere to send the user"

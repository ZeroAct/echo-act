"""Handing the user what their MCP client needs, per F-58.

F-58 says client registration information, including the credential, is
provided to the user by the app.  Until now it was not: the user had to
know that the executable takes ``--mcp``, that it reads ``ECHOACT_URL``
and ``ECHOACT_TOKEN``, and what shape their particular client wants that
in.  None of that is discoverable from anywhere.

One tab per client, because the differences are not cosmetic -- the key
is ``mcpServers`` in one, ``servers`` in another, and a TOML table in a
third -- and a single "here are the values, work it out" panel is what
the user already has.

Two things this screen is careful about:

* **The credential can only be shown once.**  F-71 keeps a verifier and
  nothing else, so a snippet for an existing credential cannot contain
  the real value and does not pretend to: it carries a placeholder, and
  issuing a new one from here is offered instead.  A snippet that looks
  complete but holds a token EchoAct cannot actually produce would be
  worse than one that says what is missing.
* **These are not verified clients.**  F-62 requires tool discovery,
  errors, progress, cancellation and result retrieval to be exercised on
  the actual target clients, and Section 8.1 says compatibility with
  clients not listed is not guaranteed.  None of that has happened, so
  the screen says the snippets follow each client's documented format
  rather than implying they have been tried.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..policy import MCP_PROTOCOL_REVISION, REST_HOST
from ..security.credentials import Credential
from . import icons
from .i18n import add_korean, tr
from .theme import METRICS, Palette

add_korean(
    {
        "Connect an app": "앱 연결",
        "Set up an MCP client": "MCP 클라이언트 설정",
        "MCP is off. Turn it on in Settings before an app can connect.":
            "MCP가 꺼져 있습니다. 앱이 연결하려면 설정에서 먼저 켜세요.",
        "The local service is off, so MCP is unavailable too.":
            "로컬 서비스가 꺼져 있어 MCP도 사용할 수 없습니다.",
        "MCP is on. EchoAct answers on {host}:{port}.":
            "MCP가 켜져 있습니다. 에코액트는 {host}:{port}에서 응답합니다.",
        "Open Settings": "설정 열기",
        "Credential": "자격 증명",
        "Issue a new one": "새로 발급",
        "EchoAct keeps only a verifier, so an existing credential cannot be shown again. "
        "Issue one for this app and the snippet below will contain it.":
            "에코액트는 검증 값만 저장하므로 기존 자격 증명은 다시 표시할 수 없습니다. "
            "이 앱용으로 새로 발급하면 아래 설정에 값이 채워집니다.",
        "This credential is in the snippet below and will not be shown again.":
            "이 자격 증명은 아래 설정에 포함되어 있으며 다시 표시되지 않습니다.",
        "Put this in": "넣을 위치",
        "Copy": "복사",
        "Copied": "복사됨",
        "Copy the path": "경로 복사",
        "Restart the app after saving the file.": "파일을 저장한 뒤 앱을 다시 시작하세요.",
        "These follow each client's documented format. None has been verified "
        "against the client itself yet, so treat a failure as a setup question "
        "rather than a fault in the app.":
            "각 클라이언트의 공개된 형식을 따릅니다. 아직 실제 클라이언트에서 검증하지 않았으므로, "
            "연결되지 않으면 앱의 결함이 아니라 설정 문제로 보고 확인하세요.",
        "Protocol revision {revision}. Ten tools, stdio only.":
            "프로토콜 개정판 {revision}. 도구 10개, stdio 전용.",
        "No credential yet": "아직 자격 증명 없음",
    }
)

#: What the user pastes when they have not issued a credential here.  A
#: value that looks obviously unfilled, rather than a plausible-looking
#: fake that might be copied as-is.
TOKEN_PLACEHOLDER = "PASTE-THE-CREDENTIAL-ECHOACT-SHOWED-YOU"


@dataclass(frozen=True, slots=True)
class Launch:
    """How this installation starts its own MCP server.

    A checkout and a packaged build differ: a bundle's ``sys.executable``
    is the application and ``-m`` means nothing to it, so the executable
    re-invokes itself.  Getting this wrong is the most likely reason a
    client fails to start the server, which is why the screen computes it
    rather than printing an example.
    """

    command: str
    args: tuple[str, ...]
    env: dict[str, str]

    @property
    def shell(self) -> str:
        parts = [_quote(self.command), *(_quote(a) for a in self.args)]
        return " ".join(parts)


def _quote(value: str) -> str:
    return f'"{value}"' if " " in value else value


def _portable(path: str) -> str:
    """A Windows path written with forward slashes.

    Windows accepts them wherever a path is opened or executed, and
    they avoid two problems at once. JSON would otherwise need every
    separator doubled, which is noise in something the user is meant
    to read; and a Korean-locale font draws U+005C as the won sign, so
    a path full of backslashes appears on screen as a path full of
    currency symbols and reads as a mistake in a snippet the user is
    about to trust.
    """
    return path.replace(chr(92), "/")


def launch_for_this_install(port: int) -> Launch:
    command = _portable(sys.executable)
    if getattr(sys, "frozen", False):
        args: tuple[str, ...] = ("--mcp",)
        env = {"ECHOACT_URL": f"http://{REST_HOST}:{port}"}
    else:
        args = ("-m", "echoact.mcp")
        # From a checkout the package is not installed where an arbitrary
        # working directory can find it, and an MCP client starts the
        # process from its own.
        env = {
            "ECHOACT_URL": f"http://{REST_HOST}:{port}",
            "PYTHONPATH": _portable(str(Path(__file__).resolve().parents[2])),
        }
    return Launch(command=command, args=args, env=env)


# ======================================================================
# The snippets
# ======================================================================


def _json_server(launch: Launch, token: str, *, key: str, typed: bool) -> str:
    entry: dict[str, object] = {}
    if typed:
        # VS Code names the transport explicitly; the others infer stdio.
        entry["type"] = "stdio"
    entry["command"] = launch.command
    entry["args"] = list(launch.args)
    entry["env"] = {**launch.env, "ECHOACT_TOKEN": token}
    return json.dumps({key: {"echoact": entry}}, indent=2, ensure_ascii=False)


def _toml_server(launch: Launch, token: str) -> str:
    """The environment as its own table rather than an inline one.

    Both are valid TOML; the inline form puts three long values on a
    single line, which runs off the side of any window the user reads
    it in and makes the token the part that gets cut off.
    """
    env = {**launch.env, "ECHOACT_TOKEN": token}
    args = ", ".join(json.dumps(a) for a in launch.args)
    lines = [
        "[mcp_servers.echoact]",
        f"command = {json.dumps(launch.command)}",
        f"args = [{args}]",
        "",
        "[mcp_servers.echoact.env]",
        *(f"{k} = {json.dumps(v)}" for k, v in env.items()),
    ]
    return "\n".join(lines) + "\n"


def _claude_code_cli(launch: Launch, token: str) -> str:
    env = {**launch.env, "ECHOACT_TOKEN": token}
    flags = " ".join(f"--env {k}={_quote(v)}" for k, v in env.items())
    return f"claude mcp add echoact --scope user {flags} -- {launch.shell}"


@dataclass(frozen=True, slots=True)
class ClientGuide:
    key: str
    title: str
    #: Where the file lives, per operating system.
    location: str
    body: str
    note: str = ""


def guides(launch: Launch, token: str) -> tuple[ClientGuide, ...]:
    """One entry per client, in the order a user is likely to want them."""
    windows = sys.platform == "win32"
    return (
        ClientGuide(
            key="claude-desktop",
            title="Claude Desktop",
            location=(
                r"%APPDATA%\Claude\claude_desktop_config.json"
                if windows
                else "~/Library/Application Support/Claude/claude_desktop_config.json"
            ),
            body=_json_server(launch, token, key="mcpServers", typed=False),
            note=tr("Restart the app after saving the file."),
        ),
        ClientGuide(
            key="claude-code",
            title="Claude Code",
            location=tr("Run this in a terminal"),
            body=_claude_code_cli(launch, token),
            note="Or put the Claude Desktop block above in .mcp.json at the root of a project.",
        ),
        ClientGuide(
            key="codex",
            title="Codex CLI",
            location=r"%USERPROFILE%\.codex\config.toml" if windows else "~/.codex/config.toml",
            body=_toml_server(launch, token),
        ),
        ClientGuide(
            key="vscode",
            title="VS Code",
            location=".vscode/mcp.json in a workspace, or the user-level mcp.json",
            body=_json_server(launch, token, key="servers", typed=True),
        ),
        ClientGuide(
            key="cursor",
            title="Cursor",
            location=r"%USERPROFILE%\.cursor\mcp.json" if windows else "~/.cursor/mcp.json",
            body=_json_server(launch, token, key="mcpServers", typed=False),
        ),
        ClientGuide(
            key="other",
            title=tr("Anything else"),
            location=tr("The values, for a client that is not listed"),
            body=(
                f"transport  stdio\n"
                f"command    {launch.command}\n"
                f"args       {' '.join(launch.args)}\n"
                + "".join(
                    f"env        {k}={v}\n"
                    for k, v in {**launch.env, "ECHOACT_TOKEN": token}.items()
                )
            ),
        ),
    )


# ======================================================================
# The screen
# ======================================================================


class _Snippet(QFrame):
    """A block of configuration with somewhere to put it and a Copy."""

    def __init__(self, guide: ClientGuide, palette: Palette, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Panel")
        m = METRICS
        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)

        where = QHBoxLayout()
        where.setSpacing(m.gap)
        label = QLabel(tr("Put this in"))
        label.setProperty("role", "secondary")
        where.addWidget(label)
        self.location = QLabel(guide.location)
        self.location.setProperty("role", "section")
        # A configuration path is the one place a backslash has to look
        # like a backslash; the interface face draws it as the won sign.
        self.location.setProperty("mono", True)
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.location.setWordWrap(True)
        where.addWidget(self.location, 1)
        self.copy_path = QPushButton(tr("Copy the path"))
        self.copy_path.setProperty("variant", "quiet")
        self.copy_path.clicked.connect(lambda: _to_clipboard(guide.location))
        where.addWidget(self.copy_path)
        box.addLayout(where)

        self.body = QPlainTextEdit(guide.body)
        self.body.setReadOnly(True)
        self.body.setProperty("mono", True)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.body.setAccessibleName(f"{guide.title} configuration")
        self.body.setMinimumHeight(190)
        box.addWidget(self.body, 1)

        row = QHBoxLayout()
        if guide.note:
            note = QLabel(guide.note)
            note.setProperty("role", "muted")
            note.setWordWrap(True)
            row.addWidget(note, 1)
        else:
            row.addStretch(1)
        self.copy = QPushButton("  " + tr("Copy"))
        self.copy.setProperty("variant", "primary")
        self.copy.setIcon(icons.icon("copy", palette.text_on_accent))
        self.copy.setIconSize(icons.icon_size(16))
        self.copy.clicked.connect(self._copy)
        row.addWidget(self.copy)
        box.addLayout(row)

    def _copy(self) -> None:
        _to_clipboard(self.body.toPlainText())
        self.copy.setText("  " + tr("Copied"))


def _to_clipboard(value: str) -> None:
    clipboard = QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(value)


class McpSetupView(QWidget):
    """F-58's registration information, one tab per client."""

    #: (name) -- the window issues the credential and calls ``set_token``.
    credential_requested = Signal(str)
    open_settings_requested = Signal()

    def __init__(
        self,
        palette: Palette,
        *,
        port: int,
        mcp_enabled: bool,
        rest_enabled: bool,
        credentials: list[Credential] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self._palette = palette
        self._launch = launch_for_this_install(port)
        self._token = TOKEN_PLACEHOLDER

        m = METRICS
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad_wide)
        outer.setSpacing(m.gap_wide)

        title = QLabel(tr("Set up an MCP client"))
        title.setProperty("role", "title")
        outer.addWidget(title)

        self.state = QLabel()
        self.state.setWordWrap(True)
        outer.addWidget(self.state)
        self.settings_button = QPushButton(tr("Open Settings"))
        self.settings_button.setProperty("variant", "quiet")
        self.settings_button.clicked.connect(self.open_settings_requested)
        outer.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.set_service_state(mcp_enabled=mcp_enabled, rest_enabled=rest_enabled, port=port)

        outer.addWidget(self._credential_row(credentials or []))

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 1)
        self._rebuild_tabs()

        footer = QLabel(
            tr(
                "These follow each client's documented format. None has been verified "
                "against the client itself yet, so treat a failure as a setup question "
                "rather than a fault in the app."
            )
        )
        footer.setProperty("role", "muted")
        footer.setWordWrap(True)
        outer.addWidget(footer)

        revision = QLabel(
            tr("Protocol revision {revision}. Ten tools, stdio only.").format(
                revision=MCP_PROTOCOL_REVISION
            )
        )
        revision.setProperty("role", "muted")
        outer.addWidget(revision)

    # -- credential ----------------------------------------------------

    def _credential_row(self, credentials: list[Credential]) -> QWidget:
        m = METRICS
        panel = QFrame()
        panel.setObjectName("Panel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)

        row = QHBoxLayout()
        row.setSpacing(m.gap)
        label = QLabel(tr("Credential"))
        label.setProperty("role", "section")
        row.addWidget(label)
        self.clients = QComboBox()
        self.clients.setEditable(True)
        self.clients.setAccessibleName(tr("Credential"))
        self.clients.addItems([c.name for c in credentials] or [])
        if not credentials:
            self.clients.setEditText("My MCP client")
        row.addWidget(self.clients, 1)
        self.issue = QPushButton(tr("Issue a new one"))
        self.issue.setProperty("variant", "primary")
        self.issue.clicked.connect(
            lambda: self.credential_requested.emit(self.clients.currentText().strip() or "MCP client")
        )
        row.addWidget(self.issue)
        box.addLayout(row)

        self.credential_note = QLabel(
            tr(
                "EchoAct keeps only a verifier, so an existing credential cannot be shown "
                "again. Issue one for this app and the snippet below will contain it."
            )
        )
        self.credential_note.setProperty("role", "muted")
        self.credential_note.setWordWrap(True)
        box.addWidget(self.credential_note)
        return panel

    def set_token(self, token: str) -> None:
        """Put a freshly issued credential into every snippet.

        The only moment a real value can appear here: F-71 shows it once
        and keeps a verifier, so this screen cannot fetch it later and
        does not try.
        """
        self._token = token
        self.credential_note.setText(
            tr("This credential is in the snippet below and will not be shown again.")
        )
        self.credential_note.setProperty("role", "warn")
        self.credential_note.style().unpolish(self.credential_note)
        self.credential_note.style().polish(self.credential_note)
        self._rebuild_tabs()

    @property
    def token(self) -> str:
        return self._token

    def set_service_state(self, *, mcp_enabled: bool, rest_enabled: bool, port: int) -> None:
        """F-46: turning REST off makes MCP unavailable, and the
        consequence is stated where the change is felt."""
        if not rest_enabled:
            text = tr("The local service is off, so MCP is unavailable too.")
            role = "warn"
        elif not mcp_enabled:
            text = tr("MCP is off. Turn it on in Settings before an app can connect.")
            role = "warn"
        else:
            text = tr("MCP is on. EchoAct answers on {host}:{port}.").format(
                host=REST_HOST, port=port
            )
            role = "ok"
        self.state.setText(text)
        self.state.setProperty("role", role)
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)
        self.settings_button.setVisible(role == "warn")

    # -- tabs ----------------------------------------------------------

    def _rebuild_tabs(self) -> None:
        current = self.tabs.currentIndex()
        self.tabs.clear()
        for guide in guides(self._launch, self._token):
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QScrollArea.Shape.NoFrame)
            page.setWidget(_Snippet(guide, self._palette))
            self.tabs.addTab(page, guide.title)
        if 0 <= current < self.tabs.count():
            self.tabs.setCurrentIndex(current)

"""Showing a credential the one time it exists, per F-71.

The app stores only a verifier, from which the credential cannot be
recovered, so this dialog is the single moment the value is ever visible.
Everything about it follows from that:

* It says so, plainly, before the user closes it.  A dialog that looks
  like every other confirmation invites the click that loses the value.
* Copying is one button, because the alternative is a user retyping 44
  random characters and blaming the app when it does not work.
* The value is never logged and never written anywhere by this dialog.
  N-17 keeps tokens out of logs and backups, and the clipboard is the
  user's own choice rather than ours.

A reissue is offered where it is needed rather than described: F-71 says
a lost credential is reissued, and a user who has lost one is not in a
position to read about it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .i18n import add_korean, tr
from .theme import METRICS, Palette

add_korean(
    {
        "Credential for {name}": "{name}의 자격 증명",
        "This is the only time this value is shown.":
            "이 값은 지금 한 번만 표시됩니다.",
        "EchoAct keeps only a verifier, so it cannot show this again. "
        "If it is lost, issue a new one for the same client.":
            "에코액트는 검증 값만 저장하므로 이 값을 다시 표시할 수 없습니다. "
            "잃어버린 경우 같은 클라이언트에 새로 발급하세요.",
        "Copy": "복사",
        "Copied": "복사됨",
        "Done": "완료",
        "Give this to the client, not to a person.":
            "이 값은 사람이 아니라 클라이언트에 전달하세요.",
    }
)


class CredentialDialog(QDialog):
    """One credential, once."""

    def __init__(self, issued, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._token = issued.token
        m = METRICS

        self.setWindowTitle(
            tr("Credential for {name}").format(name=issued.credential.name)
        )
        self.setModal(True)
        self.setMinimumWidth(560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad_wide)
        outer.setSpacing(m.gap_wide)

        head = QHBoxLayout()
        head.setSpacing(m.gap)
        mark = QLabel()
        mark.setPixmap(icons.pixmap("warning", palette.warn, 20, self.devicePixelRatioF()))
        mark.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(mark)
        warning = QLabel(tr("This is the only time this value is shown."))
        warning.setProperty("role", "section")
        warning.setWordWrap(True)
        head.addWidget(warning, 1)
        outer.addLayout(head)

        # Read-only rather than a label: a label cannot be selected with the
        # keyboard, and N-30 wants the basic features usable without a mouse.
        self.value = QPlainTextEdit(self._token)
        self.value.setReadOnly(True)
        self.value.setProperty("mono", True)
        self.value.setFixedHeight(64)
        self.value.setAccessibleName(tr("Credential"))
        outer.addWidget(self.value)

        explain = QLabel(
            tr(
                "EchoAct keeps only a verifier, so it cannot show this again. "
                "If it is lost, issue a new one for the same client."
            )
        )
        explain.setProperty("role", "muted")
        explain.setWordWrap(True)
        outer.addWidget(explain)

        hint = QLabel(tr("Give this to the client, not to a person."))
        hint.setProperty("role", "muted")
        outer.addWidget(hint)

        buttons = QDialogButtonBox()
        self.copy_button = QPushButton("  " + tr("Copy"))
        self.copy_button.setIcon(icons.icon("copy", palette.text_on_accent))
        self.copy_button.setIconSize(icons.icon_size(16))
        self.copy_button.setProperty("variant", "primary")
        self.copy_button.clicked.connect(self._copy)
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        done = buttons.addButton(tr("Done"), QDialogButtonBox.ButtonRole.AcceptRole)
        done.setProperty("variant", "quiet")
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._token)
        self.copy_button.setText("  " + tr("Copied"))


def show_credential(parent: QWidget | None, issued, palette: Palette) -> None:
    dialog = CredentialDialog(issued, palette, parent)
    dialog.exec()

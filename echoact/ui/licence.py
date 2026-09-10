"""Presenting a model's use restrictions as terms, per N-11 and F-80.

N-11 makes this a release blocker rather than a courtesy: where a model's
licence obliges a distributor to bind its own users to the same use
restrictions, the product's terms must carry them through to the end user,
and shipping such a model without that is a blocker in as many words.
F-80 fixes when: presented as terms the user accepts *before* that model is
first prepared, not merely filed among the notices.

So this is a dialog with two buttons and no default.  It quotes the
restrictions in the licence's own wording -- a paraphrase is what a reader
would then hold the distributor to instead of the real term -- and it does
not let the accept button be the one the Enter key presses, because a term
accepted by reflex has not been accepted.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..models.manifest import ModelEntry
from . import icons
from .i18n import add_korean, bytes_size, tr
from .theme import METRICS, Palette

add_korean(
    {
        "Before {model} is prepared": "{model} 준비 전에",
        "This model is published under {licence}. Using it means accepting these "
        "restrictions, which EchoAct is obliged to pass on to you.":
            "이 모델은 {licence} 라이선스로 배포됩니다. 사용하려면 아래 제한 사항에 동의해야 하며, "
            "에코액트는 이를 사용자에게 그대로 전달할 의무가 있습니다.",
        "You may not use the model to:": "다음 목적으로 모델을 사용할 수 없습니다:",
        "Also worth knowing": "함께 알아 두어야 할 사항",
        "Why you are being asked": "이 확인을 요청하는 이유",
        "I accept these restrictions": "제한 사항에 동의합니다",
        "Not now": "지금은 아니오",
        "Download size {size}": "다운로드 크기 {size}",
        "The full licence is in {file} in the model's repository.":
            "전체 라이선스는 모델 저장소의 {file} 파일에 있습니다.",
    }
)


class LicenceDialog(QDialog):
    """Accept or decline one model's use restrictions."""

    def __init__(self, entry: ModelEntry, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entry = entry
        terms = entry.license
        m = METRICS

        self.setWindowTitle(tr("Before {model} is prepared").format(model=entry.display_name))
        self.setModal(True)
        self.setMinimumSize(560, 460)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad_wide)
        outer.setSpacing(m.gap_wide)

        head = QHBoxLayout()
        head.setSpacing(m.gap)
        mark = QLabel()
        mark.setPixmap(icons.pixmap("warning", palette.warn, 22, self.devicePixelRatioF()))
        mark.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(mark)
        intro = QLabel(
            tr(
                "This model is published under {licence}. Using it means accepting these "
                "restrictions, which EchoAct is obliged to pass on to you."
            ).format(licence=terms.name)
        )
        intro.setWordWrap(True)
        head.addWidget(intro, 1)
        outer.addLayout(head)

        body = QWidget()
        inner = QVBoxLayout(body)
        inner.setContentsMargins(0, 0, m.gap, 0)
        inner.setSpacing(m.gap)

        inner.addWidget(self._section(tr("You may not use the model to:")))
        for restriction in terms.restrictions:
            inner.addWidget(self._bullet(restriction))

        if terms.notes:
            inner.addWidget(self._section(tr("Also worth knowing")))
            for note in terms.notes:
                inner.addWidget(self._bullet(note))

        inner.addWidget(self._section(tr("Why you are being asked")))
        inner.addWidget(self._bullet(terms.pass_through_obligation))
        inner.addWidget(
            self._muted(
                tr("The full licence is in {file} in the model's repository.").format(
                    file=terms.source_file
                )
            )
        )
        inner.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        outer.addWidget(
            self._muted(
                tr("Download size {size}").format(size=bytes_size(entry.total_bytes))
            )
        )

        buttons = QDialogButtonBox()
        self.decline = buttons.addButton(
            tr("Not now"), QDialogButtonBox.ButtonRole.RejectRole
        )
        self.accept_button = buttons.addButton(
            tr("I accept these restrictions"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        # Declining is the default. Accepting a licence must be an act, and
        # a dialog whose accept button answers the Enter key turns it into a
        # reflex.
        self.accept_button.setDefault(False)
        self.accept_button.setAutoDefault(False)
        self.decline.setDefault(True)
        self.decline.setProperty("variant", "quiet")
        self.accept_button.setProperty("variant", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _section(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setProperty("role", "section")
        lb.setWordWrap(True)
        return lb

    @staticmethod
    def _bullet(text: str) -> QLabel:
        lb = QLabel("·  " + text)
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return lb

    @staticmethod
    def _muted(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setProperty("role", "muted")
        lb.setWordWrap(True)
        return lb

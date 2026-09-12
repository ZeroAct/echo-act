"""The controls around the reading surface.

Two panels, both deliberately dumb: they show state and emit intent, and
neither one calls the engine.  The window owns every decision, so there is
one place where "can this be started right now?" is answered -- which is
what N-09's "unavailable controls are disabled" needs to be true rather
than mostly true.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..domain import Gender, JobState, Language, SpeakingStyle, VoiceSettings
from ..models.manifest import ModelEntry
from ..policy import TEMPO_MAX, TEMPO_MIN
from . import icons
from .i18n import add_korean, duration, tr
from .theme import METRICS, Palette

add_korean(
    {
        "Reading": "읽기",
        "While reading": "읽는 동안",
        "Recent": "최근 항목",
        "Ready": "준비됨",
        "not prepared": "준비되지 않음",
        "Open file": "파일 열기",
        "Save audio": "오디오 저장",
        "Save the part generated so far": "지금까지 생성된 부분 저장",
    }
)


def label(text: str, role: str | None = None) -> QLabel:
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
    return lb


def separator() -> QFrame:
    f = QFrame()
    f.setProperty("role", "separator")
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFixedHeight(1)
    return f


class _WheelGuard(QObject):
    """Sends the wheel to the page instead of into the value.

    Qt gives a wheel event to the widget under the pointer, so a spin box,
    a combo box, or a slider inside a scrolling panel eats the scroll and
    changes its own value -- which is how someone reading down the settings
    screen silently sets their processor share to 45%.  N-30 wants the panel
    reachable by scrolling, and a control that swallows the gesture takes
    that away twice: the page does not move and something was changed.

    Deliberately not "never respond to the wheel": once the control has
    focus the gesture is aimed at it, and adjusting a focused spin box by
    wheel is the behaviour of every other desktop app.  Keyboard operation
    is untouched either way.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() is not QEvent.Type.Wheel:
            return False
        widget = watched if isinstance(watched, QWidget) else None
        if widget is None or widget.hasFocus():
            return False
        area = _scroll_area(widget)
        if area is None:
            return False
        # Hand the same event to the panel's viewport.  The filter is
        # installed on value controls only, never on a viewport, so this
        # cannot come back round.
        QApplication.sendEvent(area.viewport(), event)
        return True


_WHEEL_GUARD = _WheelGuard()


def _scroll_area(widget: QWidget) -> QAbstractScrollArea | None:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def guard_wheel(*widgets: QWidget) -> None:
    """Keep these controls from eating the page's scroll.

    Called after a panel is built, with the controls that have a value a
    wheel would change.  One shared filter object rather than one per
    widget: it holds no state.
    """
    for widget in widgets:
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        widget.installEventFilter(_WHEEL_GUARD)


def tool_button(name: str, palette: Palette, *, size: int = 18) -> QToolButton:
    """An icon button that always has an accessible name (N-30)."""
    b = QToolButton()
    b.setIcon(icons.icon(name, palette.text_secondary, palette.text_muted, size))
    b.setIconSize(icons.icon_size(size))
    text = tr(icons.accessible_name(name))
    b.setToolTip(text)
    b.setAccessibleName(text)
    b.setAccessibleDescription(text)
    b.setFixedSize(34, 34)
    b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return b


class VoicePanel(QFrame):
    """F-04 to F-08, F-83, F-30 -- everything the user picks before reading.

    Emits :attr:`changed` with a complete :class:`VoiceSettings`, never a
    partial edit, so the window never has to assemble one from widgets.
    """

    changed = Signal(object)  # VoiceSettings
    autoplay_toggled = Signal(bool)
    follow_toggled = Signal(bool)

    def __init__(self, palette: Palette, entry: ModelEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self._palette = palette
        self._entry = entry
        self._muted = False  # guards against emitting while populating

        m = METRICS
        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)

        box.addWidget(label(tr("Voice"), "section"))

        self.language = QComboBox()
        for lang, text in (
            (Language.AUTO, tr("Automatic")),
            (Language.KO, tr("Korean")),
            (Language.EN, tr("English")),
        ):
            self.language.addItem(text, lang)
        self._row(box, tr("Language"), self.language)

        self.gender = QComboBox()
        self.gender.addItem(tr("Female"), Gender.FEMALE)
        self.gender.addItem(tr("Male"), Gender.MALE)
        self._row(box, tr("Gender"), self.gender)

        self.voice = QComboBox()
        self._row(box, tr("Voice"), self.voice)

        self.style = QComboBox()
        for style, text in (
            (SpeakingStyle.NATURAL, tr("Natural")),
            (SpeakingStyle.CALM, tr("Calm")),
            (SpeakingStyle.BRIGHT, tr("Bright")),
            (SpeakingStyle.NARRATION, tr("Narration")),
        ):
            self.style.addItem(text, style)
        self._row(box, tr("Style"), self.style)

        # Tempo is a slider rather than a spin box: it is the one setting a
        # user adjusts by ear, and a slider invites the small nudges that
        # takes.  The value is shown beside it because F-07 fixes a range
        # the user is entitled to see exactly.
        tempo_row = QHBoxLayout()
        tempo_row.setSpacing(m.gap)
        tempo_row.addWidget(label(tr("Tempo"), "secondary"))
        tempo_row.addStretch(1)
        self.tempo_value = label("1.00x", "secondary")
        tempo_row.addWidget(self.tempo_value)
        box.addLayout(tempo_row)

        self.tempo = QSlider(Qt.Orientation.Horizontal)
        self.tempo.setRange(int(TEMPO_MIN * 100), int(TEMPO_MAX * 100))
        self.tempo.setSingleStep(5)
        self.tempo.setPageStep(10)
        self.tempo.setValue(100)
        self.tempo.setAccessibleName(tr("Tempo"))
        box.addWidget(self.tempo)

        box.addWidget(separator())
        box.addWidget(label(tr("While reading"), "section"))

        self.autoplay = QCheckBox(tr("Play as soon as ready"))
        self.autoplay.setChecked(True)
        box.addWidget(self.autoplay)

        self.follow = QCheckBox(tr("Follow the reading position"))
        self.follow.setChecked(True)
        box.addWidget(self.follow)

        box.addWidget(separator())
        # F-22 and N-09: the budget in force is on the main screen, not
        # only behind the settings button.
        box.addWidget(label(tr("Resource budget"), "secondary"))
        self.resources = label("", "muted")
        self.resources.setWordWrap(True)
        box.addWidget(self.resources)

        self.setFixedWidth(272)
        # Hugs its content: a card that stretches to the window height ends
        # in a large empty panel, which reads as something missing.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)

        self._populate_voices(Gender.FEMALE)
        self.language.currentIndexChanged.connect(self._emit)
        self.gender.currentIndexChanged.connect(self._on_gender)
        self.voice.currentIndexChanged.connect(self._emit)
        self.style.currentIndexChanged.connect(self._emit)
        self.tempo.valueChanged.connect(self._on_tempo)
        self.autoplay.toggled.connect(self.autoplay_toggled)
        self.follow.toggled.connect(self.follow_toggled)

    def _row(self, box: QVBoxLayout, name: str, widget: QWidget) -> None:
        box.addWidget(label(name, "secondary"))
        widget.setAccessibleName(name)
        box.addWidget(widget)

    def _populate_voices(self, gender: Gender) -> None:
        was = self._muted
        self._muted = True
        self.voice.clear()
        for v in self._entry.voices_for(gender):
            self.voice.addItem(v.display_name, v.voice_id)
            # F-53 puts the description in front of a person too, not only
            # in the API answer.
            self.voice.setItemData(self.voice.count() - 1, v.description, Qt.ItemDataRole.ToolTipRole)
        self._muted = was

    def _on_gender(self) -> None:
        self._populate_voices(Gender(self.gender.currentData()))
        self._emit()

    def _on_tempo(self, value: int) -> None:
        self.tempo_value.setText(f"{value / 100:.2f}x")
        self._emit()

    def _emit(self) -> None:
        if self._muted:
            return
        self.changed.emit(self.settings())

    def settings(self) -> VoiceSettings:
        # Qt returns item data as a plain str even for a StrEnum, so the
        # types are restored here rather than left to surprise a caller
        # that asks for ``.value``.
        return VoiceSettings(
            model_id=self._entry.model_id,
            language=Language(self.language.currentData()),
            gender=Gender(self.gender.currentData()),
            voice_id=self.voice.currentData() or self._entry.voices[0].voice_id,
            style=SpeakingStyle(self.style.currentData()),
            tempo=self.tempo.value() / 100,
        )

    def apply(self, settings: VoiceSettings, *, autoplay: bool, follow: bool) -> None:
        """Show remembered settings without emitting a change (F-24)."""
        self._muted = True
        try:
            self._select(self.language, settings.language)
            self._select(self.gender, settings.gender)
            self._populate_voices(settings.gender)
            self._select(self.voice, settings.voice_id)
            self._select(self.style, settings.style)
            self.tempo.setValue(int(round(settings.tempo * 100)))
            self.tempo_value.setText(f"{settings.tempo:.2f}x")
            self.autoplay.setChecked(autoplay)
            self.follow.setChecked(follow)
        finally:
            self._muted = False

    @staticmethod
    def _select(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def set_resource_summary(self, text: str) -> None:
        self.resources.setText(text)

    def set_locked(self, locked: bool) -> None:
        """F-10: an in-progress job's settings do not change.

        The controls are disabled rather than merely ignored, because N-09
        requires an unavailable control to look unavailable.
        """
        for w in (self.language, self.gender, self.voice, self.style, self.tempo):
            w.setEnabled(not locked)


class TransportBar(QWidget):
    """F-12 to F-15: start, pause, stop, seek, and cancel.

    The seek slider shows *playable* length, not the document's estimated
    length: F-14 forbids seeking into audio that does not exist, and a
    slider whose track runs past the end invites exactly that.
    """

    read_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()
    cancel_requested = Signal()
    seek_requested = Signal(int)  # milliseconds

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._scrubbing = False
        self._playable_ms = 0

        m = METRICS
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(m.gap)

        self.read = QPushButton("  " + tr("Read aloud"))
        self.read.setProperty("variant", "primary")
        self.read.setIcon(icons.icon("play", palette.text_on_accent))
        self.read.setIconSize(icons.icon_size(16))
        self.read.setAccessibleName(tr("Read aloud"))
        self.read.setShortcut("Ctrl+Return")
        self.read.setToolTip(tr("Read aloud") + "  (Ctrl+Enter)")
        row.addWidget(self.read)

        self.pause = tool_button("pause", palette)
        self.stop = tool_button("stop", palette)
        row.addWidget(self.pause)
        row.addWidget(self.stop)

        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 0)
        self.position.setAccessibleName(tr("Playback position"))
        self.position.setEnabled(False)
        row.addWidget(self.position, 1)

        self.time = label("0:00 / 0:00", "secondary")
        self.time.setMinimumWidth(96)
        self.time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.time)

        self.read.clicked.connect(self.read_requested)
        self.pause.clicked.connect(self.pause_requested)
        self.stop.clicked.connect(self.stop_requested)
        self.position.sliderPressed.connect(self._begin_scrub)
        self.position.sliderReleased.connect(self._end_scrub)

    def _begin_scrub(self) -> None:
        self._scrubbing = True

    def _end_scrub(self) -> None:
        self._scrubbing = False
        self.seek_requested.emit(self.position.value())

    @property
    def scrubbing(self) -> bool:
        return self._scrubbing

    def set_playable(self, ms: int) -> None:
        self._playable_ms = ms
        self.position.setEnabled(ms > 0)
        if not self._scrubbing:
            self.position.setRange(0, max(0, ms))

    def set_position(self, ms: int) -> None:
        if not self._scrubbing:
            self.position.setValue(min(ms, self._playable_ms))
        self.time.setText(f"{duration(ms)} / {duration(self._playable_ms)}")

    def set_generating(self, generating: bool) -> None:
        """The primary button is start or cancel, never both.

        F-69 makes cancelling reachable in one place, and A.3 makes
        cancelling the owner's way to reclaim the slot, so it belongs on
        the button the eye already goes to.
        """
        if generating:
            self.read.setText("  " + tr("Cancel generation"))
            self.read.setIcon(icons.icon("close", self._palette.text_on_accent))
            self.read.setAccessibleName(tr("Cancel generation"))
        else:
            self.read.setText("  " + tr("Read aloud"))
            self.read.setIcon(icons.icon("play", self._palette.text_on_accent))
            self.read.setAccessibleName(tr("Read aloud"))

    def set_playback_state(self, state: JobState | str, *, playing: bool) -> None:
        name = "pause" if playing else "play"
        self.pause.setIcon(
            icons.icon(name, self._palette.text_secondary, self._palette.text_muted, 18)
        )
        text = tr(icons.accessible_name(name))
        self.pause.setToolTip(text)
        self.pause.setAccessibleName(text)

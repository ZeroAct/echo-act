"""The visual language: one palette, one type scale, one stylesheet.

The product is a reading surface with controls around it, so the design goal
is that the text looks like a page and everything else recedes.  Three rules
follow from that and from the requirements:

* The reading surface carries no colour of its own.  N-13 forbids
  distinguishing the reading position by colour alone, and the emphasis
  mechanism is a glyph outline, so the page stays plain and the outline is
  the only mark on it.
* Nothing in the chrome moves when state changes.  A control that resizes
  when it becomes active drags the eye away from the text, and N-13's layout
  stability is about the same instinct.
* Every colour is a token here.  A widget that hard-codes a colour cannot
  follow the light/dark switch, and there is no second place to look.

Colours are chosen for contrast first: body text against the page is at
least 12:1 in both themes, secondary text at least 5.5:1, and the accent
against its own background at least 4.5:1, which is what N-30's
accessibility floor needs once colour is not the only signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication

from . import icons


class Mode(StrEnum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Palette:
    """Semantic colours.  Widgets name the role, never the hue."""

    # Surfaces, from furthest back to closest.
    canvas: str  # window background behind everything
    surface: str  # panels, toolbars, cards
    page: str  # the reading surface itself
    raised: str  # hover / pressed / selected fill
    border: str  # 1px separations
    border_strong: str  # focus ring, active outline

    # Text.
    text: str
    text_secondary: str
    text_muted: str
    text_on_accent: str

    # One accent.  A second one would compete with the reading emphasis.
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str  # tinted fill for selected rows

    # Status.  Used with an icon or a word, never alone (N-30).
    ok: str
    warn: str
    danger: str

    # The reading emphasis outline (N-13).  Deliberately near-black rather
    # than the accent: the mark means "here", not "special".
    emphasis: str
    #: Where playback is waiting on a segment that is not generated yet
    #: (Section 5.2 distinguishes that state, F-28 keeps the last position).
    emphasis_waiting: str

    is_dark: bool


LIGHT = Palette(
    canvas="#f4f4f2",
    surface="#ffffff",
    page="#fdfdfc",
    raised="#ececeb",
    border="#e2e2df",
    border_strong="#c6c6c1",
    text="#1a1a19",
    text_secondary="#5c5c58",
    text_muted="#8b8b85",
    text_on_accent="#ffffff",
    accent="#3f4bd6",
    accent_hover="#4d58e0",
    accent_pressed="#333dbb",
    accent_soft="#e8eaff",
    ok="#1f7a4d",
    warn="#9a6300",
    danger="#b3261e",
    emphasis="#111827",
    emphasis_waiting="#8b8b85",
    is_dark=False,
)

DARK = Palette(
    canvas="#161615",
    surface="#1e1e1d",
    page="#1a1a19",
    raised="#2a2a28",
    border="#2f2f2d",
    border_strong="#4a4a47",
    text="#eeeeec",
    text_secondary="#a8a8a3",
    text_muted="#77776f",
    text_on_accent="#ffffff",
    accent="#7c86f5",
    accent_hover="#8e97ff",
    accent_pressed="#6a74e6",
    accent_soft="#26284a",
    ok="#4bbd85",
    warn="#d9a441",
    danger="#f0837b",
    emphasis="#f5f5f3",
    emphasis_waiting="#77776f",
    is_dark=True,
)


@dataclass(frozen=True, slots=True)
class Metrics:
    """Spacing, radius, and the type scale.

    A single 4px grid.  Every margin and gap in the app is one of these, so
    density is one edit rather than a hunt.
    """

    unit: int = 4
    gap_tight: int = 4
    gap: int = 8
    gap_wide: int = 12
    pad: int = 12
    pad_wide: int = 16
    pad_page: int = 28

    radius_small: int = 6
    radius: int = 8
    radius_large: int = 12

    control_height: int = 32
    control_height_large: int = 40

    # Type sizes in points.  The reading size is set apart because it is the
    # only one the user is looking at for minutes at a time.
    size_reading: int = 12
    size_body: int = 10
    size_small: int = 9
    size_title: int = 15
    size_section: int = 11

    #: Reading line spacing as a percentage.  Korean needs more than Latin
    #: because Hangul syllable blocks fill the em box, but much past 160 the
    #: lines stop reading as a paragraph and start reading as a list.
    reading_line_height_pct: int = 158
    #: Space between paragraphs, on top of the line height.  Separating the
    #: two means a blank line in the source does not inherit the full
    #: reading leading twice over.
    reading_paragraph_gap: int = 10
    #: A blank line between paragraphs gets this instead, so one visual gap
    #: does not cost three line heights.
    reading_blank_line_height_pct: int = 70


METRICS = Metrics()


# Font selection.
#
# A.2 wants one bundled Korean and Latin family so that metrics -- and
# therefore N-12's synchronisation measurements and N-30's layout checks --
# are identical on Windows and macOS.  Until that family is bundled the app
# falls back to the best per-OS pair, which is correct to look at but means
# a measurement taken on one machine does not transfer to another.  The
# fallback is ordered, not a guess: the first entry present wins.
BUNDLED_FAMILY = "Pretendard"  # SIL OFL; not yet bundled, see A.4
_FALLBACKS = (
    "Pretendard",
    "Malgun Gothic",  # Windows Korean, ships with the OS
    "Apple SD Gothic Neo",  # macOS Korean
    "Noto Sans KR",
    "Segoe UI",
    "Helvetica Neue",
)
_MONO_FALLBACKS = ("JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono", "Menlo")


def resolve_family(candidates: tuple[str, ...] = _FALLBACKS) -> str:
    """First installed family, or Qt's default if none of them are."""
    installed = set(QFontDatabase.families())
    for name in candidates:
        if name in installed:
            return name
    return QFont().family()


def reading_font(family: str | None = None) -> QFont:
    """The font of the reading surface.

    Hinting is set to ``PreferFullHinting`` off and subpixel positioning left
    to the platform: the outline emphasis in N-13 draws a pen around existing
    glyph shapes, so anything that snaps glyph advances differently between
    the plain and emphasised paint would reintroduce the movement the
    requirement forbids.
    """
    f = QFont(family or resolve_family(), METRICS.size_reading)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    f.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    return f


def ui_font(family: str | None = None, size: int | None = None) -> QFont:
    return QFont(family or resolve_family(), size or METRICS.size_body)


def mono_font(size: int | None = None) -> QFont:
    return QFont(resolve_family(_MONO_FALLBACKS), size or METRICS.size_small)


def system_prefers_dark() -> bool:
    """Qt 6.5+ reports the OS colour scheme; older builds guess from the
    default window colour."""
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is not None:
        return scheme() == Qt.ColorScheme.Dark
    window = QGuiApplication.palette().window().color()
    return window.lightness() < 128


def palette_for(mode: Mode) -> Palette:
    if mode is Mode.SYSTEM:
        return DARK if system_prefers_dark() else LIGHT
    return DARK if mode is Mode.DARK else LIGHT


def qcolor(hex_value: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_value)
    if alpha < 1.0:
        c.setAlphaF(alpha)
    return c


def stylesheet(p: Palette, m: Metrics = METRICS, family: str | None = None) -> str:
    """The whole application stylesheet.

    Written as one string rather than per-widget so that a widget cannot
    quietly disagree with the theme, and so the light/dark switch is a single
    ``setStyleSheet`` call.

    Focus is a visible ring everywhere, because N-30 requires the basic
    features to be usable by keyboard and an invisible focus makes that
    false in practice.

    Four marks -- the combo arrow, the spin arrows, and the check mark --
    can only reach a Qt stylesheet as a URL, so they are rendered to files
    per theme rather than drawn.  Styling an indicator without supplying
    its mark is the common Qt mistake: the border and fill follow the
    theme and the check simply vanishes.
    """
    fam = family or resolve_family()
    key = "dark" if p.is_dark else "light"
    a = icons.stylesheet_assets(
        key,
        {
            "chevron": ("chevron-down", p.text_secondary, 14),
            "chevron_disabled": ("chevron-down", p.text_muted, 14),
            "up": ("chevron-up", p.text_secondary, 12),
            "down": ("chevron-down", p.text_secondary, 12),
            "check": ("check", p.text_on_accent, 14),
            "search": ("search", p.text_muted, 14),
        },
    )
    return f"""
* {{
    font-family: "{fam}";
    font-size: {m.size_body}pt;
    color: {p.text};
    outline: none;
}}

QWidget#Root, QMainWindow, QDialog {{
    background: {p.canvas};
}}

QWidget#Panel, QFrame#Panel {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: {m.radius}px;
}}

QLabel[role="title"] {{
    font-size: {m.size_title}pt;
    font-weight: 600;
    color: {p.text};
}}
QLabel[role="section"] {{
    font-size: {m.size_section}pt;
    font-weight: 600;
    color: {p.text};
}}
QLabel[role="secondary"] {{ color: {p.text_secondary}; }}
QLabel[role="muted"] {{ color: {p.text_muted}; font-size: {m.size_small}pt; }}
QLabel[role="ok"] {{ color: {p.ok}; }}
QLabel[role="warn"] {{ color: {p.warn}; }}
QLabel[role="danger"] {{ color: {p.danger}; }}

/* ---- buttons ------------------------------------------------------- */
QPushButton {{
    background: {p.surface};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_small}px;
    padding: 0 {m.pad}px;
    min-height: {m.control_height}px;
    color: {p.text};
}}
QPushButton:hover:!disabled {{ background: {p.raised}; }}
QPushButton:pressed:!disabled {{ background: {p.border}; }}
QPushButton:focus {{ border: 1px solid {p.accent}; }}
QPushButton:disabled {{ color: {p.text_muted}; border-color: {p.border}; }}

QPushButton[variant="primary"] {{
    background: {p.accent};
    border: 1px solid {p.accent};
    color: {p.text_on_accent};
    font-weight: 600;
    min-height: {m.control_height_large}px;
    padding: 0 {m.pad_wide}px;
}}
QPushButton[variant="primary"]:hover:!disabled {{
    background: {p.accent_hover}; border-color: {p.accent_hover};
}}
QPushButton[variant="primary"]:pressed:!disabled {{
    background: {p.accent_pressed}; border-color: {p.accent_pressed};
}}
QPushButton[variant="primary"]:disabled {{
    background: {p.raised}; border-color: {p.border}; color: {p.text_muted};
}}
QPushButton[variant="quiet"] {{
    background: transparent; border: 1px solid transparent; color: {p.text_secondary};
}}
QPushButton[variant="quiet"]:hover:!disabled {{ background: {p.raised}; color: {p.text}; }}
QPushButton[variant="danger"] {{ color: {p.danger}; border-color: {p.border_strong}; }}
QPushButton[variant="danger"]:hover:!disabled {{ background: {p.raised}; }}

QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {m.radius_small}px;
    padding: {m.gap_tight}px;
}}
QToolButton:hover:!disabled {{ background: {p.raised}; }}
QToolButton:checked {{ background: {p.accent_soft}; border-color: {p.accent}; }}
QToolButton:focus {{ border-color: {p.accent}; }}
QToolButton:disabled {{ color: {p.text_muted}; }}

/* ---- inputs -------------------------------------------------------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
    background: {p.surface};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_small}px;
    padding: 0 {m.gap}px;
    min-height: {m.control_height}px;
    selection-background-color: {p.accent};
    selection-color: {p.text_on_accent};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {p.accent};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {p.text_muted}; background: {p.canvas};
}}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url("{a["chevron"]}"); width: 14px; height: 14px; }}
QComboBox::down-arrow:disabled {{ image: url("{a["chevron_disabled"]}"); }}
QComboBox QAbstractItemView {{
    background: {p.surface};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_small}px;
    selection-background-color: {p.accent_soft};
    selection-color: {p.text};
    padding: {m.gap_tight}px;
}}

QCheckBox, QRadioButton {{ spacing: {m.gap}px; min-height: {m.control_height}px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p.border_strong};
    background: {p.surface};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p.accent}; border-color: {p.accent};
}}
QCheckBox::indicator:checked {{ image: url("{a["check"]}"); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background: {p.canvas}; border-color: {p.border};
}}
QCheckBox:focus, QRadioButton:focus {{ color: {p.accent}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {p.text_muted}; }}

QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background: transparent;
    border: none;
    border-left: 1px solid {p.border};
    width: 20px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {p.raised}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{a["up"]}"); width: 12px; height: 12px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{a["down"]}"); width: 12px; height: 12px;
}}

/* ---- the reading surface ------------------------------------------- */
QTextEdit#ReadingSurface {{
    background: {p.page};
    border: 1px solid {p.border};
    border-radius: {m.radius}px;
    padding: {m.pad_page}px;
    font-size: {m.size_reading}pt;
    selection-background-color: {p.accent_soft};
    selection-color: {p.text};
}}
QTextEdit#ReadingSurface:focus {{ border-color: {p.border_strong}; }}

/* ---- sliders and progress ------------------------------------------ */
QSlider::groove:horizontal {{
    height: 4px; background: {p.border}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {p.surface};
    border: 2px solid {p.accent};
    width: 12px; height: 12px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::handle:horizontal:disabled {{ border-color: {p.border_strong}; }}
QSlider:focus::handle:horizontal {{ border-color: {p.accent_hover}; }}

QProgressBar {{
    background: {p.raised};
    border: none;
    border-radius: 2px;
    max-height: 4px;
    min-height: 4px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 2px; }}

/* ---- lists and tables ---------------------------------------------- */
QListView, QTreeView, QTableView {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: {m.radius}px;
    alternate-background-color: {p.canvas};
    selection-background-color: {p.accent_soft};
    selection-color: {p.text};
    padding: {m.gap_tight}px;
}}
QListView::item, QTreeView::item, QTableView::item {{
    padding: {m.gap}px;
    border-radius: {m.radius_small}px;
    min-height: 24px;
}}
QListView::item:hover, QTreeView::item:hover {{ background: {p.raised}; }}
QHeaderView::section {{
    background: {p.canvas};
    color: {p.text_secondary};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: {m.gap}px;
    font-weight: 600;
}}

/* ---- tabs ----------------------------------------------------------- */
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: {m.radius}px;
    top: -1px;
    background: {p.surface};
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_secondary};
    padding: {m.gap}px {m.pad_wide}px;
    margin-right: {m.gap_tight}px;
    border: 1px solid transparent;
    border-radius: {m.radius_small}px;
}}
QTabBar::tab:selected {{ color: {p.text}; background: {p.surface}; border-color: {p.border}; }}
QTabBar::tab:hover:!selected {{ color: {p.text}; background: {p.raised}; }}
QTabBar::tab:focus {{ border-color: {p.accent}; }}

/* ---- scrollbars: present but quiet --------------------------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {p.border_strong}; border-radius: 4px; min-height: 32px; }}
QScrollBar::handle:hover {{ background: {p.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- chrome --------------------------------------------------------- */
QToolBar {{ background: transparent; border: none; spacing: {m.gap}px; padding: {m.gap}px; }}
QStatusBar {{ background: transparent; color: {p.text_secondary}; }}
QStatusBar::item {{ border: none; }}
QMenuBar {{ background: transparent; }}
QMenuBar::item {{ padding: {m.gap_tight}px {m.gap}px; border-radius: {m.radius_small}px; }}
QMenuBar::item:selected {{ background: {p.raised}; }}
QMenu {{
    background: {p.surface};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius}px;
    padding: {m.gap_tight}px;
}}
QMenu::item {{ padding: {m.gap}px {m.pad}px; border-radius: {m.radius_small}px; }}
QMenu::item:selected {{ background: {p.accent_soft}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: {m.gap_tight}px 0; }}

QToolTip {{
    background: {p.text};
    color: {p.canvas};
    border: none;
    border-radius: {m.radius_small}px;
    padding: {m.gap_tight}px {m.gap}px;
}}

QFrame[role="separator"] {{ background: {p.border}; max-height: 1px; border: none; }}

QGroupBox {{
    border: 1px solid {p.border};
    border-radius: {m.radius}px;
    margin-top: {m.pad}px;
    padding: {m.pad}px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: {m.pad}px;
    padding: 0 {m.gap_tight}px;
    color: {p.text_secondary};
}}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: {m.gap}px; }}
QSplitter::handle:vertical {{ height: {m.gap}px; }}
"""


def apply(app, mode: Mode = Mode.SYSTEM, family: str | None = None) -> Palette:
    """Set the application stylesheet and return the palette in force."""
    p = palette_for(mode)
    app.setStyleSheet(stylesheet(p, METRICS, family))
    return p

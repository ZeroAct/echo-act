"""Icons drawn in code, not shipped as files.

Two reasons rather than one.  N-30 requires icons to carry accessible names
and descriptions, and an icon that is a character in a font gets whatever
name the font gives it -- on Windows the transport glyphs render as colour
emoji, which is both wrong for the theme and meaningless to a screen reader.
And the light/dark switch has to recolour every mark; a bitmap cannot follow
it, while a stroked path is redrawn in the current token colour for nothing.

Everything here is a 24-unit square path stroked at 2 units with round caps,
scaled to whatever size is asked for, so the whole set stays visually
consistent without a designer's file to keep in sync.

A few icons also have to reach Qt's stylesheet, which can only take a URL --
the combo box arrow and the check mark.  Those are rendered once per theme
into a cache directory and referenced by path; ``stylesheet_assets`` returns
the mapping.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

# ----------------------------------------------------------------------
# Path definitions.  Each takes a QPainterPath and draws in a 24x24 box.
# ----------------------------------------------------------------------


def _line(p: QPainterPath, x1: float, y1: float, x2: float, y2: float) -> None:
    p.moveTo(x1, y1)
    p.lineTo(x2, y2)


def _play(p: QPainterPath) -> None:
    p.moveTo(8, 5.5)
    p.lineTo(18.5, 12)
    p.lineTo(8, 18.5)
    p.closeSubpath()


def _pause(p: QPainterPath) -> None:
    p.addRoundedRect(QRectF(8, 5.5, 2.6, 13), 1.3, 1.3)
    p.addRoundedRect(QRectF(13.4, 5.5, 2.6, 13), 1.3, 1.3)


def _stop(p: QPainterPath) -> None:
    p.addRoundedRect(QRectF(7, 7, 10, 10), 2, 2)


def _skip_back(p: QPainterPath) -> None:
    p.moveTo(17, 6)
    p.lineTo(9, 12)
    p.lineTo(17, 18)
    p.closeSubpath()
    p.addRect(QRectF(6.5, 6, 1.6, 12))


def _skip_forward(p: QPainterPath) -> None:
    p.moveTo(7, 6)
    p.lineTo(15, 12)
    p.lineTo(7, 18)
    p.closeSubpath()
    p.addRect(QRectF(15.9, 6, 1.6, 12))


def _chevron_down(p: QPainterPath) -> None:
    p.moveTo(7, 10)
    p.lineTo(12, 15)
    p.lineTo(17, 10)


def _chevron_up(p: QPainterPath) -> None:
    p.moveTo(7, 14)
    p.lineTo(12, 9)
    p.lineTo(17, 14)


def _chevron_right(p: QPainterPath) -> None:
    p.moveTo(10, 7)
    p.lineTo(15, 12)
    p.lineTo(10, 17)


def _check(p: QPainterPath) -> None:
    p.moveTo(6, 12.5)
    p.lineTo(10, 16.5)
    p.lineTo(18, 7.5)


def _close(p: QPainterPath) -> None:
    _line(p, 7, 7, 17, 17)
    _line(p, 17, 7, 7, 17)


def _plus(p: QPainterPath) -> None:
    _line(p, 12, 6, 12, 18)
    _line(p, 6, 12, 18, 12)


def _search(p: QPainterPath) -> None:
    p.addEllipse(QPointF(11, 11), 5, 5)
    _line(p, 14.8, 14.8, 18.5, 18.5)


def _trash(p: QPainterPath) -> None:
    _line(p, 5.5, 7, 18.5, 7)
    p.moveTo(7.5, 7)
    p.lineTo(8.4, 19)
    p.lineTo(15.6, 19)
    p.lineTo(16.5, 7)
    p.moveTo(9.5, 7)
    p.lineTo(9.5, 4.8)
    p.lineTo(14.5, 4.8)
    p.lineTo(14.5, 7)


def _folder(p: QPainterPath) -> None:
    p.moveTo(4, 18.5)
    p.lineTo(4, 6)
    p.lineTo(10, 6)
    p.lineTo(11.8, 8.4)
    p.lineTo(20, 8.4)
    p.lineTo(20, 18.5)
    p.closeSubpath()


def _save(p: QPainterPath) -> None:
    p.addRoundedRect(QRectF(5, 5, 14, 14), 2, 2)
    p.addRect(QRectF(8.5, 5, 7, 5))
    p.addRect(QRectF(8, 13, 8, 6))


def _download(p: QPainterPath) -> None:
    _line(p, 12, 4.5, 12, 15)
    p.moveTo(7.5, 10.5)
    p.lineTo(12, 15)
    p.lineTo(16.5, 10.5)
    _line(p, 5.5, 18.5, 18.5, 18.5)


def _export(p: QPainterPath) -> None:
    _line(p, 12, 15, 12, 4.5)
    p.moveTo(7.5, 9)
    p.lineTo(12, 4.5)
    p.lineTo(16.5, 9)
    _line(p, 5.5, 18.5, 18.5, 18.5)


def _refresh(p: QPainterPath) -> None:
    p.arcMoveTo(QRectF(5, 5, 14, 14), 60)
    p.arcTo(QRectF(5, 5, 14, 14), 60, 280)
    p.moveTo(15.5, 4.5)
    p.lineTo(16.5, 8.6)
    p.lineTo(12.4, 9.2)


def _settings(p: QPainterPath) -> None:
    """Sliders, not a gear.

    A six-spoke gear collapses into something that reads as a sparkle below
    about 18 px, which is the size this icon is actually used at.
    """
    for y, knob in ((7.5, 15.5), (12.0, 9.0), (16.5, 13.5)):
        _line(p, 4.5, y, 19.5, y)
        p.addEllipse(QPointF(knob, y), 2.1, 2.1)


def _library(p: QPainterPath) -> None:
    p.addRect(QRectF(5, 5.5, 3.4, 13))
    p.addRect(QRectF(10, 5.5, 3.4, 13))
    p.moveTo(15.6, 6.4)
    p.lineTo(18.9, 5.7)
    p.lineTo(20.4, 18.2)
    p.lineTo(17.1, 18.9)
    p.closeSubpath()


def _cube(p: QPainterPath) -> None:
    p.moveTo(12, 3.8)
    p.lineTo(19.5, 8)
    p.lineTo(19.5, 16)
    p.lineTo(12, 20.2)
    p.lineTo(4.5, 16)
    p.lineTo(4.5, 8)
    p.closeSubpath()
    p.moveTo(4.5, 8)
    p.lineTo(12, 12.2)
    p.lineTo(19.5, 8)
    p.moveTo(12, 12.2)
    p.lineTo(12, 20.2)


def _plug(p: QPainterPath) -> None:
    _line(p, 9, 3.5, 9, 8)
    _line(p, 15, 3.5, 15, 8)
    p.moveTo(6.5, 8)
    p.lineTo(17.5, 8)
    p.lineTo(17.5, 12)
    p.arcTo(QRectF(6.5, 6.5, 11, 11), 0, -180)
    p.closeSubpath()
    _line(p, 12, 17.5, 12, 20.5)


def _volume(p: QPainterPath) -> None:
    p.moveTo(4.5, 9.5)
    p.lineTo(8, 9.5)
    p.lineTo(12, 5.5)
    p.lineTo(12, 18.5)
    p.lineTo(8, 14.5)
    p.lineTo(4.5, 14.5)
    p.closeSubpath()
    p.arcMoveTo(QRectF(11, 7.5, 9, 9), 60)
    p.arcTo(QRectF(11, 7.5, 9, 9), 60, -120)


def _volume_muted(p: QPainterPath) -> None:
    p.moveTo(4.5, 9.5)
    p.lineTo(8, 9.5)
    p.lineTo(12, 5.5)
    p.lineTo(12, 18.5)
    p.lineTo(8, 14.5)
    p.lineTo(4.5, 14.5)
    p.closeSubpath()
    _line(p, 15, 9.5, 20, 14.5)
    _line(p, 20, 9.5, 15, 14.5)


def _locate(p: QPainterPath) -> None:
    """Return to the reading position (F-30)."""
    p.addEllipse(QPointF(12, 12), 3, 3)
    _line(p, 12, 3, 12, 6.5)
    _line(p, 12, 17.5, 12, 21)
    _line(p, 3, 12, 6.5, 12)
    _line(p, 17.5, 12, 21, 12)


def _info(p: QPainterPath) -> None:
    p.addEllipse(QPointF(12, 12), 8, 8)
    _line(p, 12, 11, 12, 16.5)
    p.addEllipse(QPointF(12, 7.8), 0.2, 0.2)


def _warning(p: QPainterPath) -> None:
    p.moveTo(12, 4)
    p.lineTo(21, 19.5)
    p.lineTo(3, 19.5)
    p.closeSubpath()
    _line(p, 12, 9.5, 12, 14.5)
    p.addEllipse(QPointF(12, 17), 0.2, 0.2)


def _error(p: QPainterPath) -> None:
    p.addEllipse(QPointF(12, 12), 8, 8)
    _line(p, 9, 9, 15, 15)
    _line(p, 15, 9, 9, 15)


def _check_circle(p: QPainterPath) -> None:
    p.addEllipse(QPointF(12, 12), 8, 8)
    p.moveTo(8.2, 12.2)
    p.lineTo(11, 15)
    p.lineTo(15.8, 9.4)


def _copy(p: QPainterPath) -> None:
    p.addRoundedRect(QRectF(8.5, 4.5, 11, 13), 2, 2)
    p.moveTo(15.5, 19.5)
    p.lineTo(6.5, 19.5)
    p.lineTo(6.5, 8)


def _document(p: QPainterPath) -> None:
    p.moveTo(6, 3.5)
    p.lineTo(14, 3.5)
    p.lineTo(18.5, 8)
    p.lineTo(18.5, 20.5)
    p.lineTo(6, 20.5)
    p.closeSubpath()
    p.moveTo(14, 3.5)
    p.lineTo(14, 8)
    p.lineTo(18.5, 8)


def _pulse(p: QPainterPath) -> None:
    """Activity: a heartbeat line, which reads at 16 px where a gauge does not."""
    p.moveTo(3, 12)
    p.lineTo(8, 12)
    p.lineTo(10.5, 6)
    p.lineTo(13.5, 18)
    p.lineTo(16, 12)
    p.lineTo(21, 12)


def _more(p: QPainterPath) -> None:
    for x in (7.0, 12.0, 17.0):
        p.addEllipse(QPointF(x, 12), 0.9, 0.9)


#: name -> (drawer, filled?, accessible name)
#:
#: The accessible name travels with the icon so that a caller cannot forget
#: it; N-30 requires every icon to have one.
_ICONS: dict[str, tuple[Callable[[QPainterPath], None], bool, str]] = {
    "play": (_play, True, "Play"),
    "pause": (_pause, True, "Pause"),
    "stop": (_stop, True, "Stop"),
    "skip-back": (_skip_back, True, "Previous segment"),
    "skip-forward": (_skip_forward, True, "Next segment"),
    "chevron-down": (_chevron_down, False, "Open"),
    "chevron-up": (_chevron_up, False, "Close"),
    "chevron-right": (_chevron_right, False, "Expand"),
    "check": (_check, False, "Selected"),
    "close": (_close, False, "Close"),
    "plus": (_plus, False, "Add"),
    "search": (_search, False, "Search"),
    "trash": (_trash, False, "Delete"),
    "folder": (_folder, False, "Open file"),
    "save": (_save, False, "Save"),
    "download": (_download, False, "Download"),
    "export": (_export, False, "Export"),
    "refresh": (_refresh, False, "Refresh"),
    "settings": (_settings, False, "Settings"),
    "library": (_library, False, "Library"),
    "cube": (_cube, False, "Models"),
    "plug": (_plug, False, "Connect an app"),
    "pulse": (_pulse, False, "Activity"),
    "volume": (_volume, False, "Volume"),
    "volume-muted": (_volume_muted, False, "Muted"),
    "locate": (_locate, False, "Return to the reading position"),
    "info": (_info, False, "Information"),
    "warning": (_warning, False, "Warning"),
    "error": (_error, False, "Error"),
    "check-circle": (_check_circle, False, "Done"),
    "copy": (_copy, False, "Copy"),
    "document": (_document, False, "Document"),
    "more": (_more, True, "More"),
}


def accessible_name(name: str) -> str:
    return _ICONS[name][2]


def available() -> tuple[str, ...]:
    return tuple(_ICONS)


def _render(name: str, color: str, size: int, dpr: float, stroke: float) -> QPixmap:
    drawer, filled, _ = _ICONS[name]
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)

    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # Qt already folds the pixmap's device-pixel-ratio into the painter's
    # transform, so the painter works in logical units and the scale is
    # size/24 rather than size*dpr/24.  Multiplying by dpr here draws the
    # path at twice its size and clips it against the pixmap.
    painter.scale(size / 24.0, size / 24.0)

    path = QPainterPath()
    drawer(path)
    c = QColor(color)
    if filled:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(path, c)
    else:
        pen = QPen(c)
        pen.setWidthF(stroke)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
    painter.end()
    return px


@lru_cache(maxsize=512)
def pixmap(name: str, color: str, size: int = 18, dpr: float = 1.0, stroke: float = 1.8) -> QPixmap:
    return _render(name, color, size, dpr, stroke)


@lru_cache(maxsize=512)
def icon(name: str, color: str, disabled_color: str | None = None, size: int = 18) -> QIcon:
    """A QIcon with a normal and a disabled rendering.

    Both are supplied because N-09 requires unavailable controls to look
    unavailable, and a single-pixmap icon stays at full contrast when Qt
    disables the button.
    """
    ic = QIcon()
    for dpr in (1.0, 2.0):
        ic.addPixmap(_render(name, color, size, dpr, 1.8), QIcon.Mode.Normal, QIcon.State.Off)
        if disabled_color:
            ic.addPixmap(
                _render(name, disabled_color, size, dpr, 1.8),
                QIcon.Mode.Disabled,
                QIcon.State.Off,
            )
    return ic


def icon_size(size: int = 18) -> QSize:
    return QSize(size, size)


# ----------------------------------------------------------------------
# Assets the stylesheet needs as files
# ----------------------------------------------------------------------


def _asset_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "echoact-ui-assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stylesheet_assets(theme_key: str, spec: dict[str, tuple[str, str, int]]) -> dict[str, str]:
    """Render the marks Qt's stylesheet can only take as a URL.

    ``spec`` maps a key to (icon name, colour, size).  Returns the same keys
    mapped to forward-slash paths, which is what Qt expects inside
    ``url(...)`` on every platform.  Files are written once per theme and
    reused, so switching themes twice does not accumulate files.
    """
    out: dict[str, str] = {}
    d = _asset_dir()
    for key, (name, color, size) in spec.items():
        path = d / f"{theme_key}-{key}-{size}.png"
        if not path.exists():
            _render(name, color, size, 2.0, 2.0).save(str(path))
        out[key] = path.as_posix()
    return out

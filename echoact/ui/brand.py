"""The brandmark: the Origin drawing, in code rather than in files.

``icons.py`` gives the reason a module like this exists: a stroked mark
follows the theme's tokens and every device-pixel-ratio, while a bitmap
follows neither.  The brand is held to the same rule so the taskbar tile
recolours with the theme and survives Retina without a second asset.

The drawing is the "Origin" concept from ``docs/icons/ripple-candidates.html``:
one filled dot -- the source text, the only solid thing -- inside outline
rings widening outward.  The rings are outlines because the app's entire
marking language is the outline that never moves what it marks (N-13).

The size ladder is the study's, and it is a *reduction* rather than a
redesign: three rings above 32 px, two under it, and at 16 px the mark is
the ieung form -- a heavy ring and the dot, ㅇ, the first letter of 에코.
That is the same drawing told the truth about small sizes, which is what
makes one concept cover the favicon, the taskbar, and the window.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from .theme import Palette

#: The unit box every geometry below is authored in -- the same 64 the
#: concept study uses, so the files there and the pixels here agree.
_UNIT = 64.0

#: (radius, stroke, opacity) from the inside out, per variant.  ``two`` is
#: the <=32 px ladder rung, with slightly stouter strokes for the same
#: reason a road sign thickens its lines at distance.
_VARIANTS: dict[str, tuple[tuple[float, float, float], ...]] = {
    "full": ((14.5, 4.2, 0.8), (23.5, 3.8, 0.5), (29.6, 3.4, 0.26)),
    "two": ((16.5, 5.0, 0.85), (26.5, 4.4, 0.45)),
}
_DOT: dict[str, float] = {"full": 5.4, "two": 6.2, "ieung": 5.2}
_IEUNG_RING: tuple[float, float] = (20.5, 7.5)

#: Corner rounding of the tile, matching the Windows 11 icon family.
TILE_RADIUS_RATIO: float = 0.21
#: How much of the tile the mark may claim; below this it drowns in the
#: tile, above it the tile stops reading as ground.
MARK_FILL_RATIO: float = 0.72


def variant_for(size: int) -> str:
    """Which rung of the ladder this size draws."""
    if size <= 20:
        return "ieung"
    if size <= 32:
        return "two"
    return "full"


def _paint_mark(painter: QPainter, color: QColor, variant: str) -> None:
    """Draw in the 64-unit box; the caller has already scaled into it."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if variant == "ieung":
        radius, stroke = _IEUNG_RING
        pen = QPen(color)
        pen.setWidthF(stroke)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(32, 32), radius, radius)
    else:
        for radius, stroke, opacity in _VARIANTS[variant]:
            ring = QColor(color)
            ring.setAlphaF(opacity)
            pen = QPen(ring)
            pen.setWidthF(stroke)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(32, 32), radius, radius)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    dot = _DOT[variant]
    painter.drawEllipse(QPointF(32, 32), dot, dot)


def mark_pixmap(size: int, color: str, dpr: float = 1.0) -> QPixmap:
    """The bare mark on transparency, for in-app use (the About screen)."""
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    painter.scale(size / _UNIT, size / _UNIT)
    _paint_mark(painter, QColor(color), variant_for(size))
    painter.end()
    return px


def tile_pixmap(size: int, ground: str, mark: str, dpr: float = 1.0) -> QPixmap:
    """The mark on its tile: the taskbar, the window, and the exe file."""
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ground))
    painter.drawRoundedRect(
        QRectF(0.0, 0.0, size, size), size * TILE_RADIUS_RATIO, size * TILE_RADIUS_RATIO
    )
    inset = size * MARK_FILL_RATIO
    painter.translate((size - inset) / 2, (size - inset) / 2)
    painter.scale(inset / _UNIT, inset / _UNIT)
    _paint_mark(painter, QColor(mark), variant_for(size))
    painter.end()
    return px


def app_icon(palette: Palette) -> QIcon:
    """Every size the window manager may ask for, in theme colours.

    256 is included because Windows stores the window icon in the larger
    renditions when it can; asking for 16 only is what leaves an app
    blurry in Alt-Tab.
    """
    ic = QIcon()
    for px in (16, 20, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(tile_pixmap(px, palette.accent, palette.text_on_accent))
    return ic


__all__ = ["app_icon", "mark_pixmap", "tile_pixmap", "variant_for"]

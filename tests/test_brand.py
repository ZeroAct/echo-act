"""The brandmark is the product's face in two places -- the taskbar while
running and the exe file on disk -- and both are checked here rather than
in the visual review, because both have already been wrong once in other
people's apps: a mark that dissolves at 16 px, and an icon that quietly
stops being shipped when someone edits the spec.
"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor

from echoact.ui import brand
from echoact.ui.theme import LIGHT

ROOT = Path(__file__).resolve().parent.parent
ICO = ROOT / "packaging" / "echoact.ico"
SPEC = ROOT / "packaging" / "echoact.spec"


def test_the_ladder_reduces_rather_than_redraws() -> None:
    """The study's promise: one concept at every size, three rungs."""
    assert brand.variant_for(256) == "full"
    assert brand.variant_for(24) == "two"
    assert brand.variant_for(16) == "ieung"


def test_the_tile_has_a_mark_on_ground(qapp) -> None:
    """Opaque centre in the mark colour, transparent corner in the ground:
    not an empty square, and not a full-bleed square either."""
    px = brand.tile_pixmap(32, LIGHT.accent, LIGHT.text_on_accent)
    assert not px.isNull()
    image = px.toImage()
    centre = image.pixelColor(16, 16)  # the dot, which every rung fills
    assert centre == QColor(LIGHT.text_on_accent)
    corner = image.pixelColor(0, 0)  # outside the rounded corner
    assert corner.alpha() == 0


def test_the_app_icon_carries_every_size_a_window_manager_asks(qapp) -> None:
    icon = brand.app_icon(LIGHT)
    sizes = {s for s in icon.availableSizes()}
    assert {QSize(16, 16), QSize(32, 32), QSize(256, 256)} <= sizes


def test_the_ico_container_is_honest() -> None:
    """A hand-written container deserves a test on its bytes: header,
    directory count, and a square of each advertised size."""
    assert ICO.is_file(), "run scripts/make_app_icon.py"
    data = ICO.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1), "not an icon file"
    assert count >= 6
    seen: list[int] = []
    for i in range(count):
        w, h = data[6 + 16 * i], data[7 + 16 * i]
        w = w or 256
        h = h or 256
        assert w == h
        size, offset = struct.unpack_from("<II", data, 6 + 16 * i + 8)
        assert data[offset : offset + 4] == b"\x89PNG", "entries must be PNG-compressed"
        assert offset + size <= len(data)
        seen.append(w)
    assert 256 in seen, "Explorer takes the largest entry; 256 must be present"


def test_the_spec_still_ships_the_icon() -> None:
    """The icon half of the spec is the easy line to drop while editing
    the collector above it, and a build without an exe icon looks
    finished -- which is how this regresses silently."""
    text = SPEC.read_text(encoding="utf-8")
    assert "echoact.ico" in text
    assert "icon=str(ICON)" in text

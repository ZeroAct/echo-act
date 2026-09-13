"""Render ``packaging/echoact.ico`` -- the executable's face on disk.

    uv run python scripts/make_app_icon.py

The drawing lives in ``echoact/ui/brand.py``, not in a file, so the
taskbar while running and the exe in Explorer are the same mark rendered
twice rather than two assets free to drift.  Qt renders each size into a
PNG; the .ico is then assembled here by hand because it is a six-byte
header, one sixteen-byte directory entry per image, and the PNGs -- every
Windows since Vista reads PNG-compressed entries, and a container that
takes twenty lines to write is one fewer build dependency.

The tile is the light-theme tile on purpose: Explorer shows this icon on
grounds of its own choosing, and the accent tile reads on both.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from echoact.ui import brand  # noqa: E402
from echoact.ui.theme import LIGHT  # noqa: E402

OUT = ROOT / "packaging" / "echoact.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _png(pixmap) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "PNG")
    return bytes(buf.data())


def main() -> int:
    QGuiApplication(sys.argv)
    images = [(size, _png(brand.tile_pixmap(size, LIGHT.accent, LIGHT.text_on_accent)))
              for size in SIZES]

    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, png in images:
        # The width and height fields are one byte each; 256 is spelled 0.
        w = size if size < 256 else 0
        h = size if size < 256 else 0
        out += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    for _, png in images:
        out += png

    OUT.write_bytes(out)
    print(f"wrote {OUT} ({len(out):,} bytes): {', '.join(str(s) for s in SIZES)} px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

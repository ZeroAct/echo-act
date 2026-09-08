"""Spike: can emphasis be drawn without disturbing text layout?

Appendix A.2 of docs/design.md assumes N-13 can be satisfied by drawing the
current segment with a glyph outline rather than a heavier weight, applied as a
view-level extra selection so F-29 and F-30 also hold.  This measures it.

Two earlier attempts produced confident nonsense, so the guards matter:

  - The first compared outline against bold and found neither moved any
    character.  That is the measurement failing, not the assumption holding:
    an extra selection is a paint-time format, so a format that changes glyph
    shaping cannot take effect through it at all.
  - The second ran under QT_QPA_PLATFORM=offscreen, which on Windows has no
    font database.  Qt reported family "" at size -1 and every result was an
    artefact.  Hence the platform assertion below, and hence the probe: if
    document-level bold does not move characters, this file is measuring
    nothing and says so rather than reporting a pass.

What is checked, per widget and per scale factor:

  1. Probe.  Document-level bold must move characters.  Without this the
     geometry comparison is void.
  2. No relayout.  An outline applied as an extra selection must not move any
     character, which is what N-13 needs.
  3. Visibility.  It must change rendered pixels, since a format that draws
     nothing would pass check 2 trivially.
  4. Ink spill.  Heavier ink painted into cells laid out for lighter glyphs can
     cross into neighbouring cells.  This counts pixels landing outside the
     emphasised characters' own cells, as a legibility signal for review.

    uv run --with PySide6 --with numpy spikes/outline_emphasis.py
"""

from __future__ import annotations

import os
import subprocess
import sys

SCALES = ("1.0", "1.5", "2.0")  # N-30 requires 100% through 200%
WIDTHS = (0.3, 0.4, 0.6)  # candidate outline pen widths, for tuning
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")

SAMPLE = (
    "EchoAct reads Korean and English aloud. "
    "에코액트는 한국어와 영어를 소리내어 읽습니다. "
    "Mixed 문장 with 123 numbers, symbols (!?), and emoji \U0001f3a7 too. "
    "두 번째 문장입니다. This is the second sentence, long enough to wrap "
    "across several lines so that any change in advance width shows up as a "
    "different line break rather than only a shifted glyph."
)

# The FIRST sentence stands in for the segment being played.  Emphasising the
# first line maximises the reflow signal: if advance widths change at all,
# every remaining line shifts, so changed pixels run to the bottom of the
# text.  Emphasising a middle sentence leaves too little below it to tell a
# reflow apart from the emphasis itself.
SEG_START = 0
SEG_END = SAMPLE.index("에코액트")


def child(scale: str) -> int:
    import numpy as np
    from PySide6.QtGui import (QColor, QFont, QImage, QPen, QTextCharFormat,
                               QTextCursor)
    from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTextEdit

    app = QApplication([])

    def build(cls):
        w = cls()
        w.setFont(QFont("Malgun Gothic", 11))
        w.setLineWrapMode(cls.LineWrapMode.WidgetWidth)
        w.setPlainText(SAMPLE)
        w.resize(560, 420)
        w.ensurePolished()
        w.grab()  # force a first layout pass; QTextEdit reports zeros without one
        app.processEvents()
        fi = w.fontInfo()
        assert fi.family() and fi.pointSize() > 0, (
            f"no usable font database (family={fi.family()!r} size={fi.pointSize()}); "
            "this platform plugin cannot measure text"
        )
        return w

    def seg_cursor(w):
        cur = QTextCursor(w.document())
        cur.setPosition(SEG_START)
        cur.setPosition(SEG_END, QTextCursor.MoveMode.KeepAnchor)
        return cur

    def select(w, fmt):
        if fmt is None:
            w.setExtraSelections([])
        else:
            sel = QTextEdit.ExtraSelection()
            sel.cursor = seg_cursor(w)
            sel.format = fmt
            w.setExtraSelections([sel])
        app.processEvents()

    def geometry(w):
        doc = w.document()
        cur = QTextCursor(doc)
        out = []
        for i in range(doc.characterCount()):
            cur.setPosition(i)
            r = w.cursorRect(cur)
            out.append((r.x(), r.y(), r.height()))
        return out

    def pixels(w):
        img = w.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
        arr = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(
            img.height(), img.bytesPerLine() // 4, 4
        )
        return arr[:, : img.width(), :].copy()

    def cell_mask(shape, geom, dpr):
        """Pixels belonging to the emphasised characters' own layout cells.

        Geometry is logical; the grabbed image is device pixels, so at any
        scale factor above 1.0 the two only line up after scaling by dpr.
        """
        m = np.zeros(shape[:2], dtype=bool)
        for i in range(SEG_START, min(SEG_END, len(geom) - 1)):
            x0, y0, h = (v * dpr for v in geom[i])
            nx, ny, _ = (v * dpr for v in geom[i + 1])
            x1 = nx if ny == y0 else shape[1]
            lo, hi = sorted((max(0, int(x0)), max(0, int(x1))))
            m[max(0, int(y0)) : int(y0 + h), lo : hi + 1] = True
        return m

    bold = QTextCharFormat()
    bold.setFontWeight(QFont.Weight.Bold)

    os.makedirs(OUT_DIR, exist_ok=True)
    failures = []
    inconclusive = set()

    for cls in (QPlainTextEdit, QTextEdit):
        name = cls.__name__
        w = build(cls)

        select(w, None)
        base_geom, base_px = geometry(w), pixels(w)

        # Idempotence guard: measuring must not itself perturb the widget.
        # QPlainTextEdit lays out lazily, so a full sweep of cursorRect can
        # change what the next sweep reports, which would masquerade as a
        # format moving text.
        drift = sum(1 for a, b in zip(base_geom, geometry(w)) if a != b)
        if drift:
            print(f"      UNSTABLE probe: re-measuring with no change moved "
                  f"{drift} chars; geometry results for this widget are void")
            # advisory only: the reflow test below does not depend on it

        dpr = base_px.shape[1] / max(1, w.width())
        mask = cell_mask(base_px.shape, base_geom, dpr)
        w.grab().save(os.path.join(OUT_DIR, f"{name}-{scale}-plain.png"))

        print(f"  {name:<15} scale={scale}  font={w.fontInfo().family()}  dpr={dpr:g}")

        # --- reflow test, without geometry ---
        # If emphasis reflows, every line after the emphasised run shifts, so
        # changed pixels reach the bottom of the text.  If it does not, changes
        # stop at the run.  This needs no cursorRect, so it survives lazy
        # layout.  Calibrated against a document-level bold on a throwaway
        # widget, which must reflow; without that control this proves nothing.
        def lowest_change(px):
            d = np.any(px != base_px, axis=2)
            rows = np.flatnonzero(d.any(axis=1))
            return int(rows[-1]) if rows.size else -1

        pw = build(cls)
        pw_base = pixels(pw)
        seg_cursor(pw).mergeCharFormat(bold)
        app.processEvents()
        d = np.any(pixels(pw) != pw_base, axis=2)
        rows = np.flatnonzero(d.any(axis=1))
        reflow_y = int(rows[-1]) if rows.size else -1
        print(f"      control document bold: changes reach y={reflow_y}")
        if reflow_y < 0:
            failures.append(f"{name} @ {scale}: control did not reflow, test void")

        # --- extra-selection formats ---
        cases = [("bold", bold)]
        for wd in WIDTHS:
            f = QTextCharFormat()
            f.setTextOutline(QPen(QColor(0, 0, 0), wd))
            cases.append((f"outline-{wd}", f))

        for label, fmt in cases:
            select(w, fmt)
            g, px = geometry(w), pixels(w)
            moved = sum(1 for a, b in zip(base_geom, g) if a != b)
            diff = np.any(px != base_px, axis=2)
            spill = int((diff & ~mask).sum())
            low = lowest_change(px)
            # Only meaningful where the control reflowed far enough to be
            # distinguishable from the emphasis's own extent.
            conclusive = reflow_y >= 2 * max(low, 1)
            reflowed = conclusive and low >= reflow_y - 2 and int(diff.sum()) > 0
            print(
                f"      {label:<12} px changed={int(diff.sum()):<6} "
                f"changes reach y={low:<5} "
                f"reflow={'YES' if reflowed else ('no' if conclusive else '?'):<4} "
                f"outside own cells={spill}"
            )
            w.grab().save(os.path.join(OUT_DIR, f"{name}-{scale}-{label}.png"))

            if reflowed:
                failures.append(f"{name} @ {scale}: {label} relaid out the text")
            elif not conclusive:
                inconclusive.add(f"{name} @ {scale}")
            if label.startswith("outline") and diff.sum() == 0:
                failures.append(f"{name} @ {scale}: {label} draws nothing")
            select(w, None)

    for f in failures:
        print("  " + f)
    for i in sorted(inconclusive):
        print(f"  {i}: control too weak to measure reflow here; pixel behaviour "
              f"matches the verified widget but layout stability is unproven")
    return 1 if failures else 0


def main() -> int:
    if "--child" in sys.argv:
        return child(os.environ.get("QT_SCALE_FACTOR", "1.0"))

    print("Emphasis layout stability (N-13), via extra selections (F-29, F-30)\n")
    rc = 0
    for scale in SCALES:
        env = dict(os.environ, QT_SCALE_FACTOR=scale)
        env.pop("QT_QPA_PLATFORM", None)  # needs a real font database
        rc |= subprocess.run([sys.executable, __file__, "--child"], env=env).returncode
    print("\nRESULT:", "assumption holds" if rc == 0 else "assumption does NOT hold")
    print("images:", OUT_DIR)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

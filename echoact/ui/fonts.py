"""Loading the bundled typeface, when there is one.

A.2 wants one bundled Korean and Latin family rather than the per-OS
defaults, so that metrics -- and therefore N-12's synchronisation
measurements and N-30's layout checks -- are identical on Windows and
macOS.  Without it the app still looks right, but a measurement taken on
one machine does not transfer to another, because Malgun Gothic and Apple
SD Gothic Neo do not lay out the same text the same way.

The font is not committed to the repository.  It is fetched by
``scripts/fetch_fonts.py`` into ``echoact/assets/fonts`` and picked up
here if it is present; the packaging step includes whatever it finds.
Two reasons for that split: a licence that requires its own notice file
should be acquired deliberately rather than inherited by a checkout, and
a few megabytes of binary in a source tree is a cost every clone pays
forever.

``load()`` is safe to call when nothing is there.  A missing font is a
measurement caveat, not a failure -- so it degrades to the per-OS stack
and says so, rather than refusing to start.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..util.logging import get_logger

log = get_logger("ui.fonts")

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

#: The family A.2 calls for.  SIL Open Font License, covers Hangul and
#: Latin in one family with matching metrics, and has a variable weight
#: axis -- which matters because N-13's alternative mechanism was "a
#: variable font whose weight axis preserves metrics".
BUNDLED_FAMILY = "Pretendard"


@dataclass(frozen=True, slots=True)
class FontReport:
    """What was loaded, for F-72's diagnostic export and for a test.

    ``metrics_are_portable`` is the fact that actually matters: it says
    whether a layout measurement taken here would mean anything on
    another machine.
    """

    families: tuple[str, ...]
    files: tuple[str, ...]
    bundled_available: bool

    @property
    def metrics_are_portable(self) -> bool:
        return self.bundled_available


def available_files() -> tuple[Path, ...]:
    if not ASSET_DIR.is_dir():
        return ()
    return tuple(
        sorted(p for p in ASSET_DIR.iterdir() if p.suffix.lower() in {".ttf", ".otf", ".ttc"})
    )


def load() -> FontReport:
    """Register every bundled face with Qt.  Returns what is now usable."""
    from PySide6.QtGui import QFontDatabase

    families: list[str] = []
    files: list[str] = []
    for path in available_files():
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            log.warning("could not load bundled font %s", path.name)
            continue
        files.append(path.name)
        families.extend(QFontDatabase.applicationFontFamilies(font_id))

    unique = tuple(dict.fromkeys(families))
    report = FontReport(
        families=unique,
        files=tuple(files),
        bundled_available=any(BUNDLED_FAMILY in f for f in unique),
    )
    if not report.bundled_available:
        # Said once, at start-up, because it changes what a measurement
        # means rather than what the user sees.
        log.info(
            "no bundled typeface; using the per-OS stack, so layout "
            "measurements are not comparable across operating systems"
        )
    return report

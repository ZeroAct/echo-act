"""Fetch the typeface A.2 asks the release to bundle.

Run once, deliberately, before packaging:

    uv run python scripts/fetch_fonts.py

Pretendard is under the SIL Open Font License 1.1, which allows bundling
and redistribution inside an application and requires the licence to
travel with the font.  This script copies the licence next to the faces
for exactly that reason, and refuses to leave a font behind without one.

Not run automatically and not committed.  A checkout should not acquire a
third-party licence obligation as a side effect of cloning, and the app
runs perfectly well without this -- what it loses is the guarantee that a
layout measurement taken on Windows means the same thing on macOS.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent.parent / "echoact" / "assets" / "fonts"

VERSION = "1.3.9"
ARCHIVE_URL = (
    f"https://github.com/orioncactus/pretendard/releases/download/v{VERSION}/Pretendard-{VERSION}.zip"
)
#: Only the two weights the app uses.  The emphasis is an outline rather
#: than a heavier weight (N-13), so a full weight range would be dead
#: bytes in every install.
WANTED = ("Pretendard-Regular.otf", "Pretendard-SemiBold.otf")
LICENCE_NAMES = ("LICENSE", "LICENSE.txt", "OFL.txt", "SIL Open Font License.txt")


def main() -> int:
    import httpx

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    print(f"fetching Pretendard {VERSION}")
    try:
        response = httpx.get(ARCHIVE_URL, follow_redirects=True, timeout=120.0)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - a script, and the reason is the point
        print(f"could not fetch the archive: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    wrote: list[str] = []
    licence_written = False
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for info in archive.infolist():
            name = Path(info.filename).name
            if name in WANTED:
                (ASSET_DIR / name).write_bytes(archive.read(info))
                wrote.append(name)
            elif name in LICENCE_NAMES and not licence_written:
                (ASSET_DIR / "LICENSE-Pretendard.txt").write_bytes(archive.read(info))
                licence_written = True

    if not wrote:
        print("the archive did not contain the expected faces", file=sys.stderr)
        return 1
    if not licence_written:
        # The OFL requires the licence to travel with the font, so a font
        # without one is worse than no font: it is an obligation the
        # release would silently fail to meet.
        for name in wrote:
            (ASSET_DIR / name).unlink(missing_ok=True)
        print("no licence file in the archive; removed the faces", file=sys.stderr)
        return 1

    for name in wrote:
        print(f"  {name}  {(ASSET_DIR / name).stat().st_size / 1000:.0f} kB")
    print(f"  LICENSE-Pretendard.txt\nwritten to {ASSET_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

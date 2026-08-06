#!/usr/bin/env python3
"""Rebuild the vendored signature font from the upstream DejaVu release.

WHY THE FONT IS RESCALED
------------------------
pyHanko writes a CID font's /W array straight from the font's hmtx table,
in raw font design units:

    cidfont_obj['/W'] = ... width ...        # pyhanko/pdf_utils/font/opentype.py

PDF expects those widths in glyph space, which for a CIDFontType2 is fixed at
1/1000 of text space. The two only agree when the font's unitsPerEm is 1000.
DejaVu Sans is 2048, so every glyph advanced 2.048x too far: text ran outside
its box and letters looked spaced out. pyHanko's own layout was unaffected,
because that path divides by unitsPerEm, so the defect is invisible until the
PDF is actually rendered.

Rescaling the font to 1000 units per em makes the raw widths correct by
construction, which avoids patching pyHanko internals at runtime.

Run:  venv/bin/python scripts/prepare-signature-font.py
"""

import hashlib
import io
import pathlib
import sys
import urllib.request
import zipfile

UPSTREAM = ("https://github.com/dejavu-fonts/dejavu-fonts/releases/download/"
            "version_2_37/dejavu-fonts-ttf-2.37.zip")
UPSTREAM_SHA256 = "7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a"
MEMBER_FONT = "dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf"
MEMBER_LICENSE = "dejavu-fonts-ttf-2.37/LICENSE"

TARGET_UPEM = 1000
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "fonts"
OUT_FONT = OUT_DIR / "DejaVuSans-1000upem.ttf"
OUT_LICENSE = OUT_DIR / "DejaVuSans-LICENSE.txt"


def main():
    try:
        from fontTools.ttLib import TTFont
        from fontTools.ttLib.scaleUpem import scale_upem
    except ImportError:
        sys.exit("fontTools is required: pip install 'pyHanko[opentype]'")

    print(f"Downloading {UPSTREAM}")
    payload = urllib.request.urlopen(UPSTREAM).read()
    digest = hashlib.sha256(payload).hexdigest()
    print(f"  sha256 {digest}")
    if UPSTREAM_SHA256 and digest != UPSTREAM_SHA256:
        sys.exit(f"checksum mismatch, expected {UPSTREAM_SHA256}")

    archive = zipfile.ZipFile(io.BytesIO(payload))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_LICENSE.write_bytes(archive.read(MEMBER_LICENSE))

    font = TTFont(io.BytesIO(archive.read(MEMBER_FONT)))
    print(f"  upstream unitsPerEm {font['head'].unitsPerEm}")
    scale_upem(font, TARGET_UPEM)
    font.save(OUT_FONT)

    written = TTFont(OUT_FONT)
    assert written['head'].unitsPerEm == TARGET_UPEM
    print(f"Wrote {OUT_FONT} at {TARGET_UPEM} units per em")
    print(f"Wrote {OUT_LICENSE}")


if __name__ == '__main__':
    main()

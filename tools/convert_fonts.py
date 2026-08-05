"""Converts the racecast runtime's woff2 fonts to ttf.

Pillow can't read woff2, fontTools can. Run once.
"""
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

SOURCE = Path("/Users/jegr/racecast/runtime/fonts")
TARGET = Path(__file__).resolve().parent.parent / "channels" / "erf" / "fonts"
WANTED = ["Oswald-Bold", "BarlowCondensed-Bold", "Archivo-Bold"]


def convert(name: str) -> Path:
    source = SOURCE / f"{name}.woff2"
    if not source.exists():
        raise FileNotFoundError(f"Font missing: {source}")
    TARGET.mkdir(parents=True, exist_ok=True)
    target = TARGET / f"{name}.ttf"
    font = TTFont(str(source))
    font.flavor = None
    font.save(str(target))
    return target


if __name__ == "__main__":
    for name in WANTED:
        try:
            print("ok:", convert(name))
        except FileNotFoundError as error:
            print("skipped:", error, file=sys.stderr)

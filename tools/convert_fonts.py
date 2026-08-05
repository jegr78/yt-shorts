"""Converts a racecast runtime's woff2 fonts to ttf.

Pillow can't read woff2, fontTools can. Run once, naming the runtime's font
directory:

    python tools/convert_fonts.py ~/racecast/runtime/fonts

The source directory is an argument rather than a constant. It used to be a
hardcoded path into one machine's home directory, which a public repository
cannot carry - and which was never right for anyone else anyway, since that
runtime belongs to a separate project each operator keeps wherever they like.
"""
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

TARGET = Path(__file__).resolve().parent.parent / "channels" / "erf" / "fonts"
WANTED = ["Oswald-Bold", "BarlowCondensed-Bold", "Archivo-Bold"]


def convert(name: str, source_dir: Path) -> Path:
    source = source_dir / f"{name}.woff2"
    if not source.exists():
        raise FileNotFoundError(f"Font missing: {source}")
    TARGET.mkdir(parents=True, exist_ok=True)
    target = TARGET / f"{name}.ttf"
    font = TTFont(str(source))
    font.flavor = None
    font.save(str(target))
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} <woff2-source-directory>\n"
                 f"  e.g. {Path(sys.argv[0]).name} ~/racecast/runtime/fonts")
    directory = Path(sys.argv[1]).expanduser()
    if not directory.is_dir():
        sys.exit(f"not a directory: {directory}")
    for name in WANTED:
        try:
            print("ok:", convert(name, directory))
        except FileNotFoundError as error:
            print("skipped:", error, file=sys.stderr)

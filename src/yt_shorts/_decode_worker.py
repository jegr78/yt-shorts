"""Decode one wav in a separate, killable process (see stage D2a design).

A hung Whisper decode cannot be stopped in-process - see transcribe.py's
"Unbounded decode" note - so a stream chunk decodes here, where the parent can
kill the whole process on timeout. Prints the chunk's words as JSON on stdout.
"""

import json
import sys

from .glossary import Glossary
from .transcribe import decode_wav


def main(argv: list[str]) -> int:
    wav, model = argv[1], argv[2]
    glossary = Glossary(terms=[], replacements={})
    if len(argv) > 3 and argv[3]:
        with open(argv[3], encoding="utf-8") as handle:
            data = json.load(handle)
        # `replacements` defaulting to {} is load-bearing, not defensive
        # boilerplate: the stream path (stream_transcribe.subprocess_decoder)
        # deliberately writes a TERMS-ONLY payload, because decode_wav applies
        # replacements unconditionally and would therefore cache corrected
        # text - which defeats a correction spanning a chunk boundary and the
        # "a glossary edit needs no re-decode" property both. Do not make this
        # payload's keys mandatory, and do not change what an absent
        # `replacements` means.
        glossary = Glossary(terms=data.get("terms", []),
                            replacements=data.get("replacements", {}))
    words, _dropped = decode_wav(wav, model_name=model, glossary=glossary)
    json.dump(words, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

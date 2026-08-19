#!/usr/bin/env python3
"""
Extract distinct CJK ideograph characters appearing anywhere in a file.

Deliberately format-agnostic: scans every character of the input regardless
of CSV/JSON structure, so it works unmodified on rutopio's data-gov.csv,
ChineseNames' givenname.csv, or any other file where the characters you want
are mixed in among Latin/Hangul/numeric columns you don't care about --
those columns simply contribute no CJK codepoints.

USAGE:
  python3 extract_cjk_chars.py <input_file> > chars.txt
"""
from __future__ import annotations

import io
import sys

# CJK Unified Ideographs + the extension blocks most likely to appear in a
# personal/given-name character list. Not exhaustive of every CJK block
# (no Bopomofo, no Kangxi radicals) -- deliberately: those aren't hanzi/hanja.
CJK_RANGES = [
    (0x3400, 0x4DBF),    # Extension A
    (0x4E00, 0x9FFF),    # Unified Ideographs (the vast majority of hits)
    (0xF900, 0xFAFF),    # Compatibility Ideographs
    (0x20000, 0x2A6DF),  # Extension B
    (0x2A700, 0x2EBEF),  # Extensions C-F
]


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in CJK_RANGES)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: extract_cjk_chars.py <input_file>", file=sys.stderr)
        sys.exit(2)

    path = sys.argv[1]
    chars: set[str] = set()
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            for ch in line:
                if is_cjk(ch):
                    chars.add(ch)

    for ch in sorted(chars):
        print(ch)
    print(f"# distinct CJK characters found: {len(chars)}", file=sys.stderr)


if __name__ == "__main__":
    main()
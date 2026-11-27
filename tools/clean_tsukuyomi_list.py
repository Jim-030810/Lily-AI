"""
clean_tsukuyomi_list.py

Cleans a GPT-SoVITS training .list file whose transcript text still
contains voice-actor script annotations that a g2p/phonemizer can't
handle:

  - Pitch-accent arrows: ↓ ↑ (mora rise/fall markers)
  - Furigana/reading annotations in parentheses: 東寺（と↓う↑じ↑） -> 東寺
  - Word-boundary slashes: ニュー/イングランド -> ニューイングランド

Each line of the input .list is expected in GPT-SoVITS's format:
    <wav_path>|<speaker>|<lang>|<transcript text>

Only the 4th field (transcript) is modified. The first three fields
are passed through unchanged.

Usage:
    python clean_tsukuyomi_list.py input.list output.list
"""
import re
import sys

# Fullwidth or halfwidth parenthetical furigana/reading annotations.
# These are readings for the preceding word already spelled out in
# kanji/katakana in the main text, so the whole parenthetical
# (including the parens) can just be dropped.
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")

# Pitch-accent rise/fall arrows, which can appear standalone in the
# middle of a word (not just inside parens).
_ARROW_RE = re.compile(r"[↓↑]")

# Word-boundary slashes used in the voice-actor script to mark
# compound-word breaks in loanwords. Safe to just delete -- the words
# read continuously either way.
_SLASH_RE = re.compile(r"/")

# Collapse any run of spaces left behind after stripping the above.
_MULTI_SPACE_RE = re.compile(r"\s{2,}")


def clean_transcript(text: str) -> str:
    text = _PAREN_RE.sub("", text)
    text = _ARROW_RE.sub("", text)
    text = _SLASH_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text.strip()


def clean_list_file(in_path: str, out_path: str) -> None:
    kept = 0
    skipped = 0
    with open(in_path, "r", encoding="utf-8") as fin, \
         open(out_path, "w", encoding="utf-8") as fout:
        for line_num, raw_line in enumerate(fin, 1):
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("|", 3)
            if len(parts) != 4:
                print(f"[skip] line {line_num}: expected 4 '|'-separated fields, got {len(parts)}")
                skipped += 1
                continue
            wav_path, speaker, lang, transcript = parts
            cleaned = clean_transcript(transcript)
            if not cleaned:
                print(f"[skip] line {line_num}: transcript empty after cleaning ({wav_path})")
                skipped += 1
                continue
            fout.write(f"{wav_path}|{speaker}|{lang}|{cleaned}\n")
            kept += 1
    print(f"Done. Kept {kept} lines, skipped {skipped}. Wrote: {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python clean_tsukuyomi_list.py <input.list> <output.list>")
        sys.exit(1)
    clean_list_file(sys.argv[1], sys.argv[2])
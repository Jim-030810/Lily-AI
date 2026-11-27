"""
make_labs.py
============
Generates one .txt label file per .wav in the ITA corpus folders,
matching each audio file to its official transcript line.

Expects these two files to already be downloaded into the corpus root
(ITAcorpus_amitaro_runrun/ITAcorpus_amitaro_runrun/):
    emotion_transcript_utf8.txt
    recitation_transcript_utf8.txt

Transcript line format (comma-separated):
    EMOTION100_001:えっ嘘でしょ。,エッウソデショ。
    RECITATION324_001:<kanji text>,<katakana reading>

For each wav found under emotion/48k and rectation/48k, writes a
same-named .txt file containing just the kanji/kana text (the part
before the first comma, with the "KEY:" prefix stripped) -- this is
what GPT-SoVITS's own transcription/labeling step expects to find.

Run from anywhere; just make sure CORPUS_ROOT below matches your setup.
"""

import os
import re

# ── CONFIG -- adjust if your paths differ ──
TRANSCRIPT_ROOT = r"C:\local_llm\fish-speech\references\clips\ITAcorpus_amitaro_runrun\ITAcorpus_amitaro_runrun"

STYLE_PACKS = [
    {
        "name": "runrun",
        "root": r"C:\local_llm\fish-speech\references\clips\ITAcorpus_amitaro_runrun\ITAcorpus_amitaro_runrun",
        "emotion_dir": "emotion/48k",
        "recitation_dir_candidates": ["rectation/48k", "recitation/48k"],
        "filename_style": "key",
    },
    {
        "name": "yofukashi",
        "root": r"C:\local_llm\fish-speech\references\clips\ITAcorpus_amitaro_yofukashi_1.1\ITAcorpus_amitaro_yofukashi_1.1",
        "emotion_dir": "48k/emotion",
        "recitation_dir_candidates": ["48k/recitation"],
        "filename_style": "key",
    },
    {
        "name": "punsuka",
        "root": r"C:\local_llm\fish-speech\references\clips\ITAcorpus_amitaro_punsuka_1.0\ITAcorpus_amitaro_punsuka_1.0",
        "emotion_dir": "48k/emotion",
        "recitation_dir_candidates": ["48k/recitation"],
        "filename_style": "text",
    },
]

EMOTION_TRANSCRIPT = os.path.join(TRANSCRIPT_ROOT, "emotion_transcript_utf8.txt")
RECITATION_TRANSCRIPT = os.path.join(TRANSCRIPT_ROOT, "recitation_transcript_utf8.txt")

# The existing training list for her current voice (original game-line
# clips). New entries get appended after these so the original identity
# is preserved in the training set, not replaced.
EXISTING_LIST_FILE = r"C:\local_llm\fish-speech\references\clips\lily_train.list"
OUTPUT_LIST_FILE = r"C:\local_llm\fish-speech\references\clips\lily_train_expanded.list"
SPEAKER_NAME = "lily"


def load_transcript(path):
    """Parses a transcript file into {key: text} e.g. {'EMOTION100_001': 'えっ嘘でしょ。'}"""
    mapping = {}
    if not os.path.isfile(path):
        print(f"⚠️  Transcript file not found, skipping: {path}")
        return mapping

    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            # Format: KEY:text,reading
            m = re.match(r"^([A-Za-z0-9_]+):(.+?),(.+)$", line)
            if not m:
                print(f"  (line {line_num} didn't match expected format, skipping): {line[:50]!r}")
                continue
            key, text, _reading = m.groups()
            mapping[key] = text.strip()

    print(f"✅ Loaded {len(mapping)} entries from {os.path.basename(path)}")
    return mapping


def find_wav_dir(root, candidates):
    for c in candidates:
        full = os.path.join(root, *c.split("/"))
        if os.path.isdir(full):
            return full
    return None


def build_text_lookup(transcript_map):
    """For punsuka-style packs where the filename IS the sentence text
    (e.g. 'えっ嘘でしょ。.wav'), build a reverse lookup: text -> text
    (identity), just so we can confirm the file's own name is a known
    corpus sentence rather than something unrelated."""
    return {text: text for text in transcript_map.values()}


def write_labels(wav_dir, transcript_map, filename_style, dry_run=False, list_lines=None):
    if not wav_dir or not os.path.isdir(wav_dir):
        print(f"⚠️  Wav directory not found, skipping: {wav_dir}")
        return 0, 0

    written = 0
    missing = 0
    text_lookup = build_text_lookup(transcript_map) if filename_style == "text" else {}

    for fname in sorted(os.listdir(wav_dir)):
        if not fname.lower().endswith(".wav"):
            continue

        stem = os.path.splitext(fname)[0]

        if filename_style == "key":
            # e.g. "EMOTION100_001.wav" -> look up key "EMOTION100_001"
            text = transcript_map.get(stem)
        else:
            # e.g. "えっ嘘でしょ。.wav" -> the stem itself IS the text
            text = text_lookup.get(stem, stem)  # fall back to stem even if not matched

        if text is None:
            print(f"  ❌ No transcript found for {fname}")
            missing += 1
            continue

        label_path = os.path.join(wav_dir, stem + ".txt")
        wav_path = os.path.join(wav_dir, fname)

        if dry_run:
            print(f"  (dry run) would write {label_path} -> {text}")
        else:
            with open(label_path, "w", encoding="utf-8") as f:
                f.write(text)

        if list_lines is not None:
            # GPT-SoVITS manifest format: path|speaker|lang|text
            list_lines.append(f"{wav_path.replace(chr(92), '/')}|{SPEAKER_NAME}|ja|{text}")

        written += 1

    return written, missing


def build_expanded_list(new_lines, dry_run=False):
    """Combines the existing training list (her current voice identity)
    with the newly generated ITA corpus lines, writing the result to
    OUTPUT_LIST_FILE. The original file is never modified in place --
    this way lily_train.list stays untouched as a fallback/backup."""
    existing_lines = []
    if os.path.isfile(EXISTING_LIST_FILE):
        with open(EXISTING_LIST_FILE, "r", encoding="utf-8") as f:
            existing_lines = [line.rstrip("\n") for line in f if line.strip()]
        print(f"✅ Loaded {len(existing_lines)} existing line(s) from {os.path.basename(EXISTING_LIST_FILE)}")
    else:
        print(f"ℹ️  No existing list file found at {EXISTING_LIST_FILE} -- "
              f"building a fresh list from the new ITA corpus data only. "
              f"This is fine if her original training list was already "
              f"consumed/deleted after the first training run.")

    combined = existing_lines + new_lines
    print(f"📝 Combined list: {len(existing_lines)} existing + {len(new_lines)} new = {len(combined)} total")

    if dry_run:
        print(f"(dry run) would write {len(combined)} lines to {OUTPUT_LIST_FILE}")
    else:
        with open(OUTPUT_LIST_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(combined) + "\n")
        print(f"✅ Wrote combined list to {OUTPUT_LIST_FILE}")


def main(dry_run=False):
    print("=" * 60)
    print("ITA Corpus Label Generator (multi-style)")
    print("=" * 60)

    emotion_map = load_transcript(EMOTION_TRANSCRIPT)
    recitation_map = load_transcript(RECITATION_TRANSCRIPT)

    grand_written = 0
    grand_missing = 0
    new_list_lines = []

    for pack in STYLE_PACKS:
        print(f"\n### Style pack: {pack['name']} ###")

        emo_dir = os.path.join(pack["root"], *pack["emotion_dir"].split("/"))
        print(f"--- {pack['name']} / emotion ---")
        w, m = write_labels(emo_dir, emotion_map, pack["filename_style"], dry_run=dry_run, list_lines=new_list_lines)
        print(f"✅ Wrote {w} label(s), {m} missing transcript(s)")
        grand_written += w
        grand_missing += m

        rec_dir = find_wav_dir(pack["root"], pack["recitation_dir_candidates"])
        print(f"--- {pack['name']} / recitation ---")
        w2, m2 = write_labels(rec_dir, recitation_map, pack["filename_style"], dry_run=dry_run, list_lines=new_list_lines)
        print(f"✅ Wrote {w2} label(s), {m2} missing transcript(s)")
        grand_written += w2
        grand_missing += m2

    print("\n" + "=" * 60)
    print(f"GRAND TOTAL: {grand_written} labels written, {grand_missing} missing")
    print("=" * 60)

    print("\n--- Building combined training list ---")
    build_expanded_list(new_list_lines, dry_run=dry_run)


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if dry:
        print("(dry run mode -- no files will be written)\n")
    main(dry_run=dry)
"""
flatten_corpus.py
==================
Fixes the filename-collision problem across the three ITA style packs
(runrun / yofukashi / punsuka). GPT-SoVITS's "1-dataset formatting"
step flattens whatever folder you point it at, keyed only by basename
-- so three files all called EMOTION100_001.wav (one per style pack)
step on each other and the tool can't resolve which physical file a
given path/label belongs to.

This script:
  1. Walks each style pack's emotion + recitation wav folders.
  2. Copies each .wav into ONE flat output folder, renamed with a
     style prefix so every filename is globally unique
     (e.g. runrun_EMOTION100_001.wav, yofukashi_EMOTION100_001.wav).
  3. Copies/writes the matching .txt label alongside it (same stem),
     reading the text from the sidecar .txt files make_labs.py already
     produced -- no need to re-parse the transcript files.
  4. Rebuilds a training manifest (.list) that points at the new flat
     paths, prepended with the existing lily_train.list so her
     original voice identity data is preserved.

Run make_labs.py FIRST -- this script depends on the per-wav .txt
label files it writes.

Usage:
    python flatten_corpus.py --dry-run   # preview only, writes nothing
    python flatten_corpus.py             # actually copy files
"""

import os
import shutil
import sys

# ── CONFIG -- keep in sync with make_labs.py ──
STYLE_PACKS = [
    {
        "name": "runrun",
        "root": r"C:\local_llm\fish-speech\references\clips\ITAcorpus_amitaro_runrun\ITAcorpus_amitaro_runrun",
        "emotion_dir": "emotion/48k",
        "recitation_dir_candidates": ["rectation/48k", "recitation/48k"],
    },
    {
        "name": "yofukashi",
        "root": r"C:\local_llm\fish-speech\references\clips\ITAcorpus_amitaro_yofukashi_1.1\ITAcorpus_amitaro_yofukashi_1.1",
        "emotion_dir": "48k/emotion",
        "recitation_dir_candidates": ["48k/recitation"],
    },
    {
        "name": "punsuka",
        "root": r"C:\local_llm\fish-speech\references\clips\ITAcorpus_amitaro_punsuka_1.0\ITAcorpus_amitaro_punsuka_1.0",
        "emotion_dir": "48k/emotion",
        "recitation_dir_candidates": ["48k/recitation"],
    },
]

FLAT_OUTPUT_DIR = r"C:\local_llm\fish-speech\references\clips\lily_flat"

# Existing training list for her current voice identity (untouched, just read).
EXISTING_LIST_FILE = r"C:\local_llm\fish-speech\references\clips\lily_train.list"
OUTPUT_LIST_FILE = r"C:\local_llm\fish-speech\references\clips\lily_train_flat.list"
SPEAKER_NAME = "lily"


def find_wav_dir(root, candidates):
    for c in candidates:
        full = os.path.join(root, *c.split("/"))
        if os.path.isdir(full):
            return full
    return None


def flatten_dir(wav_dir, style_name, dry_run, list_lines, stats):
    if not wav_dir or not os.path.isdir(wav_dir):
        print(f"⚠️  Wav directory not found, skipping: {wav_dir}")
        return

    for fname in sorted(os.listdir(wav_dir)):
        if not fname.lower().endswith(".wav"):
            continue

        stem = os.path.splitext(fname)[0]
        label_path = os.path.join(wav_dir, stem + ".txt")

        if not os.path.isfile(label_path):
            print(f"  ❌ No label .txt for {fname} in {wav_dir} (run make_labs.py first) -- skipping")
            stats["missing_label"] += 1
            continue

        with open(label_path, "r", encoding="utf-8") as f:
            text = f.read().strip()

        new_stem = f"{style_name}_{stem}"
        new_wav_name = new_stem + ".wav"
        new_txt_name = new_stem + ".txt"

        src_wav = os.path.join(wav_dir, fname)
        dst_wav = os.path.join(FLAT_OUTPUT_DIR, new_wav_name)
        dst_txt = os.path.join(FLAT_OUTPUT_DIR, new_txt_name)

        if os.path.exists(dst_wav):
            # Should never happen since style prefix guarantees uniqueness,
            # but guard against it rather than silently overwriting.
            print(f"  ⚠️  Unexpected duplicate target, skipping: {dst_wav}")
            stats["dup"] += 1
            continue

        if dry_run:
            print(f"  (dry run) would copy {src_wav} -> {dst_wav}")
        else:
            shutil.copy2(src_wav, dst_wav)
            with open(dst_txt, "w", encoding="utf-8") as f:
                f.write(text)

        list_lines.append(f"{dst_wav.replace(chr(92), '/')}|{SPEAKER_NAME}|ja|{text}")
        stats["copied"] += 1


def build_combined_list(new_lines, dry_run):
    existing_lines = []
    if os.path.isfile(EXISTING_LIST_FILE):
        with open(EXISTING_LIST_FILE, "r", encoding="utf-8") as f:
            existing_lines = [line.rstrip("\n") for line in f if line.strip()]
        print(f"✅ Loaded {len(existing_lines)} existing line(s) from {os.path.basename(EXISTING_LIST_FILE)}")
    else:
        print(f"ℹ️  No existing list file found at {EXISTING_LIST_FILE} -- building fresh from flat corpus only.")

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
    print("ITA Corpus Flattener (fixes cross-pack filename collisions)")
    print("=" * 60)

    if not dry_run:
        os.makedirs(FLAT_OUTPUT_DIR, exist_ok=True)

    list_lines = []
    stats = {"copied": 0, "missing_label": 0, "dup": 0}

    for pack in STYLE_PACKS:
        print(f"\n### Style pack: {pack['name']} ###")

        emo_dir = os.path.join(pack["root"], *pack["emotion_dir"].split("/"))
        print(f"--- {pack['name']} / emotion ---")
        flatten_dir(emo_dir, pack["name"], dry_run, list_lines, stats)

        rec_dir = find_wav_dir(pack["root"], pack["recitation_dir_candidates"])
        print(f"--- {pack['name']} / recitation ---")
        flatten_dir(rec_dir, pack["name"], dry_run, list_lines, stats)

    print("\n" + "=" * 60)
    print(f"GRAND TOTAL: {stats['copied']} copied, {stats['missing_label']} missing labels, {stats['dup']} unexpected dupes")
    print(f"Flat folder: {FLAT_OUTPUT_DIR}")
    print("=" * 60)

    print("\n--- Building combined training list ---")
    build_combined_list(list_lines, dry_run=dry_run)

    print("\n➡️  Next step in the GPT-SoVITS webui:")
    print(f"   Set 'Audio dataset folder' to: {FLAT_OUTPUT_DIR}")
    print(f"   Set the training list path to: {OUTPUT_LIST_FILE}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if dry:
        print("(dry run mode -- no files will be written)\n")
    main(dry_run=dry)
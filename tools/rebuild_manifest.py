"""
rebuild_manifest.py
====================
Scans the already-flattened lily_flat folder directly and rebuilds
lily_train_flat.list from the wav/txt pairs sitting there.
"""

import os

FLAT_DIR = r"C:\local_llm\fish-speech\references\clips\lily_flat"
EXISTING_LIST_FILE = r"C:\local_llm\fish-speech\references\clips\lily_train.list"
OUTPUT_LIST_FILE = r"C:\local_llm\fish-speech\references\clips\lily_train_flat.list"
SPEAKER_NAME = "lily"


def main():
    new_lines = []
    missing_txt = 0

    for fname in sorted(os.listdir(FLAT_DIR)):
        if not fname.lower().endswith(".wav"):
            continue
        stem = os.path.splitext(fname)[0]
        txt_path = os.path.join(FLAT_DIR, stem + ".txt")
        wav_path = os.path.join(FLAT_DIR, fname)

        if not os.path.isfile(txt_path):
            print(f"  ❌ No label for {fname}, skipping")
            missing_txt += 1
            continue

        with open(txt_path, "r", encoding="utf-8") as f:
            text = f.read().strip()

        new_lines.append(f"{wav_path.replace(chr(92), '/')}|{SPEAKER_NAME}|ja|{text}")

    print(f"✅ Found {len(new_lines)} wav/txt pairs in {FLAT_DIR} ({missing_txt} missing labels)")

    existing_lines = []
    if os.path.isfile(EXISTING_LIST_FILE):
        with open(EXISTING_LIST_FILE, "r", encoding="utf-8") as f:
            existing_lines = [line.rstrip("\n") for line in f if line.strip()]
        print(f"✅ Loaded {len(existing_lines)} existing line(s) from {os.path.basename(EXISTING_LIST_FILE)}")
    else:
        print(f"ℹ️  No existing list file at {EXISTING_LIST_FILE}, building from flat corpus only.")

    combined = existing_lines + new_lines
    with open(OUTPUT_LIST_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(combined) + "\n")

    print(f"📝 Wrote {len(combined)} total lines to {OUTPUT_LIST_FILE}")


if __name__ == "__main__":
    main()
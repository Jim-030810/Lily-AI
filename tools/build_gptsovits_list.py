"""
Converts your existing sample*.wav + sample*.lab clips into the .list
format GPT-SoVITS expects for training:

    audio_path|speaker_name|language|transcript_text

Excludes sample3 (confirmed not to be the real speaker's voice).

Run with your MAIN venv python:
    & c:/local_llm/.venv/Scripts/python.exe C:\\local_llm\\build_gptsovits_list.py
"""

import os
import glob

CLIPS_DIR = r"C:\local_llm\fish-speech\references\clips"
OUTPUT_LIST = r"C:\local_llm\GPT-SoVITS\lily_dataset.list"

SPEAKER_NAME = "lily"
LANGUAGE = "ja"  # Japanese

EXCLUDE = {"sample3"}  # confirmed not the real speaker's voice


def main():
    wav_files = sorted(glob.glob(os.path.join(CLIPS_DIR, "sample*.wav")))
    entries = []
    skipped = []

    for wav_path in wav_files:
        base = os.path.splitext(os.path.basename(wav_path))[0]  # e.g. "sample14"
        if base in EXCLUDE:
            skipped.append(base)
            continue

        lab_path = os.path.join(CLIPS_DIR, base + ".lab")
        if not os.path.isfile(lab_path):
            print(f"WARNING: no matching .lab file for {wav_path}, skipping.")
            continue

        with open(lab_path, "r", encoding="utf-8") as f:
            text = f.read().strip()

        if not text:
            print(f"WARNING: {lab_path} is empty, skipping.")
            continue

        # Use absolute path with forward slashes (GPT-SoVITS reads this fine on Windows)
        abs_wav_path = os.path.abspath(wav_path).replace("\\", "/")
        entries.append(f"{abs_wav_path}|{SPEAKER_NAME}|{LANGUAGE}|{text}")

    with open(OUTPUT_LIST, "w", encoding="utf-8") as f:
        f.write("\n".join(entries) + "\n")

    print(f"Wrote {len(entries)} entries to {OUTPUT_LIST}")
    if skipped:
        print(f"Excluded: {', '.join(skipped)}")


if __name__ == "__main__":
    main()
"""
Splice multiple clean dialogue clips into one richer reference audio
file for Fish Speech voice cloning.

Automatically trims leading/trailing silence from each clip before
splicing, so blank/dead-air portions don't dilute the reference.

USAGE:
1. Put your clip files (wav/mp3/m4a/mp4/etc - any format ffmpeg supports)
   in a list below, IN THE ORDER you want them spliced together.
2. Run this script. It outputs a combined wav to:
   C:\\local_llm\\fish-speech\\references\\lily_voice_combined.wav
3. Manually write down the exact transcript of each clip, in the same
   order, and combine them (with a space between) into your new
   FISH_SPEECH_REFERENCE_TEXT in lily_bot.py.
4. Update FISH_SPEECH_REFERENCE_AUDIO_PATH in lily_bot.py to point at
   lily_voice_combined.wav instead of lily_voice.wav.
"""

import os
from typing import cast
from pydub import AudioSegment
from pydub.silence import detect_leading_silence

# ffmpeg path (same one Lily uses)
FFMPEG_PATH = r"C:\local_llm\ffmpeg\bin\ffmpeg.exe"
if os.path.isfile(FFMPEG_PATH):
    AudioSegment.converter = FFMPEG_PATH

# ============================================================
# EDIT THIS LIST: paths to your clean clips, in desired order
# ============================================================
CLIP_PATHS = (
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_001.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_002.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_003.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_004.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_005.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_006.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_007.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_008.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_009.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_010.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_011.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_012.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_013.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_014.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_015.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_016.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_017.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_018.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_019.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_020.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_021.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_022.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_023.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_024.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_025.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_026.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_027.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_028.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_029.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_030.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_031.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_032.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_033.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_034.wav",
    r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_035.wav",
)


OUTPUT_PATH = r"C:\local_llm\fish-speech\references\clips\tsukuyomi_vol1\02_wav_boosted\VOICEACTRESS100_100.wav"
SILENCE_BETWEEN_MS = 300   # short pause inserted between spliced clips
SILENCE_THRESHOLD_DB = -40  # anything quieter than this is "silence" for trimming
EDGE_PADDING_MS = 80        # tiny buffer kept at the start/end of each clip

# Target format - must match what Fish Speech expects
TARGET_SR = 44100
TARGET_CHANNELS = 1  # mono


def load_and_normalize(path: str) -> AudioSegment:
    audio = AudioSegment.from_file(path)
    audio = audio.set_frame_rate(TARGET_SR)
    audio = audio.set_channels(TARGET_CHANNELS)
    audio = audio.set_sample_width(2)  # 16-bit
    return audio


def trim_silence(audio: AudioSegment) -> AudioSegment:
    """Trim leading/trailing silence, keeping a small edge buffer."""
    start_trim = detect_leading_silence(audio, silence_threshold=SILENCE_THRESHOLD_DB)
    end_trim = detect_leading_silence(
        audio.reverse(), silence_threshold=SILENCE_THRESHOLD_DB
    )

    start = max(0, start_trim - EDGE_PADDING_MS)
    end = max(0, len(audio) - max(0, end_trim - EDGE_PADDING_MS))

    if start >= end:
        # Clip is entirely silence or trimming logic failed - return as-is
        return audio
    return cast(AudioSegment, audio[start:end])


def main():
    missing = [p for p in CLIP_PATHS if not os.path.isfile(p)]
    if missing:
        print("ERROR: these clip files were not found:")
        for m in missing:
            print(f"  - {m}")
        print("\nEdit CLIP_PATHS in this script to point at your actual clip files.")
        return

    print(f"Loading and trimming {len(CLIP_PATHS)} clips...")
    silence = AudioSegment.silent(duration=SILENCE_BETWEEN_MS, frame_rate=TARGET_SR)

    combined = AudioSegment.empty()
    total_duration = 0.0
    for i, path in enumerate(CLIP_PATHS):
        raw = load_and_normalize(path)
        raw_dur = len(raw) / 1000.0

        trimmed = trim_silence(raw)
        trimmed_dur = len(trimmed) / 1000.0
        total_duration += trimmed_dur

        cut_amount = raw_dur - trimmed_dur
        print(
            f"  [{i+1}] {os.path.basename(path)} - "
            f"{raw_dur:.2f}s -> {trimmed_dur:.2f}s "
            f"(trimmed {cut_amount:.2f}s of silence)"
        )

        combined += trimmed
        if i < len(CLIP_PATHS) - 1:
            combined += silence

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    combined.export(OUTPUT_PATH, format="wav")

    final_dur = len(combined) / 1000.0
    print(f"\nSaved combined reference: {OUTPUT_PATH}")
    print(f"Total spoken duration (silence-trimmed): {total_duration:.2f}s")
    print(f"Total file duration (with gaps): {final_dur:.2f}s")
    print("\nNEXT STEP: write down the exact transcript of each clip above,")
    print("in order, and combine them into your new FISH_SPEECH_REFERENCE_TEXT.")
    print(
        "\nNote: if a clip looks like it got trimmed too aggressively or not "
        "enough, adjust SILENCE_THRESHOLD_DB (more negative = stricter) and "
        "re-run."
    )


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
r"""
render_song_rvc.py
-------------------
Turns a song (local audio file or YouTube URL) into a "Lily sings it"
final.wav, using YOUR trained RVC voice model, and drops it exactly where
lily_singing.py expects it: <songs_dir>/<song_name>/final.wav
(confirmed against the actual lily_singing.py contract: only final.wav is
required, plus an optional metadata.json with a "title" field used for
fuzzy voice-request matching -- this script writes that too now.)

Why this exists:
  GPT-SoVITS (used for talking) is text-to-speech -- it can't hold a melody.
  RVC is voice CONVERSION -- it takes an existing vocal recording and swaps
  the timbre while keeping the original pitch/rhythm. That's why singing
  needs a pre-rendered file instead of anything generated live: you convert
  the vocals ahead of time, once, per song.

Pipeline:
  1. (optional) download audio from a YouTube URL with yt-dlp
  2. separate vocals from instrumental with Demucs
  3. run the isolated vocals through your RVC model  (rvc-python)
  4. remix converted vocals + instrumental with pydub
  5. write <songs_dir>/<song_name>/final.wav + metadata.json

Install (Windows, matches the rest of your local_llm stack):
  pip install demucs rvc-python yt-dlp pydub
  # GPU (recommended, you already have CUDA set up for Whisper/GPT-SoVITS):
  pip install torch --index-url https://download.pytorch.org/whl/cu118  --force-reinstall
  # ffmpeg must be on PATH (you already have this, per your Whisper/pydub setup)

Usage:
  # from a local file you already have (e.g. an acapella/karaoke rip):
  python render_song_rvc.py --input "C:\path\to\song.mp3" --song-name "world_execute_me" ^
      --title "World.execute(me)" --model "C:\rvc_models\lily\lily.pth" --index "C:\rvc_models\lily\lily.index"

  # straight from YouTube:
  python render_song_rvc.py --url "https://youtu.be/XXXXXXXX" --song-name "world_execute_me" ^
      --title "World.execute(me)" --model "C:\rvc_models\lily\lily.pth" --index "C:\rvc_models\lily\lily.index" ^
      --pitch 0

Notes:
  - --pitch is in semitones. If Lily's model was trained on a voice with a
    different range than the original singer, you'll likely need +/- a few
    semitones (try 0 first, then +/-3, +/-12 if it sounds like a different
    octave entirely).
  - --index is optional but improves conversion quality/timbre stability
    if you have one alongside the .pth. As of this version it is actually
    wired into rvc-python's load_model() call (see FIX notes below) --
    earlier versions of this script silently dropped it.
  - --title controls what resolve_song() fuzzy-matches spoken requests
    against (e.g. "sing world execute me" will match a title containing
    that phrase even if --song-name is a plainer slug). If omitted, it
    falls back to --song-name, same as lily_singing.py's own default.
  - lily_singing.py has a TEST_SINGING_MODE flag that, when True, always
    performs songs/test_song/final.wav no matter what was asked for --
    make sure that's set to False (it now is, if you applied the patched
    version) or your new song will render fine but never actually play.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def check_tool(module_name: str, pip_name: str) -> None:
    try:
        __import__(module_name)
    except ImportError:
        die(f"Missing dependency '{module_name}'. Install it with: pip install {pip_name}")


def download_youtube(url: str, out_dir: Path) -> Path:
    """Download best audio from a YouTube URL using yt-dlp, return the wav path."""
    check_tool("yt_dlp", "yt-dlp")
    import yt_dlp

    out_template = str(out_dir / "source.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
            "preferredquality": "0",
        }],
        "quiet": False,
        "noplaylist": True,
    }
    print(f"[1/4] Downloading audio from {url!r} ...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    wav_path = out_dir / "source.wav"
    if not wav_path.exists():
        # some postprocessor configs land on a different extension; find it
        candidates = list(out_dir.glob("source.*"))
        if not candidates:
            die("yt-dlp finished but no output file was found.")
        wav_path = candidates[0]
    return wav_path


def separate_vocals(source_audio: Path, work_dir: Path) -> tuple[Path, Path]:
    """Run Demucs two-stem separation. Returns (vocals_path, instrumental_path)."""
    check_tool("demucs", "demucs")
    print("[2/4] Separating vocals from instrumental (Demucs, this can take a bit)...")

    out_dir = work_dir / "demucs_out"
    out_dir.mkdir(exist_ok=True)

    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",
        "-o", str(out_dir),
        str(source_audio),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        die("Demucs separation failed -- see output above.")

    # Demucs writes to <out_dir>/<model_name>/<track_stem>/{vocals,no_vocals}.wav
    stem = source_audio.stem
    matches = list(out_dir.glob(f"*/{stem}/vocals.wav"))
    if not matches:
        die(f"Couldn't find Demucs output for stem '{stem}' under {out_dir}")
    vocals_path = matches[0]
    instrumental_path = vocals_path.parent / "no_vocals.wav"
    if not instrumental_path.exists():
        die(f"Expected instrumental track missing: {instrumental_path}")

    return vocals_path, instrumental_path


def convert_vocals_rvc(
    vocals_path: Path,
    model_path: Path,
    index_path: Path | None,
    pitch: int,
    device: str,
    work_dir: Path,
) -> Path:
    """Run the isolated vocals through the RVC model."""
    check_tool("rvc_python", "rvc-python")
    from rvc_python.infer import RVCInference

    print(f"[3/4] Converting vocals through RVC model {model_path.name} (device={device})...")

    rvc = RVCInference(device=device)

    # FIX: index_path must be passed directly into load_model() -- it is
    # NOT a valid set_params() key in rvc-python 0.1.5 (confirmed by
    # reading the installed package source). Passing it via set_params()
    # gets silently dropped ("parameter index_path not recognized and
    # will be ignored"), which means every earlier render used no index
    # at all -- the single biggest cause of flat/blurry-sounding
    # conversion, since the index is what carries fine-grained timbre
    # detail from training. Real signature:
    #   load_model(self, model_path_or_name, version="v2", index_path="")
    rvc.load_model(
        str(model_path),
        version="v2",
        index_path=str(index_path) if index_path is not None else "",
    )

    # FIX: valid set_params() keys (confirmed from installed source) are
    # exactly: index_rate, filter_radius, resample_sr, rms_mix_rate,
    # protect, f0up_key, f0method -- note "f0method", NOT "f0_method".
    # Default f0method is "harvest"; rmvpe is the stronger pitch tracker
    # and is already downloaded/cached locally (confirmed by "File
    # rmvpe.pt successfully loaded" in earlier run logs), so this
    # switches to it explicitly instead of silently staying on the
    # weaker default -- this is what was producing the monotone-ish
    # pitch tracking.
    params: dict[str, int | float | str] = {
        "f0up_key": pitch,
        "f0method": "rmvpe",
        "protect": 0.5,
        "rms_mix_rate": 0.8, # default is 0.5; higher = more stable but less timbre detail
    }
    if index_path is not None:
        # index_rate: how strongly the index's stored timbre detail is
        # blended in. Library default is 0.5; 0.6-0.75 is a common
        # sweet spot for singing -- high enough to pull in real detail
        # without destabilizing the conversion. Tune per-song if needed.
        params["index_rate"] = 0.65
    try:
        if hasattr(rvc, "set_params"):
            rvc.set_params(**params)
        else:
            for k, v in params.items():
                setattr(rvc, k, v)
    except Exception as e:
        print(f"⚠️  Couldn't set RVC params ({e}); continuing with defaults.")

    converted_path = work_dir / "vocals_converted.wav"
    rvc.infer_file(str(vocals_path), str(converted_path))

    if not converted_path.exists():
        die("RVC conversion did not produce an output file.")
    return converted_path


def mix_final(
    converted_vocals: Path,
    instrumental: Path,
    output_path: Path,
    vocal_gain_db: float,
    inst_gain_db: float,
) -> None:
    """Remix converted vocals with the instrumental track."""
    check_tool("pydub", "pydub")
    from pydub import AudioSegment

    print("[4/4] Mixing converted vocals with instrumental...")

    vocals = AudioSegment.from_wav(converted_vocals) + vocal_gain_db
    inst = AudioSegment.from_wav(instrumental) + inst_gain_db

    # Match lengths so overlay doesn't cut either track short.
    length = max(len(vocals), len(inst))
    vocals = vocals + AudioSegment.silent(duration=max(0, length - len(vocals)))
    inst = inst + AudioSegment.silent(duration=max(0, length - len(inst)))

    mixed = inst.overlay(vocals)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mixed.export(output_path, format="wav")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=str, help="Path to a local audio file (mp3/wav/etc).")
    src.add_argument("--url", type=str, help="YouTube URL to download audio from.")

    parser.add_argument("--song-name", required=True, help="Folder name lily_singing.py will look this song up by (its 'id'). Keep it a plain slug, e.g. world_execute_me.")
    parser.add_argument("--title", default=None, help="Friendly title written to metadata.json, used by resolve_song() for fuzzy voice-request matching (e.g. 'World.execute(me)'). Defaults to --song-name if omitted.")
    parser.add_argument("--songs-dir", default="singing/songs", help="Base songs directory. Default matches the path seen in your logs: singing/songs/<name>/final.wav")
    parser.add_argument("--model", required=True, help="Path to your RVC .pth model for Lily's voice.")
    parser.add_argument("--index", default=None, help="Path to the matching .index file (optional, improves quality).")
    parser.add_argument("--pitch", type=int, default=0, help="Pitch shift in semitones for the RVC conversion (try 0 first).")
    parser.add_argument("--device", default="cuda:0", help="cuda:0 or cpu")
    parser.add_argument("--vocal-gain", type=float, default=0.0, help="dB adjustment on converted vocals in the final mix.")
    parser.add_argument("--inst-gain", type=float, default=0.0, help="dB adjustment on the instrumental in the final mix.")
    parser.add_argument("--keep-temp", action="store_true", help="Don't delete intermediate files (vocals/instrumental/etc).")

    args = parser.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        die(f"RVC model not found: {model_path}")
    index_path = Path(args.index) if args.index else None
    if index_path is not None and not index_path.exists():
        die(f"Index file not found: {index_path}")

    songs_dir = Path(args.songs_dir)
    output_path = songs_dir / args.song_name / "final.wav"

    work_dir = Path(tempfile.mkdtemp(prefix="lily_sing_"))
    print(f"(working in {work_dir})")

    try:
        if args.url:
            source_audio = download_youtube(args.url, work_dir)
        else:
            source_audio = Path(args.input)
            if not source_audio.exists():
                die(f"Input file not found: {source_audio}")
            print("[1/4] Using local input file.")

        vocals_path, instrumental_path = separate_vocals(source_audio, work_dir)
        converted_vocals = convert_vocals_rvc(
            vocals_path, model_path, index_path, args.pitch, args.device, work_dir,
        )
        mix_final(converted_vocals, instrumental_path, output_path, args.vocal_gain, args.inst_gain)

        metadata = {
            "id": args.song_name,
            "title": args.title or args.song_name,
            "final": "final.wav",
        }
        metadata_path = output_path.parent / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n✅ Done: {output_path}")
        print(f"   metadata.json written -> title: {metadata['title']!r}")
        print("   Restart the bot (or just ask her to sing) and it should pick this up.")
        print("   Make sure lily_singing.py has TEST_SINGING_MODE = False, or this will")
        print("   render fine but never actually get played.")
    finally:
        if not args.keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"(intermediate files kept at {work_dir})")


if __name__ == "__main__":
    main()
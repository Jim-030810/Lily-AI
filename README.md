* the loading will take long time depending on your setup. For my Omen 16 gaming laptop 16GB, it take 5 mins.

# Lily: a local AI Assistant

Lily is an AI character that runs on your own PC. She listens to your
microphone, thinks with a local LLM, answers in a cloned voice, and moves a
VRM avatar with lip-sync, idle animation, and facial expressions. She can
also read Twitch and Discord chat.

> **Status:** hobby project, Windows only, work in progress. Expect rough edges.

## Features
- Voice conversation in Japanese or English, with interruption (barge-in)
- Local LLM through [Ollama](https://ollama.com) (Qwen2.5), no cloud API needed
- Speech-to-text with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- Text-to-speech with [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)
- VRM avatar viewer (Electron + Vite) with lip-sync, gestures, and idle motion
- Long-term memory (ChromaDB), user profile, and relationship growth
- Web search with source-checked answers (DuckDuckGo)
- Screen understanding through a local vision model
- Music playback and a code-writing helper window
- Twitch chat and Discord input

## Requirements

- Windows 10/11, a recent NVIDIA GPU recommended
- Python 3.10
- Node.js (for the VRM viewer)
- [Ollama](https://ollama.com)
- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) with its API server (`api_v2.py`)
- [ffmpeg](https://ffmpeg.org/download.html)
- [VLC](https://www.videolan.org/vlc/) for music playback
- [Deno](https://deno.com), the JavaScript runtime that yt-dlp needs for music playback
- Optional: [VB-Cable](https://vb-audio.com/Cable/) (route her voice into OBS or Discord)
- Optional: XR Animator (motion capture over VMC)

## Setup

1. **Clone and install**
   ```powershell
   git clone https://github.com/Jim-030810/Lily-AI.git
   cd Lily-AI
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Pull the models**
   ```powershell
   ollama pull qwen2.5:7b
   ollama pull qwen2.5vl:7b
   ollama pull nomic-embed-text
   ```

3. **Install the VRM viewer's dependencies**
   ```powershell
   cd vrm-viewer
   npm install
   cd ..
   ```
   Put your own `.vrm` model where the viewer expects it. No avatar model is
   included, and every VRM has its own license.

4. **Create your settings files**
   ```powershell
   copy .env.example .env
   copy config.example.yaml config.yaml
   ```
   - `.env` holds secrets (Discord/Twitch tokens). Every one is optional.
   - `config.yaml` holds your paths: GPT-SoVITS location, model weights,
     reference voice clip, and so on. Both files are gitignored.

5. **Set up the voice**

   Lily's voice uses GPT-SoVITS zero-shot cloning from a short reference clip.
   Provide your own clip and its exact transcript in `config.yaml`
   (`tts.reference_audio_path` and `tts.reference_text`). Optionally load your
   own fine-tuned weights through `tts.gpt_weights_path` and
   `tts.sovits_weights_path`.

   **No voice data or weights are included in this repo.** Only use voices you
   have permission to clone, and follow the license of any voice corpus you use.

## Run

```powershell
python Lily.py
```
or double-click `start_lily.bat`. Use `stop_lily.bat` to stop her.

## Voice and text commands

| Say | What happens |
|---|---|
| `voice mode` / `text mode` / `discord mode` | Switch the input source |
| `speak english` / `日本語で話して` | Switch language |
| `search for <topic>` / `<topic>について調べて` | Web search, answered from the sources |
| `look at my screen` / `画面見て` | She describes what's on your screen |
| `play <song>` / `stop` | Music playback (she stays quiet until the song is over) |
| `write code for <thing>` | Sends generated code to the coding window |
| `goodbye` / `さようなら` | Shuts down |

In Twitch chat: `!ask <question>`, `!voice <style>`.

## Privacy

Everything runs locally, but be aware of this:

- **Screen capture:** when you ask her to look at your screen she takes a
  screenshot and sends it to your local Ollama vision model. Nothing is sent
  to a cloud service.
- **Memory:** conversations are saved on disk (`memory.json`, `vector_memory/`,
  and similar files). They stay on your machine and are gitignored. Delete
  them to reset her.
- **Web search** sends your query to DuckDuckGo.
- **Twitch/Discord** messages are passed to your local model.

## Limitations

- Windows only (`.bat` launchers, process-group handling)
- Needs a capable GPU to run the LLM, TTS, and Whisper at once
- Written for Japanese first; English works but is less polished
- Closing the window with the X button doesn't stop everything. Use `stop_lily.bat` to terminate all of her processes.

## License

See `LICENSE`. Third-party tools and models (Ollama models, GPT-SoVITS,
Whisper, VRM models, voice corpora) keep their own licenses.


## Animations

The emotion animations (angry, curious, excited, happy, sad, surprised) are not included. Put your own motion files named like `happy_mocap.json` in `vrm-viewer/public/animations/`. The avatar still works without them, but those emotion animations won't play.

## Credits

Lily's voice is a GPT-SoVITS model trained on the
[つくよみちゃんコーパス](https://tyc.rei-yumesaki.net/material/corpus/)
(Tsukuyomi-chan Corpus, CV.夢前黎). The corpus and its audio are **not**
included in this repo. If you use the corpus yourself, follow its
[terms of use](https://tyc.rei-yumesaki.net/material/corpus/) and credit it as
「つくよみちゃんコーパス（CV.夢前黎）」.

Lily's avatar is an original model made by the author in
[VRoid Studio](https://vroid.com/en/studio). It is not included in this repo.

This project is not affiliated with or endorsed by the Tsukuyomi-chan project.

"""
music_player.py — background YouTube music playback for Lily.

Search YouTube, play audio in the background, and loop the current track
until told otherwise. Runs on its own thread so it never blocks the main
asyncio event loop — Lily can keep talking, reacting to the screen, etc.
while a song loops.

SETUP (once, on the machine running Lily):
    1. Install VLC (the actual app): https://www.videolan.org/vlc/
    2. pip install yt-dlp python-vlc
    3. Install a JS runtime for yt-dlp (e.g. Deno): https://docs.deno.com/runtime/getting_started/installation/

USAGE from the main script:
    from music_player import lily_music, detect_music_command

    music_cmd = detect_music_command(user_input)
    if music_cmd:
        ... see MUSIC_INTEGRATION.md for the full process_response branch
"""

import asyncio
import re
import threading
import time
from typing import Any, Optional

try:
    import vlc
except ImportError:
    vlc = None

try:
    import yt_dlp  # type: ignore[import]
except (ImportError, ModuleNotFoundError):
    yt_dlp = None

# ── add to music_player.py ──

import json
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
# reuse whatever model name your main script already uses (qwen2.5:7b per your log)
CLASSIFIER_MODEL = "qwen2.5:7b"
_QUIET_CLASSIFY_PROMPT = """You are classifying a short voice-transcribed message from a user \
talking to an AI character while music is playing and she's staying quiet until told the song is over.

The transcription may be garbled or noisy (it comes from speech-to-text).

Classify the message into exactly one of these intents:
- "done": user is saying the song is over / she can talk again
- "stop": user wants the music stopped
- "loop_on": user wants the current song to repeat/loop
- "loop_off": user wants looping turned off
- "play": user wants a DIFFERENT SONG played (extract the song/artist as "query"). Only use this if they clearly name a song, artist, or genre to play -- not a topic, news item, or question.
- "question": user is asking her a real question she should answer even though music is playing, INCLUDING requests to search, look up, or find information about any real-world topic, news, or event.
- "other": anything else (comments, corrections, noise, unrelated chatter) — she should stay silent

IMPORTANT: requests to "search", "look up", "find out about", or ask about any real-world topic/news/event are always "question", never "play" — "play" is reserved strictly for requests to play a specific song, artist, or genre of music.

Respond with ONLY minified JSON, no other text: {"intent": "<intent>", "query": "<song query or null>"}

Message: "{text}"
"""

async def llm_classify_quiet_intent(text: str) -> dict:
    """LLM-based classification for input received during music-quiet mode.
    Falls back to {"intent": "other", "query": None} (i.e. stay silent) on
    any failure, so a broken/slow LLM call never crashes the flow — it just
    behaves like today's default."""

    def _call():
        prompt = _QUIET_CLASSIFY_PROMPT.replace("{text}", text)
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": CLASSIFIER_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "{}")
        return json.loads(raw)

    try:
        result = await asyncio.to_thread(_call)
        intent = result.get("intent")
        if intent not in {"done", "stop", "loop_on", "loop_off", "play", "question", "other"}:
            return {"intent": "other", "query": None}
        return {"intent": intent, "query": result.get("query")}
    except Exception as e:
        print(f"[Music-quiet classify] error, defaulting to 'other': {e}")
        return {"intent": "other", "query": None}



# ============================================================
# DETECTION
# ============================================================
_MUSIC_PLAY_RE = re.compile(
    r"\b(?:play|put on|throw on|queue up)\b\s+(?:the\s+song\s+)?(.+)$",
    re.IGNORECASE,
)
_MUSIC_PLAY_JP_RE = re.compile(r"^(.+?)(?:を|の曲)?(?:かけて|流して|再生して)[。！]?$")

_MUSIC_STOP_TRIGGERS = [
    "stop the music", "stop music", "turn off the music", "stop playing",
    "kill the music", "pause the music",
    "音楽止めて", "曲止めて", "音楽やめて", "曲やめて", "音楽ストップ",
]
_MUSIC_LOOP_ON_TRIGGERS = [
    "loop it", "loop this", "put it on repeat", "repeat this song",
    "keep looping", "loop this song", "on repeat",
    "リピートして", "ループして", "繰り返して", "リピートにして",
]
_MUSIC_LOOP_OFF_TRIGGERS = [
    "stop looping", "don't loop", "turn off loop", "no repeat",
    "just play once", "don't repeat",
    "ループやめて", "リピートやめて", "繰り返さないで", "ループ切って",
]


def music_available() -> bool:
    """Call this from the main script before touching lily_music --
    it's None if VLC failed to initialize at import time."""
    return lily_music is not None

_MUSIC_DONE_TRIGGERS = [
    "song's done", "song is done", "it's done", "it's over", "song's over",
    "you can talk now", "talk now", "go ahead and talk", "you can speak now",
    "okay talk", "ok talk",
    "曲終わった", "曲終わったよ", "終わったよ", "もう話していいよ",
    "喋っていいよ", "話していいよ", "もういいよ",
]

def detect_music_command(text: str) -> Optional[dict]:
    """Returns one of:
        {"action": "play", "query": "<song search text>"}
        {"action": "stop", "query": None}
        {"action": "loop_on", "query": None}
        {"action": "loop_off", "query": None}
    or None if the text isn't a music command at all.

    Checked in this order: stop / loop toggles first (so "stop the music"
    doesn't get misparsed as "play ... music"), then a play request.
    """
    tl = text.strip()
    tll = tl.lower()

    if any(t in tll for t in _MUSIC_STOP_TRIGGERS):
        return {"action": "stop", "query": None}
    if any(t in tll for t in _MUSIC_LOOP_OFF_TRIGGERS):
        return {"action": "loop_off", "query": None}
    if any(t in tll for t in _MUSIC_LOOP_ON_TRIGGERS):
        return {"action": "loop_on", "query": None}

    m = _MUSIC_PLAY_RE.search(tl)
    if m and m.group(1).strip():
        return {"action": "play", "query": m.group(1).strip()}

    m_jp = _MUSIC_PLAY_JP_RE.match(tl)
    if m_jp and m_jp.group(1).strip():
        return {"action": "play", "query": m_jp.group(1).strip()}

    return None

_HYPE_KEYWORDS = [
    "remix", "edm", "dance", "party", "rock", "metal", "hardstyle", "trap",
    "phonk", "dubstep", "hype", "banger", "energetic", "upbeat", "workout",
    "hard rock", "punk", "drum and bass", "dnb",
]
_SAD_KEYWORDS = [
    "ballad", "sad", "cry", "heartbreak", "slow", "acoustic", "lullaby",
    "piano", "melancholy", "breakup", "emotional", "tearjerker", "sorrow",
]
_CHILL_KEYWORDS = [
    "lofi", "lo-fi", "chill", "relax", "ambient", "study", "background",
    "smooth jazz", "soft",
]

_MUSIC_DONE_TRIGGERS = [
    "song's done", "song is done", "it's done", "it's over", "song's over",
    "you can talk now", "talk now", "go ahead and talk", "you can speak now",
    "okay talk", "ok talk",
    "曲終わった", "曲終わったよ", "終わったよ", "もう話していいよ",
    "喋っていいよ", "話していいよ", "もういいよ",
]



def detect_music_done(text: str) -> bool:
    """True if the user is telling Lily the song has finished and she
    can go back to talking normally."""
    tll = text.strip().lower()
    return any(t in tll for t in _MUSIC_DONE_TRIGGERS)

# ============================================================
# QUIET-MODE ROUTER
# ============================================================
async def handle_quiet_mode_input(text: str) -> dict:
    """Call this from the main script for every user utterance WHILE
    music is playing and Lily is in quiet mode. Decides what to do
    without her speaking unless the user actually asked something.

    Returns one of:
      {"action": "silent"}                          -- ignore, don't respond
      {"action": "resume_talking"}                   -- song's over, go back to normal mode
      {"action": "stop_music"}
      {"action": "loop_on"} / {"action": "loop_off"}
      {"action": "change_song", "query": "..."}
      {"action": "answer", "query": "<original text>"}  -- she SHOULD speak
    """
    # Fast path: plain rule-based triggers first, no LLM round-trip needed
    if detect_music_done(text):
        return {"action": "resume_talking"}

    cmd = detect_music_command(text)
    if cmd:
        # stop / loop_on / loop_off / play all map 1:1 to the router's actions
        if cmd["action"] == "play":
            return {"action": "change_song", "query": cmd["query"]}
        return {"action": cmd["action"] + ("_music" if cmd["action"] == "stop" else "")}

    # Ambiguous / free-form speech -- let the LLM decide whether this
    # was a real question aimed at her, or just noise/comments she
    # should silently ignore while the song plays.
    result = await llm_classify_quiet_intent(text)
    intent = result.get("intent")

    if intent == "done":
        return {"action": "resume_talking"}
    if intent == "stop":
        return {"action": "stop_music"}
    if intent == "loop_on":
        return {"action": "loop_on"}
    if intent == "loop_off":
        return {"action": "loop_off"}
    if intent == "play":
        return {"action": "change_song", "query": result.get("query") or text}
    if intent == "question":
        return {"action": "answer", "query": text}

    # "other" -- comments, noise, corrections: stay quiet
    return {"action": "silent"}

def _classify_mood(title: str, tags: list, categories: list, duration: Optional[int]) -> dict:
    """Heuristic mood/energy from yt-dlp metadata. Returns
    {"mood": "hype"|"sad"|"chill", "energy": 0.0-1.0}.
    Tags/categories are much more reliable than parsing the raw title
    alone, since a lot of songs don't spell out their genre in the name."""
    text = " ".join([title or ""] + (tags or []) + (categories or [])).lower()

    hype_score = sum(1 for w in _HYPE_KEYWORDS if w in text)
    sad_score = sum(1 for w in _SAD_KEYWORDS if w in text)
    chill_score = sum(1 for w in _CHILL_KEYWORDS if w in text)

    if hype_score > 0 and hype_score >= sad_score:
        return {"mood": "hype", "energy": min(1.0, 0.65 + hype_score * 0.08)}
    if sad_score > 0:
        return {"mood": "sad", "energy": max(0.15, 0.35 - sad_score * 0.04)}
    if chill_score > 0:
        return {"mood": "chill", "energy": 0.45}

    # No tag signal at all -- fall back to a mild default rather than
    # pretending we know. Slightly favors "chill" since most untagged
    # pop/general music sits closer to mid-energy than either extreme.
    return {"mood": "chill", "energy": 0.55}


# ============================================================
# PLAYER
# ============================================================
class LilyMusicPlayer:
    def __init__(self, output_device_name: Optional[str] = None):
        """
        output_device_name: substring to match against VLC's audio output
        device list (e.g. "CABLE Input") if you want music routed to the
        same virtual cable as her TTS voice, so it's audible on stream
        alongside her voice. Leave None to use the system default output.
        """
        if vlc is None:
            raise RuntimeError(
                "VLC is not installed. Install it with: pip install python-vlc"
            )
        # vlc.Instance() may return None in some type-checkers or broken installs;
        # guard against that so we raise a clear error here instead of later
        self.vlc_instance = vlc.Instance()
        if not self.vlc_instance:
            raise RuntimeError("VLC failed to create an Instance; check your VLC installation and python-vlc installation")
        # media_player_new() should exist on a valid Instance
        try:
            self.player = self.vlc_instance.media_player_new()
        except Exception as e:
            raise RuntimeError(f"Failed to create VLC media player: {e}")
        self._maybe_set_output_device(output_device_name)

        self.loop_enabled = True
        self.current_title: Optional[str] = None
        self.current_query: Optional[str] = None
        self.current_mood: dict = {"mood": "none", "energy": 0.0}
        self._watch_thread: Optional[threading.Thread] = None
        self._stop_watch = threading.Event()
        self._lock = threading.Lock()

    def list_output_devices(self) -> None:
        """Diagnostic helper -- prints every audio output device VLC can
        see. Call once at startup so you can check whether the system
        default it picked is actually the device you're listening on."""
        try:
            dev = self.player.audio_output_device_enum()
            if not dev:
                print("[Music][AudioDevice] audio_output_device_enum() returned nothing "
                      "(normal for some VLC/output-module combos, not necessarily broken).")
                return
            d = dev
            while d:
                dev_id = d.contents.device
                dev_desc = d.contents.description
                print(f"[Music][AudioDevice] id={dev_id!r} desc={dev_desc!r}")
                d = d.contents.next
        except Exception as e:
            print(f"[Music][AudioDevice] enum failed: {e}")

    def _maybe_set_output_device(self, name_substr: Optional[str]):
        if not name_substr:
            return
        try:
            dev = self.player.audio_output_device_enum()
            while dev:
                dev_id = dev.contents.device
                dev_desc = dev.contents.description
                if dev_id and name_substr.lower() in str(dev_desc).lower():
                    self.player.audio_output_device_set(None, dev_id)
                    print(f"[Music] Routed output to device matching {name_substr!r}")
                    break
                dev = dev.contents.next
        except Exception as e:
            print(f"[Music] Could not set output device ({e}); using default.")

    def _search(self, query: str):
        if yt_dlp is None:
            raise RuntimeError(
                "yt_dlp is not installed. Install it with: pip install yt-dlp"
            )
        ydl_opts: dict[str, Any] = {
            # Prefer a plain progressive HTTP audio stream (m4a/webm) over
            # HLS/DASH manifests -- VLC's bundled build can be flaky with
            # YouTube's .m3u8 HLS streams, and a direct audio-only URL is
            # the most reliable thing to hand it.
            "format": "bestaudio[protocol^=http][ext=m4a]/bestaudio[protocol^=http]/bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "default_search": "ytsearch1",
            "skip_download": True,
            "extractor_args": {
                "youtube": {"player_client": ["android", "web"]}
            },
        }
        with yt_dlp.YoutubeDL(params=ydl_opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(query, download=False)
            if isinstance(info, dict) and "entries" in info:
                entries = info["entries"]
                if not entries:
                    raise RuntimeError(f"No results found for query: {query}")
                info = next(iter(entries))

            if not isinstance(info, dict):
                raise RuntimeError(f"Could not resolve video info for query: {query}")

            title = info.get("title")
            stream_url = info.get("url")
            protocol = info.get("protocol")
            # yt-dlp resolves the exact HTTP headers (User-Agent, Referer,
            # etc) googlevideo.com needs to actually serve the stream --
            # VLC's media_new(url) alone sends none of these, which is
            # what causes "EncounteredError" with a URL that looks fine
            # and even works in a browser/curl-with-headers.
            http_headers = info.get("http_headers") or {}
            if not title or not stream_url:
                raise RuntimeError(f"Could not resolve video info for query: {query}")

            print(f"[Music] resolved title={title!r} protocol={protocol!r} "
                  f"format_id={info.get('format_id')!r} acodec={info.get('acodec')!r} "
                  f"headers={list(http_headers.keys())!r}")

            mood = _classify_mood(
                title,
                info.get("tags") or [],
                info.get("categories") or [],
                info.get("duration"),
            )
            return title, stream_url, mood, http_headers

    async def play(self, query: str) -> Optional[str]:
        """Search for and play a song, replacing whatever's currently
        playing. Returns the resolved video title. Raises on failure so
        the caller can give an honest response instead of pretending it
        worked."""
        title, stream_url, mood, http_headers = await asyncio.to_thread(self._search, query)

        with self._lock:
            self._stop_watch.set()
        if self._watch_thread:
            self._watch_thread.join(timeout=1)

        # mypy/type-checkers may think vlc_instance is Optional; ensure it's not
        assert self.vlc_instance is not None
        media = self.vlc_instance.media_new(stream_url)

        # Feed VLC the same User-Agent/Referer yt-dlp used to resolve this
        # URL. googlevideo.com stream URLs are tied to the request context
        # they were issued for -- without matching headers, VLC's raw
        # request gets rejected server-side (this is what a bare
        # "EncounteredError" with no other detail almost always means for
        # YouTube URLs).
        user_agent = http_headers.get("User-Agent")
        referer = http_headers.get("Referer") or "https://www.youtube.com/"
        if user_agent:
            media.add_option(f":http-user-agent={user_agent}")
        media.add_option(f":http-referrer={referer}")

        self.player.set_media(media)

        try:
            assert vlc is not None
            events = self.player.event_manager()
            # Detach any listeners from a previous play() call first, so
            # errors/state changes don't get printed multiple times by
            # stacked handlers.
            try:
                events.event_detach(getattr(vlc.EventType, "MediaPlayerEncounteredError"))
            except Exception:
                pass
            try:
                events.event_detach(getattr(vlc.EventType, "MediaPlayerPlaying"))
            except Exception:
                pass
            try:
                events.event_detach(getattr(vlc.EventType, "MediaPlayerEndReached"))
            except Exception:
                pass

            events.event_attach(
                getattr(vlc.EventType, "MediaPlayerEncounteredError"),
                lambda e: print(f"[Music] ⚠️ VLC playback error for {title!r} "
                                f"(stream may be broken/throttled/unsupported)")
            )
            events.event_attach(
                getattr(vlc.EventType, "MediaPlayerPlaying"),
                lambda e: print(f"[Music] ▶️ VLC reports PLAYING for {title!r}")
            )
            events.event_attach(
                getattr(vlc.EventType, "MediaPlayerEndReached"),
                lambda e: print(f"[Music] ⏹️ VLC reports END REACHED for {title!r} "
                                f"(if this fires almost immediately, the stream likely "
                                f"failed to actually load)")
            )
        except Exception as e:
            print(f"[Music] could not attach VLC event listeners: {e}")

        # Force volume up in case it's inherited a 0/muted state from a
        # previous session or VLC's own default.
        try:
            self.player.audio_set_volume(100)
        except Exception as e:
            print(f"[Music] could not set volume: {e}")

        result = self.player.play()
        print(f"[Music] player.play() returned {result} | stream_url={stream_url[:90]!r}")

        # Give VLC a moment to actually start, then report its state --
        # the single most useful line for telling "no sound" apart from
        # "never started" or "started and errored".
        await asyncio.sleep(1.5)
        try:
            state = self.player.get_state()
            vol = self.player.audio_get_volume()
            print(f"[Music] 1.5s after play(): state={state} volume={vol} "
                  f"is_playing={self.player.is_playing()}")
        except Exception as e:
            print(f"[Music] state check failed: {e}")

        self.current_title = title
        self.current_query = query
        self.current_mood = mood

        self._stop_watch = threading.Event()
        self._watch_thread = threading.Thread(target=self._watch_for_end, daemon=True)
        self._watch_thread.start()
        return title

    def get_mood(self) -> dict:
        return self.current_mood

    def _watch_for_end(self):
        """Background thread: when the track ends, restart it if looping
        is on. This is what makes the loop survive across the whole
        conversation without blocking Lily's main event loop."""
        time.sleep(2)  # give VLC a moment to actually start
        while not self._stop_watch.is_set():
            state = self.player.get_state()
            if vlc is not None and state == getattr(vlc.State, "Ended", None):
                if self.loop_enabled:
                    self.player.stop()
                    self.player.play()
                else:
                    # Song ended naturally and we're not looping -- clear
                    # playback state so anything checking get_mood() /
                    # is_playing() knows there's no music anymore and
                    # stops the dancing/head-bob animation.
                    self.current_title = None
                    self.current_query = None
                    self.current_mood = {"mood": "none", "energy": 0.0}
                    break
            time.sleep(1)
            
    def set_loop(self, enabled: bool):
        self.loop_enabled = enabled

    def is_playing(self) -> bool:
        try:
            return self.player.is_playing() == 1
        except Exception:
            return False

    def stop(self):
        self._stop_watch.set()
        try:
            self.player.stop()
        except Exception:
            pass
        self.current_title = None
        self.current_query = None
        self.current_mood = {"mood": "none", "energy": 0.0}


def _make_player() -> Optional["LilyMusicPlayer"]:
    """Instantiating VLC happens at import time (this module gets
    imported right at the top of the main script, before anything else
    boots). If VLC isn't installed or its DLLs aren't on PATH, this
    would otherwise crash the entire bot on startup with a traceback
    that looks unrelated to music. Fail soft instead: log it and let
    the music branch report "couldn't play" at runtime."""
    try:
        player = LilyMusicPlayer(output_device_name=None)
        print("[Music] Available audio output devices:")
        player.list_output_devices()
        return player
    except Exception as e:
        print(f"[Music] ⚠️ VLC failed to initialize ({e}). "
              f"Music playback will be unavailable. "
              f"Check that VLC is installed: https://www.videolan.org/vlc/")
        return None


# Single shared instance — import this from the main script.
# Pass output_device_name="CABLE Input" if you want music on the same
# virtual cable your TTS voice uses (audible on stream).
# match a substring from the device list above, e.g. "Realtek" or "EarPods"
lily_music = _make_player()
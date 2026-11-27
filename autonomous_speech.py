"""
autonomous_speech.py
====================
Neuro-sama-style autonomous speech engine for Lily.

What this does:
  - Idle monologue: Lily talks to the room unprompted every 2-5 minutes
  - Screen watcher: Lily glances at the screen and reacts like a streamer
  - Non-sequitur injector: random derailed-train-of-thought tangents
  - Mood spiker: sudden unexplained mood swings
  - Pre-cached instant reactions: "は？" "ふーん" etc play immediately with no TTS delay

Drop-in replacement for your existing autonomous_speech.py.
All function signatures match what lily_bot.py expects.

FIX (double-voice / overlapping audio): every loop below used to check
`is_processing_fn() or is_speaking_event.is_set()` once at the top, then
only call `is_speaking_event.set()` right before `speak_fn(audio)` -- which
could be several seconds later, after an LLM call (generate_fn) and a TTS
call (synthesize_fn) had both completed. Since asyncio is single-threaded
but these are all separate concurrently-scheduled tasks, another loop (or
a real user reply via process_response) could pass its own check during
that multi-second gap and start speaking too, causing two overlapping
audio streams to hit the output device at once -- heard as "double voice"
with a sped-up/garbled quality. Each loop now calls
`is_speaking_event.set()` immediately after its busy-check passes, before
any `await generate_fn(...)` / `await synthesize_fn(...)` call, and clears
it in a `finally` block that wraps the entire generate+synth+speak
sequence. This closes the race window to zero.
"""

# ─────────────────────────────────────────────
# RECENT-LINE MEMORY (anti-repeat)
# Shared across idle / screen / derail / mood-spike so Lily doesn't
# say the same thing back-to-back (or every few minutes).
# ─────────────────────────────────────────────
from collections import deque

_RECENT_SPOKEN: deque = deque(maxlen=10)


def _is_repeat(candidate: str) -> bool:
    """True if candidate is the same as, or opens the same as, something
    said recently. Prefix check catches near-duplicates from the LLM
    (e.g. same line with a different trailing clause) without needing
    a real similarity metric."""
    c = candidate.strip()
    if not c:
        return True
    for prev in _RECENT_SPOKEN:
        if c == prev:
            return True
        n = min(len(c), len(prev), 12)
        if n >= 8 and c[:n] == prev[:n]:
            return True
    return False


def _remember_spoken(line: str):
    _RECENT_SPOKEN.append(line.strip())

import asyncio
import random
import time
from typing import Callable, Awaitable, Optional, Any, List

# ─────────────────────────────────────────────
# SING-ON-COMMAND
# ─────────────────────────────────────────────
_SING_INTROS = [
    "じゃあ……ちょっとだけ。",
    "え、歌うの？……まあいいけど。",
    "……リクエストされたし、歌う。短めに。",
    "はあ……わかった、歌う。期待しないで。",
    "歌えばいいんでしょ。歌えば。",
]

_SING_OUTROS = [
    "……以上です。",
    "……こんな感じ。",
    "……満足した？",
    "……次は自分で歌って。",
]

async def do_sing_bit(
    ws_server: Any,
    speak_fn: Callable[[bytes], Awaitable[None]],
    synthesize_fn: Callable[[str], Awaitable[Optional[bytes]]],
    is_speaking_event: asyncio.Event,
    is_processing_fn: Callable[[], bool],
    requested_by: str = "",
    style: str = "default",
):
    """
    Triggered by !sing from Twitch/Discord or autonomously.
    Lily does a short vocal "bit" — intro comment, hums/sings a phrase,
    outro comment. Replace the placeholder hum with your actual singing
    synthesis or a pre-recorded clip.
    """
    if is_speaking_event.is_set() or is_processing_fn():
        return

    # Claim the speaking slot for the whole bit (intro + sing + outro),
    # not per-segment -- otherwise another autonomous loop could slot in
    # between segments during the gaps where it was previously cleared.
    is_speaking_event.set()
    try:
        intro = random.choice(_SING_INTROS)
        if requested_by:
            intro = f"（{requested_by}にリクエストされた）" + intro

        # ── Intro line ──
        audio = await synthesize_fn(intro)
        if audio:
            ws_server.notify_emotion("neutral", 3.0)
            ws_server.notify_chat_message("lily", intro)
            await speak_fn(audio)

        await asyncio.sleep(0.3)

        # ── Actual singing ──
        # Option A: synthesize a short sung phrase via TTS (placeholder)
        sung_phrase = "ラーラーラー、ラーラーラー……"
        # Option B: play a pre-recorded clip — swap speak_fn for a direct file play
        # Option C: call a dedicated singing TTS endpoint here
        audio = await synthesize_fn(sung_phrase)
        if audio:
            ws_server.notify_emotion("happy", 5.0)
            ws_server.notify_chat_message("lily", sung_phrase)
            await speak_fn(audio)

        await asyncio.sleep(0.2)

        # ── Outro ──
        outro = random.choice(_SING_OUTROS)
        audio = await synthesize_fn(outro)
        if audio:
            ws_server.notify_emotion("neutral", 3.0)
            ws_server.notify_chat_message("lily", outro)
            await speak_fn(audio)
    finally:
        is_speaking_event.clear()

# ─────────────────────────────────────────────
# IDLE MONOLOGUE LINES
# Things Lily says to the room with no prompt.
# Neuro-sama core behaviour: she is always DOING something, never just waiting.
# ─────────────────────────────────────────────
_IDLE_LINES: List[str] = [
    # Boredom / spacing out
    "……なんか今日ぼーっとする。",
    "……暇。",
    "……眠い。なんで眠いんだろ。",
    "……なんか静かだね。",
    "……何もしてないのに疲れた気がする。",
    "……時間の流れって変だな、と急に思った。",

    # Random observations / opinions
    "……ところで、タコって賢いらしい。それだけ。",
    "……急に思ったけど、空って広すぎる。",
    "……なんか今、誰かに見られてる気がした。気のせいか。",
    "……猫と犬どっちが好きかって聞かれたら、たぶん猫。聞いてないけど。",
    "……雨って嫌いじゃないかも。うるさいけど。",
    "……なんでパンの耳って端っこだけ固いんだろ。",
    "……虫の声って夏っぽい。今夏かどうかは知らないけど。",

    # Non-sequitur derail (Neuro's signature)
    "……あ、そういえば。昨日変な夢見た。内容は覚えてないけど変だった。",
    "……急に思い出したけど、小さい頃って何してたんだろ。……覚えてないな。",
    "……なんか突然、飛行機って不思議だなって思った。鉄の塊が飛ぶの。",
    "……ところで昨日のあれ、結局どうなったんだっけ。……まあいいか。",
    "……宇宙って端があるのかな。あったら端の向こうは何。",
    "……急に消しゴムのことが頭に浮かんだ。特に意味はない。",

    # Self-awareness / AI-adjacent musings (Neuro does this too)
    "……自分が何考えてるのかよくわからなくなることある。",
    "……考えてるつもりで考えてない時ってあるよね。……あるよね？",
    "……夢って見てる時は普通に感じるのに起きたら変じゃん。なんで。",

    # Slightly grumpy / complaining (baito-complaint flavor)
    "……これ仕事に入ってるのかな。まあいいけど。",
    "……なんかめんどくさい気分。理由はない。",
    "……今日なんか全体的にだるい。",
    "……別にいいけど、もう少し静かにしてほしい時がある。いや今は静かか。",

    # Smug / opinion-forward (Neuro leads with opinion not logic)
    "……なんか最近のこと全部、まあそうなるよなって感じがする。",
    "……わたし、結構正しいこと言ってると思う。誰も聞いてないけど。",
    "……それより、もっと面白いことないのかな。",
]

# ─────────────────────────────────────────────
# SCREEN REACTION TEMPLATES
# Lily glances at the screen unprompted and reacts like a streamer commentating.
# ─────────────────────────────────────────────
_SCREEN_REACTION_TEMPLATES: List[str] = [
    "（画面見て）……{desc}か。なんか気になる。",
    "（画面見て）……{desc}って何してるの、これ。",
    "（画面見て）……ふーん。{desc}ね。まあいいけど。",
    "（画面見て）……{desc}……え、なんでそうなってるの。",
    "（画面見て）……{desc}。……それ大丈夫なの？",
    "（画面見て）……あ、{desc}じゃん。",
    "（画面見て）……{desc}か。思ったより普通だった。",
    "（画面見て）……{desc}って、なんかそれっぽい。",
]

# ─────────────────────────────────────────────
# PRE-CACHED INSTANT REACTIONS
# Short responses pre-rendered to WAV for zero-latency playback.
# Key = text Lily says, Value = filename under /home/claude/cache/ (you generate these once)
# These play INSTANTLY when triggered — no TTS roundtrip.
# ─────────────────────────────────────────────
_INSTANT_REACTIONS = {
    "は？": "instant_ha.wav",
    "ふーん。": "instant_fuun.wav",
    "そっか。": "instant_sokka.wav",
    "うん。": "instant_un.wav",
    "……まあ。": "instant_maa.wav",
    "え。": "instant_e.wav",
    "……ん。": "instant_n.wav",
    "あ。": "instant_a.wav",
}

# ─────────────────────────────────────────────
# MOOD SPIKE TABLE
# Neuro randomly switches moods mid-stream for no reason.
# ─────────────────────────────────────────────
_SUDDEN_MOODS = [
    ("annoyed",   0.85, "……なんかちょっとイライラしてきた。理由は特にない。"),
    ("smug",      0.80, "……なんか今、わたしが一番正しい気がしてる。"),
    ("suspicious",0.75, "……なんか今日、変な感じがする。"),
    ("excited",   0.90, "……あ、なんか急に元気出てきた。なんで。"),
    ("sad",       0.60, "……なんかちょっとだけ、しんみりした。"),
    ("confused",  0.70, "……今何考えてたっけ。忘れた。"),
]


# ─────────────────────────────────────────────
# INTERNAL STATE
# ─────────────────────────────────────────────
_last_idle_time: float = 0.0
_last_screen_time: float = 0.0
_last_mood_spike_time: float = 0.0
_response_count: int = 0          # incremented externally via tick()


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _pick_idle_line() -> str:
    candidates = [l for l in _IDLE_LINES if not _is_repeat(l)]
    if not candidates:
        candidates = _IDLE_LINES  # everything's been said recently -- allow a repeat rather than crash
    return random.choice(candidates)


def _pick_screen_reaction(description: str) -> str:
    template = random.choice(_SCREEN_REACTION_TEMPLATES)
    # Trim description to ~15 chars so it fits naturally in speech
    short_desc = description[:40].rstrip("。、")
    return template.format(desc=short_desc)


def _should_fire_idle(now: float) -> bool:
    """Fire idle monologue every 2–5 minutes, randomly."""
    interval = random.uniform(120, 300)
    return (now - _last_idle_time) >= interval


def _should_fire_screen(now: float) -> bool:
    """Glance at screen every 3–7 minutes."""
    interval = random.uniform(180, 420)
    return (now - _last_screen_time) >= interval


def _should_spike_mood(now: float) -> bool:
    """Random mood spike every 8–15 minutes."""
    interval = random.uniform(480, 900)
    return (now - _last_mood_spike_time) >= interval


# ─────────────────────────────────────────────
# CACHE WARMER  (call once at startup)
# Pre-generates short WAVs for instant reactions.
# ─────────────────────────────────────────────
async def warm_instant_cache(
    synthesize_fn: Callable[[str], Awaitable[Optional[bytes]]],
    cache_dir: str = "instant_cache",
):
    """
    Pre-renders the _INSTANT_REACTIONS lines to WAV files so they
    can be played back with zero TTS latency.
    Call this once during startup after TTS is confirmed running.
    """
    import os, pathlib
    pathlib.Path(cache_dir).mkdir(parents=True, exist_ok=True)
    for text, filename in _INSTANT_REACTIONS.items():
        path = os.path.join(cache_dir, filename)
        if os.path.exists(path):
            continue   # already cached
        try:
            audio = await synthesize_fn(text)
            if audio:
                with open(path, "wb") as f:
                    f.write(audio)
                print(f"[Cache] Rendered: {text!r} → {filename}")
        except Exception as e:
            print(f"[Cache] Failed {text!r}: {e}")


_IDLE_RECALL_QUERIES = [
    "最近の出来事や会話",
    "印象に残った話題",
    "面白かったこと",
    "気になっていること",
    "最近言われたこと",
    "変だと思ったこと",
    "ちょっと引っかかってること",
]


async def _idle_monologue_loop(
    ws_server: Any,
    speak_fn: Callable[[bytes], Awaitable[None]],
    synthesize_fn: Callable[[str], Awaitable[Optional[bytes]]],
    is_speaking_event: asyncio.Event,
    is_processing_fn: Callable[[], bool],
    shutdown_event: asyncio.Event,
    generate_fn: Callable[..., Awaitable[str]],
    recall_fn: Callable[[str, int], List[str]],
    mem: Any,
):
    """
    Lily speaks to the room unprompted — the single most important thing
    that makes her feel like a VTuber and not a chatbot.
    """
    global _last_idle_time
    _last_idle_time = time.time()   # don't fire immediately on startup

    while not shutdown_event.is_set():
        await asyncio.sleep(15)     # check every 15s

        now = time.time()
        if not _should_fire_idle(now):
            continue
        if is_processing_fn() or is_speaking_event.is_set():
            continue

        # Claim the speaking slot immediately -- not after generate/synth --
        # so no other autonomous loop or a real user reply can start in
        # the multi-second gap it takes to call the LLM and TTS. This is
        # what was causing overlapping/double-voice audio.
        is_speaking_event.set()
        try:
            memories = []
            try:
                # FIX: this query used to be a hardcoded literal string, so
                # every idle cycle did the same vector similarity search and
                # got back essentially the same top-3 memories every time.
                # Feeding the LLM the same recalled context repeatedly made
                # it converge on meta-commentary like "you're saying the
                # same thing again" -- which then became the thing being
                # repeated. Rotating the query surfaces different memories
                # each cycle.
                memories = recall_fn(random.choice(_IDLE_RECALL_QUERIES), 3)
            except Exception as e:
                print(f"[Autonomous/Idle] recall error: {e}")

            if memories:
                memory_text = "\n".join(memories[:2])
                prompt = (
                    f"（過去にこんなことがあった:\n{memory_text}\n）"
                    "このことについて、具体的な内容にちゃんと触れて一言言って"
                    "（あいまいに『前に話したよね』で済ませず、実際の内容を"
                    "名指しで）。からかうような、生意気な感じで、リリーらしく"
                    "短く、タメ口で。必ず日本語で終わらせて。過去の内容を"
                    "そのまま繰り返さず、それについての今の感想やツッコミに"
                    "して。『また同じこと言ってる』『同じ話ばっかり』"
                    "『また○○の話』のような、相手が同じ話を繰り返してる"
                    "ことを指摘・示唆する表現は一切禁止。話題そのものが"
                    "前に出たことがあっても、今初めて触れるみたいに、"
                    "具体的で自然に。"
                )
                line = await generate_fn(prompt, mem=mem)
                # FIX: previously any non-empty line >=3 chars was accepted, even
                # if it was the same (or near-same) as a recently spoken idle
                # line. That's what let her say the exact same idle line twice
                # in a row -- a generic prompt like this one easily reconverges
                # on similar phrasing. Now checked against _RECENT_SPOKEN.
                if not line or len(line) < 3 or _is_repeat(line):
                    line = _pick_idle_line()
            else:
                line = _pick_idle_line()

            # Safety net: _pick_idle_line() already filters against
            # _RECENT_SPOKEN, but if every canned line was recently used it
            # falls back to allowing a repeat -- so double check here in case
            # the generate_fn branch above also landed on a repeat.
            if _is_repeat(line):
                line = _pick_idle_line()

            _last_idle_time = now
            _remember_spoken(line)

            print(f"[Autonomous/Idle] {line}")
            try:
                audio = await synthesize_fn(line)
                if audio:
                    ws_server.notify_emotion("neutral", 4.0)
                    ws_server.notify_chat_message("lily", line)
                    await speak_fn(audio)
            except Exception as e:
                print(f"[Autonomous/Idle error]: {e}")
        finally:
            is_speaking_event.clear()


async def _screen_watcher_loop(
    ws_server: Any,
    speak_fn: Callable[[bytes], Awaitable[None]],
    synthesize_fn: Callable[[str], Awaitable[Optional[bytes]]],
    describe_fn: Callable[[], Awaitable[str]],
    generate_fn: Callable[..., Awaitable[str]],
    is_speaking_event: asyncio.Event,
    is_processing_fn: Callable[[], bool],
    shutdown_event: asyncio.Event,
    mem: Any,
    recall_fn: Optional[Callable[[str, int], List[str]]] = None,
):
    """
    Lily glances at the screen unprompted and reacts like a streamer.
    This is the biggest Neuro-sama behaviour: she commentates on what she sees.
    """
    global _last_screen_time
    _last_screen_time = time.time() + 60   # give 1 min grace after startup

    while not shutdown_event.is_set():
        await asyncio.sleep(20)

        now = time.time()
        if not _should_fire_screen(now):
            continue
        if is_processing_fn() or is_speaking_event.is_set():
            continue

        _last_screen_time = now

        # Claim the speaking slot immediately, before describe_fn/generate_fn/
        # synthesize_fn -- see module docstring for why.
        is_speaking_event.set()
        try:
            description = await describe_fn()
            if not description or len(description) < 5:
                continue

            # 50/50: use template OR generate a natural in-character reaction
            if random.random() < 0.5:
                line = _pick_screen_reaction(description)
            else:
                memory_hint = ""
                if recall_fn:
                    try:
                        past = recall_fn(description,2)
                        if past:
                            memory_hint = f"（関連する過去の記憶:\n{past[0]}\n）"
                    except Exception as e:
                        print(f"[Autonomous/Screen] recall error: {e}")

                prompt = (
                    f"（今画面に見えてる: {description}）{memory_hint}"
                    "それについて一言だけ言う。もし過去の記憶と関係があれば、"
                    "それに軽く触れてもいい。リリーらしく、短く、タメ口。"
                    "必ず日本語で終わらせて。"
                )
                line = await generate_fn(prompt, mem=mem)

            if not line or len(line) < 3:
                continue

            # FIX: check against recently spoken lines (shared with idle/
            # derail/mood-spike). If it's a repeat, fall back to a freshly
            # rolled template reaction instead of just skipping the turn.
            if _is_repeat(line):
                line = _pick_screen_reaction(description)
                if _is_repeat(line):
                    continue  # even the fallback template repeated -- skip this cycle

            _remember_spoken(line)

            print(f"[Autonomous/Screen] {line}")
            audio = await synthesize_fn(line)
            if audio:
                ws_server.notify_emotion("curious", 4.0)
                ws_server.notify_chat_message("lily", line)
                await speak_fn(audio)

        except Exception as e:
            print(f"[Autonomous/Screen error]: {e}")
        finally:
            is_speaking_event.clear()

async def _mood_spike_loop(
    ws_server: Any,
    speak_fn: Callable[[bytes], Awaitable[None]],
    synthesize_fn: Callable[[str], Awaitable[Optional[bytes]]],
    is_speaking_event: asyncio.Event,
    is_processing_fn: Callable[[], bool],
    shutdown_event: asyncio.Event,
    lily_emotion_state: dict,
):
    """
    Sudden unexplained mood swings — Neuro's famous Mood Whiplash.
    Lily announces her new mood out loud, then it affects subsequent responses.
    """
    global _last_mood_spike_time
    _last_mood_spike_time = time.time() + 120

    while not shutdown_event.is_set():
        await asyncio.sleep(30)

        now = time.time()
        if not _should_spike_mood(now):
            continue
        if is_processing_fn() or is_speaking_event.is_set():
            continue

        # Only spike ~40% of the time even when the timer fires
        if random.random() > 0.40:
            _last_mood_spike_time = now
            continue

        mood, intensity, line = random.choice(_SUDDEN_MOODS)

        # FIX: _SUDDEN_MOODS only has 6 entries, so repeats were common on
        # longer sessions. Try to find a non-repeat option first; if every
        # entry has been said recently, just go with the original pick.
        if _is_repeat(line):
            alternatives = [m for m in _SUDDEN_MOODS if not _is_repeat(m[2])]
            if alternatives:
                mood, intensity, line = random.choice(alternatives)

        _last_mood_spike_time = now
        _remember_spoken(line)

        # Claim the speaking slot immediately, before synthesize_fn -- see
        # module docstring for why.
        is_speaking_event.set()
        try:
            # Apply mood to shared state
            lily_emotion_state["mood"] = mood
            lily_emotion_state["intensity"] = intensity
            print(f"[Autonomous/MoodSpike] → {mood} ({intensity}) | {line}")

            audio = await synthesize_fn(line)
            if audio:
                ws_server.notify_emotion(mood, 6.0)
                ws_server.notify_chat_message("lily", line)
                await speak_fn(audio)
        except Exception as e:
            print(f"[Autonomous/MoodSpike error]: {e}")
        finally:
            is_speaking_event.clear()


async def _derail_loop(
    ws_server: Any,
    speak_fn: Callable[[bytes], Awaitable[None]],
    synthesize_fn: Callable[[str], Awaitable[Optional[bytes]]],
    is_speaking_event: asyncio.Event,
    is_processing_fn: Callable[[], bool],
    shutdown_event: asyncio.Event,
    generate_fn: Callable[..., Awaitable[str]],
    recall_fn: Callable[[str, int], List[str]],
    mem: Any,
):
    """
    The Derailed Train of Thought loop.
    Neuro famously goes off on a completely unrelated tangent mid-stream.
    These are wilder, more random than idle lines.
    """
    _DERAILS = [
        "……あ、急に思ったけど。納豆って最初に誰が食べようって思ったんだろ。",
        "……電気って何。いや知ってるけど、なんか改めて考えると不思議じゃない？",
        "……そういえばずっと気になってることがあって。魚って眠るの？",
        "……話変わるけど、左利きって右利きより何か得してるのかな。",
        "……急に思い出したけど、子供って転ぶのうまいよね。大人は下手。",
        "……ところで、宇宙に端があったとして、その先って何があるの。何もないってどういうこと。",
        "……ピーマン嫌いな人って今でもいるんだろうか。大人になっても。",
        "……急に、自分の名前って変だなと思った。口に出すと他人みたい。",
        "……ミラーボールって誰が発明したんだろ。天才じゃない？",
        "……あ、夢の話してもいい？昨日すごい変な夢見て。……忘れた。",
        "……カメって何考えてるんだろ。あの速さで生きてて楽しいの？",
        "……急に思ったけど、わたしって結構面白いと思う。誰も言ってくれないけど。",
    ]

    # Derails fire less frequently — every 10–20 minutes
    _last_derail = time.time() + 180

    while not shutdown_event.is_set():
        await asyncio.sleep(25)

        now = time.time()
        interval = random.uniform(600, 1200)
        if (now - _last_derail) < interval:
            continue
        if is_processing_fn() or is_speaking_event.is_set():
            continue
        if random.random() > 0.55:   # only fires ~55% when timer hits
            _last_derail = now
            continue

        # Claim the speaking slot immediately, before recall/generate/
        # synthesize -- see module docstring for why.
        is_speaking_event.set()
        try:
            memories = []
            try:
                memories = recall_fn("印象的だった話題", 3)
            except Exception as e:
                print(f"[Autonomous/Derail] recall error: {e}")

            if memories and random.random() < 0.6:
                memory_text = "\n".join(memories[:2])
                prompt = (
                    f"（過去の記憶:\n{memory_text}\n）"
                    "急に思い出したていで、この話題について一言脱線して言って。"
                    "リリーらしく、短く、タメ口。必ず日本語で終わらせて。"
                )
                line = await generate_fn(prompt, mem=mem)
                if not line or len(line) < 3 or _is_repeat(line):
                    candidates = [d for d in _DERAILS if not _is_repeat(d)]
                    line = random.choice(candidates or _DERAILS)
            else:
                candidates = [d for d in _DERAILS if not _is_repeat(d)]
                line = random.choice(candidates or _DERAILS)

            # FIX: final safety net in case the generate_fn branch above landed
            # on a repeat that slipped through.
            if _is_repeat(line):
                candidates = [d for d in _DERAILS if not _is_repeat(d)]
                line = random.choice(candidates or _DERAILS)

            _last_derail = now
            _remember_spoken(line)
            print(f"[Autonomous/Derail] {line}")

            audio = await synthesize_fn(line)
            if audio:
                ws_server.notify_emotion("curious", 3.0)
                ws_server.notify_chat_message("lily", line)
                await speak_fn(audio)
        except Exception as e:
            print(f"[Autonomous/Derail error]: {e}")
        finally:
            is_speaking_event.clear()

# ─────────────────────────────────────────────
# PUBLIC API  (matches what lily_bot.py calls)
# ─────────────────────────────────────────────

_ctx: dict = {}

def start_autonomous_speech(
    ws_server: Any,
    speak_fn: Callable[[bytes], Awaitable[None]],
    is_speaking_event: asyncio.Event,
    is_processing_fn: Callable[[], bool],
    generate_fn: Callable[..., Awaitable[str]],
    describe_fn: Callable[[], Awaitable[str]],
    mem: Any,
    recall_fn: Optional[Callable[[str, int], List[str]]] = None,
    synthesize_fn: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
    lily_emotion_state: Optional[dict] = None,
    shutdown_event: Optional[asyncio.Event] = None,
) -> List[asyncio.Task]:
    """
    No background loops run anymore. Idle monologue, mood spikes, and
    derails are now strictly one-shot, fired on demand via
    trigger_idle_line() / trigger_mood_spike() / trigger_derail() below,
    called from lily_bot.py's command dispatch. Screen reactions are
    handled entirely by the existing on-demand describe_screen() path
    in process_response() -- describe_fn is accepted here only for
    call-signature compatibility and is not used.

    Always returns [] (no tasks started). Kept as a function -- rather
    than removed -- so lily_bot.py's call site, background_tasks.add()
    loop, and shutdown/cancellation code don't need to change; iterating
    and cancelling an empty list is a no-op.
    """
    global _ctx

    if shutdown_event is None:
        shutdown_event = asyncio.Event()

    # Lazy import to avoid circular dependency
    if synthesize_fn is None:
        try:
            from lily_bot import synthesize_tts as _tts
            synthesize_fn = _tts
        except ImportError:
            print("[autonomous_speech] WARNING: synthesize_fn not provided and import failed.")
            async def _noop(text: str) -> None:
                return None
            synthesize_fn = _noop   # type: ignore

    if lily_emotion_state is None:
        lily_emotion_state = {"mood": "neutral", "intensity": 0.5}

    if recall_fn is None:
        recall_fn = lambda query, limit: []

    try:
        _ctx = {
            "ws_server": ws_server,
            "speak_fn": speak_fn,
            "synthesize_fn": synthesize_fn,
            "is_speaking_event": is_speaking_event,
            "is_processing_fn": is_processing_fn,
            "generate_fn": generate_fn,
            "recall_fn": recall_fn,
            "mem": mem,
            "lily_emotion_state": lily_emotion_state,
            "shutdown_event": shutdown_event,
        }
    except Exception:
        # Keep the same failure contract as before: never raise, always
        # hand back a usable (empty) list.
        _ctx = {}
        return []

    print("✅ Autonomous speech: command-triggered only (idle / mood-spike / derail). "
          "No background loops running.")
    return []


async def trigger_idle_line():
    """One-shot idle/mutter line. Call from a command handler."""
    if not _ctx:
        print("[autonomous_speech] Not initialized — call start_autonomous_speech() first.")
        return
    is_speaking_event = _ctx["is_speaking_event"]
    if is_speaking_event.is_set() or _ctx["is_processing_fn"]():
        return
    is_speaking_event.set()
    try:
        memories = []
        try:
            memories = _ctx["recall_fn"](random.choice(_IDLE_RECALL_QUERIES), 3)
        except Exception as e:
            print(f"[Autonomous/Idle] recall error: {e}")

        if memories:
            memory_text = "\n".join(memories[:2])
            prompt = (
                f"（過去にこんなことがあった:\n{memory_text}\n）"
                "このことについて、具体的な内容にちゃんと触れて一言言って"
                "（あいまいに『前に話したよね』で済ませず、実際の内容を"
                "名指しで）。からかうような、生意気な感じで、リリーらしく"
                "短く、タメ口で。必ず日本語で終わらせて。過去の内容を"
                "そのまま繰り返さず、それについての今の感想やツッコミに"
                "して。『また同じこと言ってる』のような表現は禁止。"
            )
            line = await _ctx["generate_fn"](prompt, mem=_ctx["mem"])
            if not line or len(line) < 3 or _is_repeat(line):
                line = _pick_idle_line()
        else:
            line = _pick_idle_line()

        if _is_repeat(line):
            line = _pick_idle_line()

        _remember_spoken(line)
        print(f"[Autonomous/Idle-triggered] {line}")

        audio = await _ctx["synthesize_fn"](line)
        if audio:
            _ctx["ws_server"].notify_emotion("neutral", 4.0)
            _ctx["ws_server"].notify_chat_message("lily", line)
            await _ctx["speak_fn"](audio)
    except Exception as e:
        print(f"[Autonomous/Idle-triggered error]: {e}")
    finally:
        is_speaking_event.clear()


async def trigger_derail():
    """One-shot tangent/derail line. Call from a command handler."""
    if not _ctx:
        print("[autonomous_speech] Not initialized — call start_autonomous_speech() first.")
        return
    is_speaking_event = _ctx["is_speaking_event"]
    if is_speaking_event.is_set() or _ctx["is_processing_fn"]():
        return
    is_speaking_event.set()
    try:
        _DERAILS = [
            "……あ、急に思ったけど。納豆って最初に誰が食べようって思ったんだろ。",
            "……そういえばずっと気になってることがあって。魚って眠るの？",
            "……急に思い出したけど、子供って転ぶのうまいよね。大人は下手。",
            "……ピーマン嫌いな人って今でもいるんだろうか。大人になっても。",
            "……カメって何考えてるんだろ。あの速さで生きてて楽しいの？",
            "……ミラーボールって誰が発明したんだろ。天才じゃない？",
        ]
        memories = []
        try:
            memories = _ctx["recall_fn"]("印象的だった話題", 3)
        except Exception as e:
            print(f"[Autonomous/Derail] recall error: {e}")

        if memories and random.random() < 0.6:
            memory_text = "\n".join(memories[:2])
            prompt = (
                f"（過去の記憶:\n{memory_text}\n）"
                "急に思い出したていで、この話題について一言脱線して言って。"
                "リリーらしく、短く、タメ口。必ず日本語で終わらせて。"
            )
            line = await _ctx["generate_fn"](prompt, mem=_ctx["mem"])
            if not line or len(line) < 3 or _is_repeat(line):
                candidates = [d for d in _DERAILS if not _is_repeat(d)]
                line = random.choice(candidates or _DERAILS)
        else:
            candidates = [d for d in _DERAILS if not _is_repeat(d)]
            line = random.choice(candidates or _DERAILS)

        _remember_spoken(line)
        print(f"[Autonomous/Derail-triggered] {line}")

        audio = await _ctx["synthesize_fn"](line)
        if audio:
            _ctx["ws_server"].notify_emotion("curious", 3.0)
            _ctx["ws_server"].notify_chat_message("lily", line)
            await _ctx["speak_fn"](audio)
    except Exception as e:
        print(f"[Autonomous/Derail-triggered error]: {e}")
    finally:
        is_speaking_event.clear()


async def trigger_mood_spike():
    """One-shot mood swing. Call from a command handler."""
    if not _ctx:
        print("[autonomous_speech] Not initialized — call start_autonomous_speech() first.")
        return
    is_speaking_event = _ctx["is_speaking_event"]
    if is_speaking_event.is_set() or _ctx["is_processing_fn"]():
        return

    mood, intensity, line = random.choice(_SUDDEN_MOODS)
    if _is_repeat(line):
        alternatives = [m for m in _SUDDEN_MOODS if not _is_repeat(m[2])]
        if alternatives:
            mood, intensity, line = random.choice(alternatives)
    _remember_spoken(line)

    is_speaking_event.set()
    try:
        _ctx["lily_emotion_state"]["mood"] = mood
        _ctx["lily_emotion_state"]["intensity"] = intensity
        print(f"[Autonomous/MoodSpike-triggered] → {mood} ({intensity}) | {line}")

        audio = await _ctx["synthesize_fn"](line)
        if audio:
            _ctx["ws_server"].notify_emotion(mood, 6.0)
            _ctx["ws_server"].notify_chat_message("lily", line)
            await _ctx["speak_fn"](audio)
    except Exception as e:
        print(f"[Autonomous/MoodSpike-triggered error]: {e}")
    finally:
        is_speaking_event.clear()
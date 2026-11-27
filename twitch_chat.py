"""
twitch_chat.py
- Connects to Twitch chat via TwitchIO v2 (classic IRC-based chat bot)
- Passively forwards chat messages into Lily's input queue
- Handles explicit commands: !ask, !sing, !voice <style>

NOTE: This targets TwitchIO 2.x, not 3.x. 2.x chat is IRC-based, not
EventSub/WebSocket, so the constructor and event signatures are
different from the v3 style (no client_secret/bot_id/owner_id/
Component/eventsub -- just token + prefix + initial_channels).
"""
import asyncio
import os
import random
import time
from typing import Optional

from twitchio.ext import commands

TWITCH_OAUTH_TOKEN = os.getenv("TWITCH_OAUTH_TOKEN", "")  # must include the "oauth:" prefix
TWITCH_CHANNEL      = os.getenv("TWITCH_CHANNEL", "")       # channel LOGIN NAME (e.g. "yourname"), not numeric ID

PASSIVE_COMMENT_COOLDOWN = 25.0
PASSIVE_COMMENT_CHANCE   = 0.18

_last_passive_comment = 0.0


def _normalize_token(token: str) -> str:
    """TwitchIO 2.x's IRC connection expects the token with an
    'oauth:' prefix. Some OAuth flows hand back the raw token without
    it, so add it here rather than requiring the .env value to be
    exactly right."""
    token = token.strip()
    if token and not token.lower().startswith("oauth:"):
        return f"oauth:{token}"
    return token


class LilyTwitchBot(commands.Bot):
    def __init__(self, input_queue: asyncio.Queue, sing_callback=None):
        super().__init__(
            token=_normalize_token(TWITCH_OAUTH_TOKEN),
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
        )
        self.input_queue   = input_queue
        self.sing_callback = sing_callback

    async def event_ready(self) -> None:
        print(f"[Twitch] ✅ Connected | channel={TWITCH_CHANNEL}")

    async def event_message(self, message) -> None:
        global _last_passive_comment

        # message.echo is True for messages the bot itself sent
        if message.echo:
            return

        content = (message.content or "").strip()

        # Let the commands extension handle anything starting with "!"
        if content.startswith("!"):
            await self.handle_commands(message)
            return

        if not content:
            return

        author = message.author.name if message.author else "unknown"

        now = time.time()
        if (now - _last_passive_comment) < PASSIVE_COMMENT_COOLDOWN:
            return
        if random.random() > PASSIVE_COMMENT_CHANCE:
            return

        _last_passive_comment = now
        prompt = f"（Twitchチャットでこう言ってる: {author}: {content}）これに短く反応して。"
        self._enqueue(prompt)

    def _enqueue(self, text: str):
        try:
            self.input_queue.put_nowait((text, None))
        except asyncio.QueueFull:
            try:
                self.input_queue.get_nowait()
            except Exception:
                pass
            self.input_queue.put_nowait((text, None))

    @commands.command(name="ask")
    async def cmd_ask(self, ctx: commands.Context):
        parts = (ctx.message.content or "").split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            return
        author = ctx.author.name if ctx.author else "unknown"
        self._enqueue(f"（Twitchの{author}から: {parts[1].strip()}）")

    @commands.command(name="sing")
    async def cmd_sing(self, ctx: commands.Context):
        author = ctx.author.name if ctx.author else "unknown"
        if self.sing_callback:
            asyncio.create_task(self.sing_callback(requested_by=author))

    @commands.command(name="voice")
    async def cmd_voice(self, ctx: commands.Context):
        parts = (ctx.message.content or "").split(" ", 1)
        style = parts[1].strip() if len(parts) > 1 else "default"
        author = ctx.author.name if ctx.author else "unknown"
        if self.sing_callback:
            asyncio.create_task(
                self.sing_callback(requested_by=author, style=style)
            )


def start_twitch_bot(
    input_queue: asyncio.Queue, sing_callback=None
) -> Optional[asyncio.Task]:
    if not all([TWITCH_OAUTH_TOKEN, TWITCH_CHANNEL]):
        print(
            "⚠️  Twitch not configured — missing one or more of: "
            "TWITCH_OAUTH_TOKEN, TWITCH_CHANNEL (channel login name, not numeric ID)"
        )
        return None

    bot  = LilyTwitchBot(input_queue=input_queue, sing_callback=sing_callback)
    task = asyncio.create_task(bot.start())
    print("🟣 Twitch chat bot starting...")
    return task
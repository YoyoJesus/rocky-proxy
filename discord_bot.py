"""Discord bot that chats through Kent State's Rocky API."""

import asyncio
import logging
import os
import re
import time
from collections import defaultdict

import aiohttp
import discord

from secret_env import load_secret

ROCKY_BASE = os.environ.get("ROCKY_BASE", "https://rocky.cs.kent.edu/v1")
ROCKY_KEY = load_secret("ROCKY_API_KEY")
DISCORD_TOKEN = load_secret("DISCORD_BOT_TOKEN")

CHANNELS = {int(c) for c in os.environ.get("BOT_CHANNELS", "").split(",") if c.strip()}
HISTORY = int(os.environ.get("BOT_HISTORY", "20"))
MAX_BOT_TURNS = int(os.environ.get("BOT_MAX_BOT_TURNS", "5"))
COOLDOWN = float(os.environ.get("BOT_COOLDOWN", "5"))
PERSONA = os.environ.get("BOT_PERSONA", "You are friendly, helpful, and concise.")
DISCORD_LIMIT = 2000

log = logging.getLogger("rocky-bot")


class RockyError(Exception):
    def __init__(self, status: int, message: str, retry_after: float = 0):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.retry_after = retry_after


def parse_duration(value: str | None) -> float:
    """Parse Rocky's rate-limit reset header, e.g. '17s' or '1m30s'."""
    if not value:
        return 0
    units = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(n) * units[u] for n, u in re.findall(r"([\d.]+)(ms|h|m|s)", value))


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


class RockyBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.http_session: aiohttp.ClientSession
        self.model = os.environ.get("ROCKY_MODEL", "")
        self.bot_streak: defaultdict[int, int] = defaultdict(int)
        self.ready_at: defaultdict[int, float] = defaultdict(float)
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {ROCKY_KEY}"}, timeout=aiohttp.ClientTimeout(total=600)
        )
        if not self.model:
            async with self.http_session.get(f"{ROCKY_BASE}/models") as r:
                r.raise_for_status()
                self.model = (await r.json())["data"][0]["id"]

    async def close(self) -> None:
        await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s using %s", self.user, self.model)

    def is_triggered(self, message: discord.Message) -> bool:
        if isinstance(message.channel, discord.DMChannel) or message.channel.id in CHANNELS:
            return True
        if self.user in message.mentions:
            return True
        ref = message.reference.resolved if message.reference else None
        return isinstance(ref, discord.Message) and ref.author == self.user

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return
        channel_id = message.channel.id
        if not message.author.bot:
            self.bot_streak[channel_id] = 0
        if not self.is_triggered(message) or not (message.clean_content.strip() or message.attachments):
            return
        if message.author.bot and self.bot_streak[channel_id] >= MAX_BOT_TURNS:
            return
        if time.monotonic() < self.ready_at[channel_id] and message.author.bot:
            return

        async with self.locks[channel_id]:
            await asyncio.sleep(max(0, self.ready_at[channel_id] - time.monotonic()))
            try:
                async with message.channel.typing():
                    reply = await self.generate(message.channel)
            except RockyError as e:
                log.warning("Rocky error in %s: %s", channel_id, e)
                self.ready_at[channel_id] = time.monotonic() + max(COOLDOWN, e.retry_after)
                if not message.author.bot:
                    await message.reply("Rocky is busy right now, try again in a bit.", mention_author=False)
                return

            if message.author.bot:
                self.bot_streak[channel_id] += 1
            self.ready_at[channel_id] = time.monotonic() + COOLDOWN

            parts = split_message(reply) or ["..."]
            await message.reply(parts[0], mention_author=False)
            for part in parts[1:]:
                await message.channel.send(part)

    async def build_input(self, channel: discord.abc.Messageable) -> list[dict]:
        messages = []
        async for m in channel.history(limit=HISTORY):
            text = m.clean_content.strip()
            if m.attachments:
                text += " " + " ".join(f"[attachment: {a.filename}]" for a in m.attachments)
            if not text.strip():
                continue
            if m.author == self.user:
                messages.append({"role": "assistant", "content": text})
            else:
                kind = "bot" if m.author.bot else "user"
                messages.append({"role": "user", "content": f"{m.author.display_name} ({kind}): {text}"})
        messages.reverse()
        return messages

    async def generate(self, channel: discord.abc.Messageable) -> str:
        name = self.user.display_name
        instructions = (
            f"You are {name}, a participant in a Discord chat. {PERSONA} "
            "Each user message is prefixed with the author's name and whether they are a person or a bot. "
            "Reply with only your message text, without a name prefix. Keep replies short and conversational. "
            "If another bot is going in circles, wrap up the conversation politely."
        )
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": await self.build_input(channel),
            "store": False,
        }

        try:
            async with self.http_session.post(f"{ROCKY_BASE}/responses", json=body) as r:
                if r.status != 200:
                    try:
                        message = (await r.json())["error"]["message"]
                    except (aiohttp.ContentTypeError, KeyError, TypeError):
                        message = await r.text()
                    raise RockyError(r.status, message, parse_duration(r.headers.get("x-ratelimit-reset-requests")))
                text = (await r.json()).get("output_text", "").strip()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise RockyError(0, repr(e)) from e

        return re.sub(rf"^{re.escape(name)}\s*(\((bot|user)\))?\s*:\s*", "", text)


if __name__ == "__main__":
    RockyBot().run(DISCORD_TOKEN, root_logger=True)

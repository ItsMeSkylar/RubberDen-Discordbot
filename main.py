import json
import asyncio

import discord
from discord.ext import commands
from fastapi import FastAPI
import uvicorn

from scripts import DiscordScripts

# ─────────────────────────────
# Config / tokens
# ─────────────────────────────

with open("config.json") as f:
    config = json.load(f)

with open("tokens/TOKEN_DISCORD.txt") as f:
    TOKEN_DISCORD = f.read().strip()

BASE_URL = "http://localhost/api"
INTERNAL_TOKEN = "abc123"
CHANNEL_IDS: dict = config["channels"]

# ─────────────────────────────
# Discord bot
# ─────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

# ─────────────────────────────
# FastAPI app
# ─────────────────────────────

app = FastAPI()


def start_http():
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


# ─────────────────────────────
# Register Discord handlers
# ─────────────────────────────

DiscordScripts.setup(client, config, CHANNEL_IDS, BASE_URL, INTERNAL_TOKEN, start_http)

# ─────────────────────────────
# HTTP endpoints
# ─────────────────────────────


@app.post("/post-schedule")
async def postSchedule(payload: dict):
    if DiscordScripts.BOT_LOOP is None:
        return {"ok": False, "error": "bot not ready yet"}

    fut = asyncio.run_coroutine_threadsafe(
        DiscordScripts.post_payload(payload, client, CHANNEL_IDS, BASE_URL, INTERNAL_TOKEN),
        DiscordScripts.BOT_LOOP,
    )

    try:
        fut.result(timeout=60)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True}


# ─────────────────────────────
# Start bot
# ─────────────────────────────

client.run(TOKEN_DISCORD)

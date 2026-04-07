import json
import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv
from fastapi import FastAPI
import uvicorn

from scripts import DiscordScripts

# ─────────────────────────────
# Config / tokens
# ─────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

APP_ENV = os.environ.get("APP_ENV", "dev")
load_dotenv(f"env/.env.{APP_ENV}")

_missing = [v for v in ("BASE_URL", "INTERNAL_TOKEN") if not os.environ.get(v)]
if _missing:
    raise SystemExit(f"Missing required env vars: {', '.join(_missing)}")

BASE_URL = os.environ["BASE_URL"]
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]

try:
    with open("config.json") as f:
        config = json.load(f)
except FileNotFoundError:
    raise SystemExit("config.json not found")
except json.JSONDecodeError as e:
    raise SystemExit(f"config.json is invalid JSON: {e}")

try:
    with open("tokens/TOKEN_DISCORD.txt") as f:
        TOKEN_DISCORD = f.read().strip()
except FileNotFoundError:
    raise SystemExit("tokens/TOKEN_DISCORD.txt not found")

if not TOKEN_DISCORD:
    raise SystemExit("tokens/TOKEN_DISCORD.txt is empty")

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


@app.post("/notify-session-expired")
async def notifySessionExpired(payload: dict):
    if DiscordScripts.BOT_LOOP is None:
        return {"ok": False, "error": "bot not ready yet"}

    fut = asyncio.run_coroutine_threadsafe(
        DiscordScripts.post_session_expired(payload, client, CHANNEL_IDS),
        DiscordScripts.BOT_LOOP,
    )

    try:
        fut.result(timeout=10)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True}


@app.post("/notify-failure")
async def notifyFailure(payload: dict):
    if DiscordScripts.BOT_LOOP is None:
        return {"ok": False, "error": "bot not ready yet"}

    fut = asyncio.run_coroutine_threadsafe(
        DiscordScripts.post_failure(payload, client, CHANNEL_IDS),
        DiscordScripts.BOT_LOOP,
    )

    try:
        fut.result(timeout=10)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True}


# ─────────────────────────────
# Start bot
# ─────────────────────────────

client.run(TOKEN_DISCORD)

import json
import asyncio
import logging
import os
import shutil
import signal

import discord
from discord.ext import commands
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from typing import Annotated
import uvicorn

from scripts import DiscordScripts

# ─────────────────────────────
# Config / tokens
# ─────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

APP_ENV = os.environ.get("APP_ENV", "dev")
load_dotenv(f"env/.env.{APP_ENV}")

_missing = [v for v in ("BASE_URL", "INTERNAL_TOKEN", "DISCORD_TOKEN") if not os.environ.get(v)]
if _missing:
    raise SystemExit(f"Missing required env vars: {', '.join(_missing)}")

if shutil.which("ffmpeg") is None:
    raise SystemExit("ffmpeg not found in PATH. Install ffmpeg before running.")

BASE_URL = os.environ["BASE_URL"]
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]
TOKEN_DISCORD = os.environ["DISCORD_TOKEN"]

try:
    with open("config.json") as f:
        config = json.load(f)
except FileNotFoundError:
    raise SystemExit("config.json not found")
except json.JSONDecodeError as e:
    raise SystemExit(f"config.json is invalid JSON: {e}")

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


def _auth(x_internal_token: Annotated[str | None, Header()] = None):
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


_uvicorn_server: uvicorn.Server | None = None


def start_http():
    global _uvicorn_server
    cfg = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
    _uvicorn_server = uvicorn.Server(cfg)
    _uvicorn_server.run()


def _shutdown(_signum=None, _frame=None):
    log.info("Shutdown signal received, stopping services...")
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True
    if DiscordScripts.BOT_LOOP is not None:
        asyncio.run_coroutine_threadsafe(client.close(), DiscordScripts.BOT_LOOP)


signal.signal(signal.SIGTERM, _shutdown)


# ─────────────────────────────
# Register Discord handlers
# ─────────────────────────────

DiscordScripts.setup(client, config, CHANNEL_IDS, BASE_URL, INTERNAL_TOKEN, start_http)

# ─────────────────────────────
# HTTP endpoints
# ─────────────────────────────


@app.post("/post-schedule")
async def postSchedule(payload: dict, _: None = Depends(_auth)):
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
async def notifySessionExpired(payload: dict, _: None = Depends(_auth)):
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
async def notifyFailure(payload: dict, _: None = Depends(_auth)):
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
_shutdown()  # stop HTTP server when Discord connection closes

import hmac
import json
import asyncio
import logging
import os
import shutil
import signal
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
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

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

APP_ENV = os.environ.get("APP_ENV", "dev")
load_dotenv(os.path.join(_PROJECT_ROOT, f"env/.env.{APP_ENV}"))

_missing = [v for v in ("BASE_URL", "INTERNAL_TOKEN", "DISCORD_TOKEN", "WHITELIST_IDS", "PERMITTED_CLEAR_IDS") if not os.environ.get(v)]
if _missing:
    raise SystemExit(f"Missing required env vars: {', '.join(_missing)}")

if shutil.which("ffmpeg") is None:
    raise SystemExit("ffmpeg not found in PATH. Install ffmpeg before running.")

BASE_URL = os.environ["BASE_URL"]
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]
TOKEN_DISCORD = os.environ["DISCORD_TOKEN"]

try:
    with open(os.path.join(_PROJECT_ROOT, "config.json")) as f:
        config = json.load(f)
except FileNotFoundError:
    raise SystemExit("config.json not found")
except json.JSONDecodeError as e:
    raise SystemExit(f"config.json is invalid JSON: {e}")

def _parse_ids(env_var: str) -> list[int]:
    try:
        return [int(x.strip()) for x in os.environ[env_var].split(",") if x.strip()]
    except ValueError as e:
        raise SystemExit(f"Invalid integer in {env_var}: {e}")

config["whitelist"] = _parse_ids("WHITELIST_IDS")
config["permitted-id-clear-all-messages"] = _parse_ids("PERMITTED_CLEAR_IDS")
CHANNEL_IDS: dict = config["channels"]

_REQUIRED_CHANNELS = {"bots"}
_missing_channels = _REQUIRED_CHANNELS - set(CHANNEL_IDS.keys())
if _missing_channels:
    raise SystemExit(f"config.json missing required channel IDs: {', '.join(sorted(_missing_channels))}")

# Timeout constants — override via env vars if needed
_TIMEOUT_POST_SCHEDULE = int(os.environ.get("TIMEOUT_POST_SCHEDULE", "60"))
_TIMEOUT_NOTIFY = int(os.environ.get("TIMEOUT_NOTIFY", "10"))

# ─────────────────────────────
# Discord bot
# ─────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

# ─────────────────────────────
# FastAPI app / request models
# ─────────────────────────────


class FileItem(BaseModel):
    fileDir: str | None = None
    filename: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def require_path(self):
        if not self.fileDir and not self.filename:
            raise ValueError("each file must have 'fileDir' or 'filename'")
        return self


class PostSchedulePayload(BaseModel):
    channel: str = "bots"
    header: str = ""
    footer: str = ""
    files: list[FileItem] = Field(default=[], max_length=10)


class NotifySessionExpiredPayload(BaseModel):
    site: str = "unknown"


class NotifyFailurePayload(BaseModel):
    error: str = "unknown error"
    site: str = "unknown"
    entry_id: str = "?"


app = FastAPI()

_limiter = Limiter(key_func=get_remote_address)
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def _auth(x_internal_token: Annotated[str | None, Header()] = None):
    if not hmac.compare_digest(x_internal_token or "", INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


_uvicorn_server: uvicorn.Server | None = None


def start_http():
    global _uvicorn_server
    cfg = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
    _uvicorn_server = uvicorn.Server(cfg)
    _uvicorn_server.run()


_shutdown_called = False


def _shutdown(_signum=None, _frame=None):
    global _shutdown_called
    if _shutdown_called:
        return
    _shutdown_called = True
    log.info("Shutdown signal received, stopping services...")
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True
    loop = DiscordScripts.get_bot_loop()
    if loop is not None and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(client.close(), loop)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


# ─────────────────────────────
# Register Discord handlers
# ─────────────────────────────

DiscordScripts.setup(client, config, start_http)

# ─────────────────────────────
# HTTP endpoints
# ─────────────────────────────


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/ready")
async def ready():
    if DiscordScripts.get_bot_loop() is None:
        raise HTTPException(status_code=503, detail="bot not ready")
    return {"ok": True}


@app.post("/post-schedule")
@_limiter.limit("10/minute")
async def postSchedule(request: Request, payload: PostSchedulePayload, _: None = Depends(_auth)):
    loop = DiscordScripts.get_bot_loop()
    if loop is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "bot not ready yet"})

    fut = asyncio.run_coroutine_threadsafe(
        DiscordScripts.post_payload(payload.model_dump(), client, CHANNEL_IDS, BASE_URL, INTERNAL_TOKEN),
        loop,
    )

    try:
        await asyncio.wait_for(asyncio.wrap_future(fut), timeout=_TIMEOUT_POST_SCHEDULE)
    except asyncio.TimeoutError:
        fut.cancel()
        log.error("postSchedule timed out")
        return {"ok": False, "error": "internal error"}
    except Exception as e:
        log.error("postSchedule failed: %s", e, exc_info=True)
        return {"ok": False, "error": "internal error"}

    return {"ok": True}


@app.post("/notify-session-expired")
@_limiter.limit("20/minute")
async def notifySessionExpired(request: Request, payload: NotifySessionExpiredPayload, _: None = Depends(_auth)):
    loop = DiscordScripts.get_bot_loop()
    if loop is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "bot not ready yet"})

    fut = asyncio.run_coroutine_threadsafe(
        DiscordScripts.post_session_expired(payload.model_dump(), client, CHANNEL_IDS),
        loop,
    )

    try:
        await asyncio.wait_for(asyncio.wrap_future(fut), timeout=_TIMEOUT_NOTIFY)
    except asyncio.TimeoutError:
        fut.cancel()
        log.error("notifySessionExpired timed out")
        return {"ok": False, "error": "internal error"}
    except Exception as e:
        log.error("notifySessionExpired failed: %s", e, exc_info=True)
        return {"ok": False, "error": "internal error"}

    return {"ok": True}


@app.post("/notify-failure")
@_limiter.limit("20/minute")
async def notifyFailure(request: Request, payload: NotifyFailurePayload, _: None = Depends(_auth)):
    loop = DiscordScripts.get_bot_loop()
    if loop is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "bot not ready yet"})

    fut = asyncio.run_coroutine_threadsafe(
        DiscordScripts.post_failure(payload.model_dump(), client, CHANNEL_IDS),
        loop,
    )

    try:
        await asyncio.wait_for(asyncio.wrap_future(fut), timeout=_TIMEOUT_NOTIFY)
    except asyncio.TimeoutError:
        fut.cancel()
        log.error("notifyFailure timed out")
        return {"ok": False, "error": "internal error"}
    except Exception as e:
        log.error("notifyFailure failed: %s", e, exc_info=True)
        return {"ok": False, "error": "internal error"}

    return {"ok": True}


# ─────────────────────────────
# Start bot
# ─────────────────────────────

if __name__ == "__main__":
    try:
        client.run(TOKEN_DISCORD)
    except discord.errors.LoginFailure:
        log.error("Invalid Discord token — check DISCORD_TOKEN")
        sys.exit(1)
    except Exception as e:
        log.error("Discord client exited with error: %s", e, exc_info=True)
    finally:
        _shutdown()
        http_thread = DiscordScripts.get_http_thread()
        if http_thread is not None:
            log.info("Waiting for HTTP server to finish (max 30s)...")
            http_thread.join(timeout=30)
            if http_thread.is_alive():
                log.warning("HTTP server did not stop within 30s")
        asyncio.run(DiscordScripts.close_http_session())

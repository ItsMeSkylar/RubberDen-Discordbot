import asyncio
import json
import logging
import signal
import sys

import discord
from discord.ext import commands
import uvicorn

from services import api
from services.config import TOKEN_DISCORD, config
from services import DiscordScripts

# ─────────────────────────────
# Logging (structured JSON)
# ─────────────────────────────


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj)


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)

# ─────────────────────────────
# Discord bot
# ─────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

api.set_client(client)

# ─────────────────────────────
# HTTP server
# ─────────────────────────────

_uvicorn_server: uvicorn.Server | None = None


def start_http():
    global _uvicorn_server
    cfg = uvicorn.Config(api.app, host="127.0.0.1", port=8000, log_level="info")
    _uvicorn_server = uvicorn.Server(cfg)
    _uvicorn_server.run()


# ─────────────────────────────
# Shutdown
# ─────────────────────────────

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
# Wire up Discord handlers
# ─────────────────────────────

DiscordScripts.setup(client, config, start_http)

# ─────────────────────────────
# Entry point
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

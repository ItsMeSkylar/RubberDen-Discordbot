import asyncio
import hmac
import logging
import math
import time
from typing import Annotated

import discord
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, make_asgi_app, REGISTRY
from prometheus_client.core import GaugeMetricFamily
from pydantic import BaseModel, Field, model_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import BASE_URL, CHANNEL_IDS, INTERNAL_TOKEN, TIMEOUT_POST_SCHEDULE, TIMEOUT_NOTIFY
from . import discord_scripts

log = logging.getLogger(__name__)

# ─────────────────────────────
# Metrics
# ─────────────────────────────

_http_requests = Counter(
    "jenniferbot_http_requests_total",
    "Total HTTP requests",
    ["endpoint", "status"],
)
_http_latency = Histogram(
    "jenniferbot_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["endpoint"],
)

_client: discord.Client | None = None


def set_client(c: discord.Client) -> None:
    global _client
    _client = c


class _BotReadyCollector:
    def collect(self):
        g = GaugeMetricFamily("jenniferbot_bot_ready", "1 if Discord bot is ready")
        g.add_metric([], 1.0 if (_client is not None and _client.is_ready()) else 0.0)
        yield g


REGISTRY.register(_BotReadyCollector())

# ─────────────────────────────
# Request models
# ─────────────────────────────


class FileItem(BaseModel):
    file_dir: str | None = None
    filename: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def require_path(self):
        if not self.file_dir and not self.filename:
            raise ValueError("each file must have 'file_dir' or 'filename'")
        return self


class PostSchedulePayload(BaseModel):
    channel: str = "bots"
    header: str | None = ""
    footer: str | None = ""
    files: list[FileItem] = Field(default=[], max_length=10)

    @model_validator(mode="after")
    def validate_channel(self):
        if self.channel not in CHANNEL_IDS:
            raise ValueError(f"unknown channel '{self.channel}', valid: {sorted(CHANNEL_IDS)}")
        if not self.files and not self.header:
            raise ValueError("payload must have at least one file or a header")
        return self


class NotifySessionExpiredPayload(BaseModel):
    site: str = Field(default="unknown", max_length=100)


class NotifyFailurePayload(BaseModel):
    error: str = Field(default="unknown error", max_length=1800)
    site: str = Field(default="unknown", max_length=100)
    entry_id: str = Field(default="?", max_length=100)
    title: str = Field(default="", max_length=500)


class NotifyPendingPayload(BaseModel):
    site: str = Field(default="unknown", max_length=100)
    title: str = Field(default="", max_length=500)
    publish_url: str = Field(max_length=2000)
    reminder: bool = False
    failed: bool = False
    error: str = Field(default="", max_length=1800)


# ─────────────────────────────
# App
# ─────────────────────────────

app = FastAPI()

_limiter = Limiter(key_func=get_remote_address)
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start
    path = request.url.path
    _http_latency.labels(endpoint=path).observe(duration)
    _http_requests.labels(endpoint=path, status=str(response.status_code)).inc()
    if response.status_code == 422:
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        log.warning("422 validation error on %s: %s", path, body.decode(errors="replace"))
        return Response(content=body, status_code=422, media_type=response.media_type)
    return response


def _auth(x_internal_token: Annotated[str | None, Header()] = None):
    if not hmac.compare_digest(x_internal_token or "", INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _dispatch(coro, timeout: float, name: str):
    """Dispatch a coroutine to the Discord bot loop and await it with a timeout."""
    loop = discord_scripts.get_bot_loop()
    if loop is None:
        coro.close()
        raise HTTPException(status_code=503, detail="bot not ready yet")

    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)
    except asyncio.TimeoutError:
        fut.cancel()
        log.error("%s timed out", name)
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        log.error("%s failed: %s", name, e, exc_info=True)
        return {"ok": False, "error": str(e)}
    return {"ok": True}


# ─────────────────────────────
# Endpoints
# ─────────────────────────────


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/ready")
async def ready():
    if _client is None or not _client.is_ready() or math.isnan(_client.latency):
        raise HTTPException(status_code=503, detail="bot not ready")
    return {"ok": True}


@app.post("/post-schedule")
@_limiter.limit("10/minute")
async def post_schedule(request: Request, payload: PostSchedulePayload, _: None = Depends(_auth)):
    return await _dispatch(
        discord_scripts.post_payload(payload.model_dump(), _client, CHANNEL_IDS, BASE_URL, INTERNAL_TOKEN),
        TIMEOUT_POST_SCHEDULE,
        "post_schedule",
    )


@app.post("/notify-session-expired")
@_limiter.limit("20/minute")
async def notify_session_expired(request: Request, payload: NotifySessionExpiredPayload, _: None = Depends(_auth)):
    return await _dispatch(
        discord_scripts.notify_session_expired(payload.model_dump(), _client, CHANNEL_IDS),
        TIMEOUT_NOTIFY,
        "notify_session_expired",
    )


@app.post("/notify-failure")
@_limiter.limit("20/minute")
async def notify_failure(request: Request, payload: NotifyFailurePayload, _: None = Depends(_auth)):
    return await _dispatch(
        discord_scripts.notify_failure(payload.model_dump(), _client, CHANNEL_IDS),
        TIMEOUT_NOTIFY,
        "notify_failure",
    )


@app.post("/notify-pending")
@_limiter.limit("20/minute")
async def notify_pending(request: Request, payload: NotifyPendingPayload, _: None = Depends(_auth)):
    return await _dispatch(
        discord_scripts.notify_pending(payload.model_dump(), _client, CHANNEL_IDS),
        TIMEOUT_NOTIFY,
        "notify_pending",
    )


@app.post("/send-debug-image")
@_limiter.limit("5/minute")
async def send_debug_image(
    request: Request,
    file: UploadFile = File(...),
    caption: str = Form(default=""),
    _: None = Depends(_auth),
):
    data = await file.read()
    return await _dispatch(
        discord_scripts.send_debug_image(data, file.filename or "debug.png", caption, _client, CHANNEL_IDS),
        TIMEOUT_NOTIFY,
        "send_debug_image",
    )

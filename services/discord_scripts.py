import io
import logging
import asyncio
import os
import threading
from typing import Callable

import discord
import aiohttp
from prometheus_client import Counter

from .scrubber import scrub_metadata_bytes
from .config import NOTIFY_PING_IDS

_discord_send_total = Counter(
    "jenniferbot_discord_send_total",
    "Discord channel.send attempts",
    ["outcome"],
)

log = logging.getLogger(__name__)

# ─────────────────────────────
# Bot loop (shared across threads)
# ─────────────────────────────

# Written once (per reconnect) from the Discord event loop thread;
# read from the HTTP/uvicorn thread.
BOT_LOOP: asyncio.AbstractEventLoop | None = None
_bot_loop_lock = threading.Lock()

_http_thread: threading.Thread | None = None
_http_thread_lock = threading.Lock()


def get_http_thread() -> threading.Thread | None:
    with _http_thread_lock:
        return _http_thread


def get_bot_loop() -> asyncio.AbstractEventLoop | None:
    with _bot_loop_lock:
        return BOT_LOOP


def _set_bot_loop(loop: asyncio.AbstractEventLoop) -> None:
    global BOT_LOOP
    with _bot_loop_lock:
        BOT_LOOP = loop


# ─────────────────────────────
# HTTP session
# ─────────────────────────────

_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
_TIMEOUT_DISCORD_SEND = 30  # seconds
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)

_http_session: aiohttp.ClientSession | None = None


async def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=_HTTP_TIMEOUT)
    return _http_session


async def close_http_session() -> None:
    """Close the shared aiohttp session. Call this on shutdown."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None


# ─────────────────────────────
# Discord send with retry
# ─────────────────────────────


async def _send_with_retry(channel, **kwargs):
    """Send a Discord message with up to 3 attempts and exponential backoff.

    Respects Discord's Retry-After header on 429 rate-limit responses.
    """
    for attempt in range(3):
        try:
            await asyncio.wait_for(channel.send(**kwargs), timeout=_TIMEOUT_DISCORD_SEND)
            _discord_send_total.labels(outcome="success").inc()
            return
        except (discord.HTTPException, asyncio.TimeoutError) as e:
            if attempt == 2:
                _discord_send_total.labels(outcome="failure").inc()
                raise
            if isinstance(e, discord.HTTPException) and e.status == 429:
                retry_after = float(getattr(e, "retry_after", 2 ** attempt))
                log.warning("rate limited by Discord, retrying in %.1fs", retry_after)
                await asyncio.sleep(retry_after)
            else:
                delay = 2 ** attempt
                log.warning(
                    "channel.send attempt %d failed: %s — retrying in %ds",
                    attempt + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)


# ─────────────────────────────
# File download
# ─────────────────────────────


async def _download_file(
    session: aiohttp.ClientSession,
    file_url: str,
    headers: dict,
    item: dict,
) -> tuple[str, bytes, str, str, str | None, str]:
    """Download and validate a single file item.

    Returns (filename, data, desc, content_type, video_link, file_path).
    """
    file_path = item.get("file_dir") or item.get("filename")
    if not file_path:
        raise RuntimeError(f"file missing filename/file_dir: {item}")

    # Normalize to forward slashes and resolve any . / .. segments.
    normalized = os.path.normpath(file_path).replace("\\", "/")
    # Reject traversal sequences and Windows drive letters.
    if ".." in normalized or (len(file_path) >= 2 and file_path[1] == ":"):
        raise ValueError(f"Rejected unsafe file path: {file_path!r}")
    # Absolute paths are only allowed under the expected content root.
    if normalized.startswith("/") and not normalized.startswith(_ALLOWED_PREFIX):
        raise ValueError(f"Rejected unsafe file path: {file_path!r}")

    filename = file_path.rsplit("/", 1)[-1]
    desc = item.get("description") or ""

    for attempt in range(3):
        try:
            async with session.get(file_url, params={"path": file_path}, headers=headers) as r:
                if r.status != 200:
                    text = await r.text()
                    if attempt < 2 and r.status >= 500:
                        log.warning(
                            "backend returned %d for %s, retrying in %ds",
                            r.status, file_path, 2 ** attempt,
                        )
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(f"backend file failed: {r.status} {text[:200]}")

                content_length = r.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        cl_int = int(content_length)
                    except ValueError:
                        log.warning(
                            "malformed Content-Length header %r for %s, skipping pre-check",
                            content_length, file_path,
                        )
                        cl_int = None
                    if cl_int is not None and cl_int > _MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(
                            f"file too large: {content_length} bytes (max {_MAX_DOWNLOAD_BYTES})"
                        )

                chunks = []
                total = 0
                async for chunk in r.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(
                            f"file too large: exceeded {_MAX_DOWNLOAD_BYTES} bytes"
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)

                ct = (r.headers.get("Content-Type") or "").lower()
                video_link = r.headers.get("X-Video-Link")
                return filename, data, desc, ct, video_link, file_path

        except (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError, asyncio.TimeoutError) as e:
            if attempt < 2:
                log.warning(
                    "network error fetching %s (attempt %d): %s — retrying in %ds",
                    file_path, attempt + 1, e, 2 ** attempt,
                )
                await asyncio.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"network error fetching {file_path} after 3 attempts: {e}") from e

    raise RuntimeError(f"failed to download {file_path}")  # unreachable


# ─────────────────────────────
# Posting
# ─────────────────────────────

_MAX_MSG_FIELD = 1800  # leave headroom below Discord's 2000-char limit
_ALLOWED_PREFIX = "/Apps/Shared/content/"


async def post_payload(
    payload: dict,
    client: discord.Client,
    channel_ids: dict,
    base_url: str,
    internal_token: str,
):
    site = payload.get("channel") or "bots"
    channel_id = channel_ids.get(site) or channel_ids["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    header_text = payload.get("header") or ""
    footer_text = payload.get("footer") or ""
    files_meta = payload.get("files") or []

    session = await _get_http_session()
    headers = {"X-Internal-Token": internal_token}
    file_url = f"{base_url}/internal/file"

    downloaded = []
    for item in files_meta:
        try:
            filename, data, desc, ct, video_link, file_path = await _download_file(
                session, file_url, headers, item,
            )
            scrub_name = (filename.rsplit(".", 1)[0] + ".jpg") if video_link else filename
            data = await scrub_metadata_bytes(data, scrub_name)
            downloaded.append((filename, data, desc, ct, video_link, file_path))
            log.info("OK: %s  bytes: %d  ct: %s  video: %s", filename, len(data), ct, bool(video_link))
        except Exception:
            log.error("FAILED ITEM: %s", item, exc_info=True)
            raise

    def is_image(name: str, ct: str) -> bool:
        return ct.startswith("image/") or name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))

    def thumb_name_for_video(video_filename: str) -> str:
        return video_filename.rsplit(".", 1)[0] + ".jpg"

    embeds = []
    attachments = []

    for filename, data, desc, ct, video_link, file_path in downloaded:
        is_video = file_path.lower().endswith((".mp4", ".mov", ".m4v", ".webm")) or bool(video_link)

        embed = discord.Embed(description=desc or " ", colour=0x9900FF)
        if footer_text:
            embed.set_footer(text=footer_text)

        if is_video:
            thumb_name = thumb_name_for_video(filename)
            attachments.append(discord.File(fp=io.BytesIO(data), filename=thumb_name))
            embed.set_image(url=f"attachment://{thumb_name}")
            if video_link:
                embed.add_field(name="Link to video:", value=video_link, inline=False)
        else:
            attachments.append(discord.File(fp=io.BytesIO(data), filename=filename))
            if is_image(filename, ct):
                embed.set_image(url=f"attachment://{filename}")

        embeds.append(embed)

    await _send_with_retry(
        channel,
        content=header_text or None,
        embeds=embeds,
        files=attachments,
    )
    log.info("sent to #%s: %d file(s)", channel.name, len(downloaded))


async def notify_session_expired(payload: dict, client: discord.Client, channel_ids: dict):
    channel_id = channel_ids["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    site = (payload.get("site") or "unknown")[:100]
    platform = site.capitalize()

    await _send_with_retry(
        channel,
        content=(
            f"⚠️ **Session expired: {platform}**\n"
            f"Run this on your local machine to refresh:\n"
            f"```\npython scripts/refresh-session.py {site.lower()}\n```"
        ),
    )


async def notify_failure(payload: dict, client: discord.Client, channel_ids: dict):
    channel_id = channel_ids["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    error = (payload.get("error") or "unknown error")[:_MAX_MSG_FIELD]
    site = (payload.get("site") or "unknown")[:100]
    entry_id = (payload.get("entry_id") or "?")[:100]

    await _send_with_retry(
        channel,
        content=f"❌ **Cron job failed**\nSite: `{site}` | Entry: `{entry_id}`\n```{error}```",
    )


_PLATFORM_ICONS = {
    "twitter": "🐦",
    "patreon": "🎨",
    "bluesky": "☁️",
}

_PENDING_VIEW_TIMEOUT = 7 * 24 * 3600  # 7 days


class _PostNotifyView(discord.ui.View):
    """Buttons attached to a pending-post notification message."""

    def __init__(self, publish_url: str, button_label: str = "Publish now"):
        super().__init__(timeout=_PENDING_VIEW_TIMEOUT)
        self.add_item(
            discord.ui.Button(
                label=button_label,
                style=discord.ButtonStyle.link,
                url=publish_url,
                emoji="🚀",
                row=0,
            )
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()


async def notify_pending(payload: dict, client: discord.Client, channel_ids: dict):
    channel_id = channel_ids["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    site = (payload.get("site") or "unknown")[:100]
    title = (payload.get("title") or "")[:500]
    publish_url = (payload.get("publish_url") or "")[:2000]
    reminder = payload.get("reminder", False)
    failed = payload.get("failed", False)
    error = (payload.get("error") or "unknown error")[:_MAX_MSG_FIELD]

    platform = site.capitalize()
    icon = _PLATFORM_ICONS.get(site.lower(), "📋")

    if failed:
        embed = discord.Embed(
            title=f"❌  {platform} post failed",
            color=0xE53935,
        )
        if title:
            embed.add_field(name="Post", value=title, inline=False)
        embed.add_field(name="Error", value=f"```{error}```", inline=False)
        view = _PostNotifyView(publish_url, button_label="Retry")
        ping_content = None
    elif reminder:
        embed = discord.Embed(
            title=f"⏰  Reminder: {platform} post still pending",
            color=0xFB8C00,
        )
        if title:
            embed.add_field(name="Post", value=title, inline=False)
        view = _PostNotifyView(publish_url)
        ping_content = None
    else:
        embed = discord.Embed(
            title=f"{icon}  {platform} post ready",
            color=0x7B2FBE,
        )
        if title:
            embed.add_field(name="Post", value=title, inline=False)
        embed.add_field(name="Platform", value=platform, inline=True)
        embed.set_footer(text="Click Publish now to send it live")
        view = _PostNotifyView(publish_url)
        ping = " ".join(f"<@{uid}>" for uid in NOTIFY_PING_IDS)
        ping_content = ping or None

    await _send_with_retry(channel, content=ping_content, embed=embed, view=view)


async def send_debug_image(image_bytes: bytes, filename: str, caption: str, client: discord.Client, channel_ids: dict):
    channel_id = channel_ids["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    f = discord.File(fp=io.BytesIO(image_bytes), filename=filename)
    await _send_with_retry(channel, content=caption[:_MAX_MSG_FIELD] or None, files=[f])


# ─────────────────────────────
# Discord event handlers
# ─────────────────────────────


def setup(
    client: discord.Client,
    config: dict,
    start_http: Callable,
):
    """Register Discord event handlers and slash commands on the given client."""

    _http_started = False
    _synced = False

    @client.event
    async def on_ready():
        nonlocal _http_started, _synced
        _set_bot_loop(asyncio.get_running_loop())

        if not _http_started:
            _http_started = True

            def _run_http_safe():
                try:
                    start_http()
                except Exception:
                    log.error("HTTP server crashed", exc_info=True)

            global _http_thread
            t = threading.Thread(target=_run_http_safe, daemon=False, name="http-server")
            with _http_thread_lock:
                _http_thread = t
            t.start()

        await client.change_presence(activity=discord.Game(name="Sqrrrks~"))

        if not _synced:
            _synced = True
            await client.tree.sync()
            log.info("Command tree synced successfully.")

        log.info("JenniferBot ready!")

    @client.tree.command(name="clear_all_messages", description="Purge all messages in a permitted channel")
    @discord.app_commands.default_permissions(administrator=True)
    async def clear_all_messages(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if interaction.user.id not in config["whitelist"]:
            return await interaction.response.send_message("Not authorized")

        if channel.id not in config["permitted-ids-clear-all-messages"]:
            return await interaction.response.send_message(
                f"{channel} is not permitted to clear messages"
            )

        await interaction.response.defer()
        await channel.purge(limit=None)
        await interaction.followup.send("Done")

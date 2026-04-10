import io
import logging
import asyncio
import os
import tempfile
import threading
from typing import Callable

import discord
import aiohttp
from PIL import Image, ImageOps
from prometheus_client import Counter

_scrub_total = Counter(
    "jenniferbot_scrub_total",
    "Metadata scrub operations",
    ["media_type", "outcome"],
)
_discord_send_total = Counter(
    "jenniferbot_discord_send_total",
    "Discord channel.send attempts",
    ["outcome"],
)

log = logging.getLogger(__name__)

# Thread-safe bot loop access.  Written once (per reconnect) from the Discord
# event loop thread; read from the HTTP/uvicorn thread.
BOT_LOOP: asyncio.AbstractEventLoop | None = None
_bot_loop_lock = threading.Lock()

_http_thread: threading.Thread | None = None


def get_http_thread() -> threading.Thread | None:
    return _http_thread

_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
_TIMEOUT_DISCORD_SEND = 30  # seconds
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)

_http_session: aiohttp.ClientSession | None = None


def get_bot_loop() -> asyncio.AbstractEventLoop | None:
    with _bot_loop_lock:
        return BOT_LOOP


def _set_bot_loop(loop: asyncio.AbstractEventLoop) -> None:
    global BOT_LOOP
    with _bot_loop_lock:
        BOT_LOOP = loop


async def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=_HTTP_TIMEOUT)
    return _http_session


async def close_http_session() -> None:
    """Close the shared aiohttp session.  Call this on shutdown."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None


def scrub_image_bytes(data: bytes) -> bytes:
    """Apply EXIF orientation physically, then strip all metadata. Returns scrubbed bytes."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # physically rotate based on EXIF orientation
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


async def scrub_video_bytes(data: bytes, filename: str) -> bytes:
    """Strip metadata from video bytes using ffmpeg. Returns scrubbed bytes."""
    ext = os.path.splitext(filename)[1].lower() or ".bin"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_in:
        tmp_in.write(data)
        in_path = tmp_in.name

    out_fd, out_path = tempfile.mkstemp(suffix=ext)
    os.close(out_fd)

    try:
        args = ["ffmpeg", "-y", "-i", in_path, "-map_metadata", "-1", "-c", "copy", out_path]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            log.error("ffmpeg timed out for %s, using original", filename)
            _scrub_total.labels(media_type="video", outcome="timeout").inc()
            return data

        if proc.returncode != 0:
            log.error(
                "ffmpeg metadata scrub failed for %s: %s",
                filename,
                stderr[-500:].decode(errors="replace"),
            )
            _scrub_total.labels(media_type="video", outcome="failure").inc()
            return data  # fall back to original

        _scrub_total.labels(media_type="video", outcome="success").inc()
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(in_path)
        except OSError:
            pass
        try:
            os.unlink(out_path)
        except OSError:
            pass


async def scrub_metadata_bytes(data: bytes, filename: str) -> bytes:
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    if ext in _IMAGE_EXTS:
        try:
            result = scrub_image_bytes(data)
            _scrub_total.labels(media_type="image", outcome="success").inc()
            return result
        except Exception as e:
            log.error("Pillow scrub failed for %s: %s", filename, e)
            _scrub_total.labels(media_type="image", outcome="failure").inc()
            return data
    if ext in _VIDEO_EXTS:
        return await scrub_video_bytes(data, filename)
    return data


async def _send_with_retry(channel, **kwargs):
    """Send a Discord message with up to 3 attempts and exponential backoff.

    Respects Discord's Retry-After header on 429 rate-limit responses.
    """
    for attempt in range(3):
        try:
            await asyncio.wait_for(channel.send(**kwargs), timeout=_TIMEOUT_DISCORD_SEND)
            _discord_send_total.labels(outcome="success").inc()
            return
        except discord.HTTPException as e:
            if attempt == 2:
                _discord_send_total.labels(outcome="failure").inc()
                raise
            if e.status == 429:
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

    headers = {"X-Internal-Token": internal_token}
    file_url = f"{base_url}/internal/file"

    # (filename, bytes, desc, content_type, video_link, file_path)
    downloaded = []

    session = await _get_http_session()
    for item in files_meta:
        try:
            file_path = item.get("fileDir") or item.get("filename")
            if not file_path:
                raise RuntimeError(f"file missing filename/fileDir: {item}")

            # Reject paths with traversal sequences or absolute paths.
            # Explicit string checks supplement os.path.isabs(), which returns
            # False for POSIX-style "/foo" paths on Windows.
            normalized = os.path.normpath(file_path)
            if (
                os.path.isabs(normalized)
                or normalized.startswith("..")
                or file_path.startswith("/")
                or file_path.startswith("\\")
                or (len(file_path) >= 2 and file_path[1] == ":")  # Windows drive letter
            ):
                raise ValueError(f"Rejected unsafe file path: {file_path!r}")

            filename = file_path.rsplit("/", 1)[-1]
            desc = item.get("description") or ""

            for attempt in range(3):
                try:
                    async with session.get(
                        file_url,
                        params={"path": file_path},
                        headers=headers,
                    ) as r:
                        if r.status != 200:
                            text = await r.text()
                            if attempt < 2 and r.status >= 500:
                                log.warning(
                                    "backend returned %d for %s, retrying in %ds",
                                    r.status,
                                    file_path,
                                    2 ** attempt,
                                )
                                await asyncio.sleep(2 ** attempt)
                                continue
                            raise RuntimeError(
                                f"backend file failed: {r.status} {text[:200]}"
                            )

                        content_length = r.headers.get("Content-Length")
                        if content_length is not None:
                            try:
                                cl_int = int(content_length)
                            except ValueError:
                                log.warning(
                                    "malformed Content-Length header %r for %s, skipping pre-check",
                                    content_length,
                                    file_path,
                                )
                                cl_int = None
                            if cl_int is not None and cl_int > _MAX_DOWNLOAD_BYTES:
                                raise RuntimeError(
                                    f"file too large: {content_length} bytes (max {_MAX_DOWNLOAD_BYTES})"
                                )

                        data = await r.read()
                        if len(data) > _MAX_DOWNLOAD_BYTES:
                            raise RuntimeError(
                                f"file too large: {len(data)} bytes (max {_MAX_DOWNLOAD_BYTES})"
                            )

                        ct = (r.headers.get("Content-Type") or "").lower()
                        video_link = r.headers.get("X-Video-Link")
                        break
                except (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError, asyncio.TimeoutError) as e:
                    if attempt < 2:
                        log.warning(
                            "network error fetching %s (attempt %d): %s — retrying in %ds",
                            file_path,
                            attempt + 1,
                            e,
                            2 ** attempt,
                        )
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(f"network error fetching {file_path} after 3 attempts: {e}") from e

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
        stem = video_filename.rsplit(".", 1)[0]
        return f"{stem}.jpg"

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


_MAX_MSG_FIELD = 1800  # leave headroom below Discord's 2000-char limit


async def post_session_expired(payload: dict, client: discord.Client, channel_ids: dict):
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


async def post_failure(payload: dict, client: discord.Client, channel_ids: dict):
    channel_id = channel_ids["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    error = (payload.get("error") or "unknown error")[:_MAX_MSG_FIELD]
    site = (payload.get("site") or "unknown")[:100]
    entry_id = (payload.get("entry_id") or "?")[:100]

    await _send_with_retry(
        channel,
        content=f"❌ **Cron job failed**\nSite: `{site}` | Entry: `{entry_id}`\n```{error}```",
    )


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
            _http_thread = threading.Thread(target=_run_http_safe, daemon=False, name="http-server")
            _http_thread.start()

        await client.change_presence(activity=discord.Game(name="Sqrrrks~"))

        if not _synced:
            _synced = True
            await client.tree.sync()
            log.info("Command tree synced successfully.")

        log.info("JenniferBot ready!")

    @client.tree.command(name="clear_all_messages")
    async def clear_all_messages(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if interaction.user.id not in config["whitelist"]:
            return await interaction.response.send_message("Not authorized")

        if channel.id not in config["permitted-id-clear-all-messages"]:
            return await interaction.response.send_message(
                f"{channel} is not permitted to clear messages"
            )

        await interaction.response.defer()
        await channel.purge(limit=None)

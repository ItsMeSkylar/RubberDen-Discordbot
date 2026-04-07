import io
import logging
import asyncio
import os
import tempfile
import threading
from typing import Callable

import discord
import aiohttp

log = logging.getLogger(__name__)

BOT_LOOP: asyncio.AbstractEventLoop | None = None

_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv"}


async def scrub_metadata_bytes(data: bytes, filename: str) -> bytes:
    """Strip EXIF/metadata from image or video bytes using ffmpeg. Returns scrubbed bytes."""
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    is_video = ext in _VIDEO_EXTS

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_in:
        tmp_in.write(data)
        in_path = tmp_in.name

    out_fd, out_path = tempfile.mkstemp(suffix=ext)
    os.close(out_fd)

    try:
        args = ["ffmpeg", "-y", "-i", in_path, "-map_metadata", "-1"]
        if is_video:
            args += ["-c", "copy"]
        else:
            args += ["-q:v", "2"]
        args.append(out_path)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            log.warning("ffmpeg metadata scrub failed for %s: %s", filename, stderr[-500:].decode(errors="replace"))
            return data  # fall back to original

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

    async with aiohttp.ClientSession() as session:
        for item in files_meta:
            try:
                file_path = item.get("fileDir") or item.get("filename")
                if not file_path:
                    raise RuntimeError(f"file missing filename/fileDir: {item}")

                filename = file_path.rsplit("/", 1)[-1]
                desc = item.get("description") or ""

                async with session.get(
                    file_url,
                    params={"path": file_path},
                    headers=headers,
                ) as r:
                    if r.status != 200:
                        text = await r.text()
                        raise RuntimeError(
                            f"backend file failed: {r.status} {text[:200]}"
                        )

                    data = await r.read()
                    ct = (r.headers.get("Content-Type") or "").lower()
                    video_link = r.headers.get("X-Video-Link")

                data = await scrub_metadata_bytes(data, filename)
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

    await channel.send(
        content=header_text or None,
        embeds=embeds,
        files=attachments,
    )


async def post_session_expired(payload: dict, client: discord.Client, channel_ids: dict):
    channel_id = channel_ids["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    site = payload.get("site") or "unknown"
    platform = site.capitalize()

    await channel.send(
        f"⚠️ **Session expired: {platform}**\n"
        f"Run this on your local machine to refresh:\n"
        f"```\npython scripts/refresh-session.py {site.lower()}\n```"
    )


async def post_failure(payload: dict, client: discord.Client, channel_ids: dict):
    channel_id = channel_ids["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    error = payload.get("error") or "unknown error"
    site = payload.get("site") or "unknown"
    entry_id = payload.get("entry_id") or "?"

    await channel.send(f"❌ **Cron job failed**\nSite: `{site}` | Entry: `{entry_id}`\n```{error}```")


def setup(
    client: discord.Client,
    config: dict,
    channel_ids: dict,
    base_url: str,
    internal_token: str,
    start_http: Callable,
):
    """Register Discord event handlers and slash commands on the given client."""

    global BOT_LOOP
    _http_started = [False]

    @client.event
    async def on_ready():
        global BOT_LOOP
        BOT_LOOP = asyncio.get_running_loop()

        if not _http_started[0]:
            _http_started[0] = True
            threading.Thread(target=start_http, daemon=True).start()

        await client.change_presence(activity=discord.Game(name="Sqrrrks~"))
        await client.tree.sync()
        log.info("Command tree synced successfully.")
        log.info("JenniferBot ready!")

    @client.tree.command(name="clear_all_messages")
    async def clear_all_messages(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if interaction.user.name not in config["whitelist"]:
            return await interaction.response.send_message("Not authorized")

        if channel.id not in config["permitted-id-clear-all-messages"]:
            return await interaction.response.send_message(
                f"{channel} is not permitted to clear messages"
            )

        await interaction.response.defer()
        await channel.purge(limit=None)

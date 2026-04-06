import io
import traceback
import asyncio
import threading
from typing import Callable

import discord
import aiohttp

BOT_LOOP: asyncio.AbstractEventLoop | None = None


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

                downloaded.append((filename, data, desc, ct, video_link, file_path))
                print("OK:", filename, "bytes:", len(data), "ct:", ct, "video:", bool(video_link))

            except Exception:
                print("FAILED ITEM:", item)
                traceback.print_exc()
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
        print("Command tree synced successfully.")
        print("JenniferBot ready!")

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

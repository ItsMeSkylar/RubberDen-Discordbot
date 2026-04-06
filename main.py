import traceback
import json
import io
import threading
import asyncio

import discord
from discord.ext import commands
import aiohttp

from fastapi import FastAPI
import uvicorn

# ─────────────────────────────
# Config / tokens
# ─────────────────────────────

with open("config.json") as config_file:
    config = json.load(config_file)

with open("tokens/TOKEN_DISCORD.txt", "r") as f:
    TOKEN_DISCORD = f.read().strip()


def start_http():
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


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
BOT_LOOP: asyncio.AbstractEventLoop | None = None
_http_started = False

# ─────────────────────────────
# http call
# ─────────────────────────────


@app.post("/post-schedule")
async def postSchedule(payload: dict):
    if BOT_LOOP is None:
        return {"ok": False, "error": "bot not ready yet"}

    fut = asyncio.run_coroutine_threadsafe(_post_payload(payload), BOT_LOOP)

    try:
        fut.result(timeout=60)  # allow time for file downloads + upload
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True}


async def _post_payload(payload: dict):
    site = payload.get("channel") or "bots"
    channel_id = CHANNEL_IDS.get(site) or CHANNEL_IDS["bots"]
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    header_text = payload.get("header") or ""
    footer_text = payload.get("footer") or ""
    files_meta = payload.get("files") or []

    headers = {"X-Internal-Token": INTERNAL_TOKEN}
    file_url = f"{BASE_URL}/internal/file"

    # (filename, bytes, desc, content_type, video_link, file_path)
    downloaded = []

    async with aiohttp.ClientSession() as session:
        for item in files_meta:
            try:
                file_path = item.get("fileDir") or item.get("filename")
                if not file_path:
                    raise RuntimeError(
                        f"file missing filename/fileDir: {item}")

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

                downloaded.append(
                    (filename, data, desc, ct, video_link, file_path)
                )

                print("OK:", filename, "bytes:", len(data),
                      "ct:", ct, "video:", bool(video_link))

            except Exception as e:
                print("FAILED ITEM:", item)
                traceback.print_exc()
                raise  # re-raise so the task actually errors instead of silently stopping

    embeds = []
    attachments = []

    def is_image(name: str, ct: str) -> bool:
        return ct.startswith("image/") or name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))

    def thumb_name_for_video(video_filename: str) -> str:
        stem = video_filename.rsplit(".", 1)[0]
        return f"{stem}.jpg"

    for filename, data, desc, ct, video_link, file_path in downloaded:
        is_video = file_path.lower().endswith(
            (".mp4", ".mov", ".m4v", ".webm")) or bool(video_link)

        embed = discord.Embed(description=desc or " ", colour=0x9900ff)
        if footer_text:
            embed.set_footer(text=footer_text)

        if is_video:
            # backend returns thumbnail bytes in `data`
            thumb_name = thumb_name_for_video(filename)
            attachments.append(discord.File(
                fp=io.BytesIO(data), filename=thumb_name))
            embed.set_image(url=f"attachment://{thumb_name}")

            if video_link:
                embed.add_field(name="Link to video:",
                                value=video_link, inline=False)
        else:
            attachments.append(discord.File(
                fp=io.BytesIO(data), filename=filename))
            if is_image(filename, ct):
                embed.set_image(url=f"attachment://{filename}")

        embeds.append(embed)

    await channel.send(
        content=header_text or None,
        embeds=embeds,
        files=attachments,
    )

# ─────────────────────────────
# Discord events
# ─────────────────────────────


@client.event
async def on_ready():
    global BOT_LOOP, _http_started

    BOT_LOOP = asyncio.get_running_loop()

    if not _http_started:
        _http_started = True
        threading.Thread(target=start_http, daemon=True).start()

    await client.change_presence(activity=discord.Game(name="Sqrrrks~"))
    await client.tree.sync()
    print("Command tree synced successfully.")
    print("JenniferBot ready!")

# ─────────────────────────────
# Utility command
# ─────────────────────────────


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

# ─────────────────────────────
# Start bot
# ─────────────────────────────

client.run(TOKEN_DISCORD)

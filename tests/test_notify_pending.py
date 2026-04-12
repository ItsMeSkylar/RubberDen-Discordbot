"""Unit tests for notify_pending and notify_session_expired discord functions."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from services.discord_scripts import notify_pending, notify_session_expired

CHANNEL_IDS = {"bots": 111}


def _client(channel):
    c = MagicMock()
    c.get_channel.return_value = channel
    return c


def _channel():
    ch = MagicMock()
    ch.send = AsyncMock()
    return ch


# ─────────────────────────────
# notify_session_expired
# ─────────────────────────────

async def test_session_expired_message_contains_site_name():
    channel = _channel()
    await notify_session_expired({"site": "patreon"}, _client(channel), CHANNEL_IDS)

    content = channel.send.call_args.kwargs["content"]
    assert "patreon" in content.lower()


async def test_session_expired_message_contains_refresh_script():
    channel = _channel()
    await notify_session_expired({"site": "twitter"}, _client(channel), CHANNEL_IDS)

    content = channel.send.call_args.kwargs["content"]
    assert "refresh-session.py" in content
    assert "twitter" in content


async def test_session_expired_defaults_to_unknown_site():
    channel = _channel()
    await notify_session_expired({}, _client(channel), CHANNEL_IDS)

    content = channel.send.call_args.kwargs["content"]
    assert "unknown" in content.lower()


# ─────────────────────────────
# notify_pending — normal branch
# ─────────────────────────────

async def test_pending_normal_title_contains_ready():
    channel = _channel()
    await notify_pending(
        {"site": "patreon", "title": "My Post", "publish_url": "https://example.com/p"},
        _client(channel),
        CHANNEL_IDS,
    )

    embed = channel.send.call_args.kwargs["embed"]
    assert "ready" in embed.title.lower()


async def test_pending_normal_has_publish_link_button():
    url = "https://example.com/publish"
    channel = _channel()
    await notify_pending(
        {"site": "patreon", "publish_url": url, "title": "A Post"},
        _client(channel),
        CHANNEL_IDS,
    )

    view = channel.send.call_args.kwargs["view"]
    link_buttons = [c for c in view.children if isinstance(c, discord.ui.Button) and c.style == discord.ButtonStyle.link]
    assert any(b.url == url for b in link_buttons)
    assert any(b.label == "Publish now" for b in link_buttons)


async def test_pending_normal_title_includes_platform():
    channel = _channel()
    await notify_pending(
        {"site": "twitter", "publish_url": "https://x.com/post"},
        _client(channel),
        CHANNEL_IDS,
    )

    embed = channel.send.call_args.kwargs["embed"]
    assert "Twitter" in embed.title


# ─────────────────────────────
# notify_pending — reminder branch
# ─────────────────────────────

async def test_pending_reminder_title_contains_reminder():
    channel = _channel()
    await notify_pending(
        {"site": "bluesky", "publish_url": "https://bsky.app/post", "reminder": True},
        _client(channel),
        CHANNEL_IDS,
    )

    embed = channel.send.call_args.kwargs["embed"]
    assert "reminder" in embed.title.lower()


async def test_pending_reminder_has_publish_button():
    url = "https://bsky.app/post"
    channel = _channel()
    await notify_pending(
        {"site": "bluesky", "publish_url": url, "reminder": True},
        _client(channel),
        CHANNEL_IDS,
    )

    view = channel.send.call_args.kwargs["view"]
    link_buttons = [c for c in view.children if isinstance(c, discord.ui.Button) and c.style == discord.ButtonStyle.link]
    assert any(b.url == url for b in link_buttons)


# ─────────────────────────────
# notify_pending — failed branch
# ─────────────────────────────

async def test_pending_failed_embed_is_red():
    channel = _channel()
    await notify_pending(
        {"site": "patreon", "publish_url": "https://p.com/post", "failed": True, "error": "timeout"},
        _client(channel),
        CHANNEL_IDS,
    )

    embed = channel.send.call_args.kwargs["embed"]
    assert embed.colour.value == 0xFF0000


async def test_pending_failed_title_mentions_failed():
    channel = _channel()
    await notify_pending(
        {"site": "patreon", "publish_url": "https://p.com/post", "failed": True},
        _client(channel),
        CHANNEL_IDS,
    )

    embed = channel.send.call_args.kwargs["embed"]
    assert "failed" in embed.title.lower()


async def test_pending_failed_has_retry_button():
    url = "https://p.com/post"
    channel = _channel()
    await notify_pending(
        {"site": "twitter", "publish_url": url, "failed": True, "error": "click failed"},
        _client(channel),
        CHANNEL_IDS,
    )

    view = channel.send.call_args.kwargs["view"]
    link_buttons = [c for c in view.children if isinstance(c, discord.ui.Button) and c.style == discord.ButtonStyle.link]
    assert any(b.label == "Retry" for b in link_buttons)
    assert any(b.url == url for b in link_buttons)


async def test_pending_failed_includes_error_in_embed():
    channel = _channel()
    await notify_pending(
        {"site": "patreon", "publish_url": "https://p.com/post", "failed": True, "error": "publish button not found"},
        _client(channel),
        CHANNEL_IDS,
    )

    embed = channel.send.call_args.kwargs["embed"]
    field_values = " ".join(f.value for f in embed.fields)
    assert "publish button not found" in field_values

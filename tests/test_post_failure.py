from unittest.mock import AsyncMock, MagicMock

from services.discord_scripts import notify_failure

CHANNEL_IDS = {"bots": 111}


def _client(channel):
    c = MagicMock()
    c.get_channel.return_value = channel
    return c


def _embed_text(channel) -> str:
    """Collect all text from the embed passed to channel.send."""
    embed = channel.send.call_args.kwargs["embed"]
    parts = [embed.title or ""]
    for field in embed.fields:
        parts.append(field.name)
        parts.append(field.value)
    return " ".join(parts)


async def test_failure_message_contains_payload_fields():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure(
        {"error": "timeout", "site": "mysite", "entry_id": "42"},
        _client(channel),
        CHANNEL_IDS,
    )

    text = _embed_text(channel)
    assert "mysite" in text
    assert "42" in text
    assert "timeout" in text


async def test_failure_defaults_when_payload_empty():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure({}, _client(channel), CHANNEL_IDS)

    text = _embed_text(channel)
    assert "unknown error" in text
    assert "unknown" in text


async def test_failure_message_includes_title_when_present():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure(
        {"error": "boom", "site": "patreon", "entry_id": "7", "title": "My Post"},
        _client(channel),
        CHANNEL_IDS,
    )

    text = _embed_text(channel)
    assert "My Post" in text


async def test_failure_embed_is_red():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure({"error": "x", "site": "s", "entry_id": "1"}, _client(channel), CHANNEL_IDS)

    embed = channel.send.call_args.kwargs["embed"]
    assert embed.colour.value == 0xFF0000

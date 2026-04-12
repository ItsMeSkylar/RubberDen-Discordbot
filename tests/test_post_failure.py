from unittest.mock import AsyncMock, MagicMock

from services.discord_scripts import notify_failure

CHANNEL_IDS = {"bots": 111}


def _client(channel):
    c = MagicMock()
    c.get_channel.return_value = channel
    return c


def _embed(channel):
    return channel.send.call_args.kwargs["embed"]


def _embed_text(channel) -> str:
    """Collect all text from the embed passed to channel.send."""
    embed = _embed(channel)
    parts = [embed.title or ""]
    for field in embed.fields:
        parts.append(field.name)
        parts.append(field.value)
    return " ".join(parts)


async def test_failure_message_contains_payload_fields():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure(
        {"error": "timeout", "site": "mysite"},
        _client(channel),
        CHANNEL_IDS,
    )

    text = _embed_text(channel)
    assert "mysite" in text
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
        {"error": "boom", "site": "patreon", "title": "My Post"},
        _client(channel),
        CHANNEL_IDS,
    )

    text = _embed_text(channel)
    assert "My Post" in text


async def test_failure_embed_is_red():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure({"error": "x", "site": "s"}, _client(channel), CHANNEL_IDS)

    assert _embed(channel).colour.value == 0xFF0000


async def test_discord_site_label():
    for site in ("shiny", "supershiny", "bots"):
        channel = MagicMock()
        channel.send = AsyncMock()

        await notify_failure({"error": "x", "site": site}, _client(channel), CHANNEL_IDS)

        fields = {f.name: f.value for f in _embed(channel).fields}
        assert "Discord channel" in fields, f"expected 'Discord channel' label for site={site}"
        assert site in fields["Discord channel"]


async def test_non_discord_site_label():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure({"error": "x", "site": "bluesky"}, _client(channel), CHANNEL_IDS)

    fields = {f.name: f.value for f in _embed(channel).fields}
    assert "Site" in fields
    assert "bluesky" in fields["Site"]

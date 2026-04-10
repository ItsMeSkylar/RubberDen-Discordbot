from unittest.mock import AsyncMock, MagicMock

from services.discord_scripts import notify_failure

CHANNEL_IDS = {"bots": 111}


def _client(channel):
    c = MagicMock()
    c.get_channel.return_value = channel
    return c


async def test_failure_message_contains_payload_fields():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure(
        {"error": "timeout", "site": "mysite", "entry_id": "42"},
        _client(channel),
        CHANNEL_IDS,
    )

    msg = channel.send.call_args.kwargs["content"]
    assert "mysite" in msg
    assert "42" in msg
    assert "timeout" in msg


async def test_failure_defaults_when_payload_empty():
    channel = MagicMock()
    channel.send = AsyncMock()

    await notify_failure({}, _client(channel), CHANNEL_IDS)

    msg = channel.send.call_args.kwargs["content"]
    assert "unknown error" in msg
    assert "unknown" in msg

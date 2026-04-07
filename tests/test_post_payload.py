import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.DiscordScripts import post_payload

CHANNEL_IDS = {"bots": 111, "shiny": 222}
BASE_URL = "http://localhost/api"
TOKEN = "test-token"


def _channel():
    ch = MagicMock()
    ch.send = AsyncMock()
    return ch


def _client(channel):
    c = MagicMock()
    c.get_channel.return_value = channel
    return c


def _mock_session(data=b"bytes", content_type="image/png", status=200, video_link=None):
    """Build a mock aiohttp.ClientSession async context manager."""
    headers = {"Content-Type": content_type}
    if video_link:
        headers["X-Video-Link"] = video_link

    r = MagicMock()
    r.status = status
    r.read = AsyncMock(return_value=data)
    r.text = AsyncMock(return_value="error body")
    r.headers = headers

    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=r)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get.return_value = get_cm

    cs_cm = MagicMock()
    cs_cm.__aenter__ = AsyncMock(return_value=session)
    cs_cm.__aexit__ = AsyncMock(return_value=False)

    return cs_cm


async def test_image_embed_has_image_and_footer():
    channel = _channel()
    payload = {
        "files": [{"filename": "pics/cat.png", "description": "a cat"}],
        "header": "Hello",
        "footer": "Footer text",
    }
    with patch("aiohttp.ClientSession", return_value=_mock_session(content_type="image/png")):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    channel.send.assert_called_once()
    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"] == "Hello"
    embed = kwargs["embeds"][0]
    assert embed.image.url == "attachment://cat.png"
    assert embed.footer.text == "Footer text"


async def test_video_uses_thumbnail_and_adds_video_link():
    channel = _channel()
    payload = {
        "files": [{"filename": "clips/clip.mp4", "description": ""}],
    }
    with patch("aiohttp.ClientSession", return_value=_mock_session(
        content_type="video/mp4", video_link="https://cdn/clip.mp4"
    )):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    kwargs = channel.send.call_args.kwargs
    embed = kwargs["embeds"][0]
    assert embed.image.url == "attachment://clip.jpg"
    assert any("https://cdn/clip.mp4" in f.value for f in embed.fields)


async def test_missing_filename_raises():
    channel = _channel()
    payload = {"files": [{"description": "no path"}]}
    with patch("aiohttp.ClientSession", return_value=_mock_session()):
        with pytest.raises(RuntimeError, match="file missing filename/fileDir"):
            await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)


async def test_backend_non_200_raises():
    channel = _channel()
    payload = {"files": [{"filename": "img.png"}]}
    with patch("aiohttp.ClientSession", return_value=_mock_session(status=404)):
        with pytest.raises(RuntimeError, match="backend file failed: 404"):
            await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)


async def test_defaults_to_bots_channel_when_channel_missing():
    channel = _channel()
    client = _client(channel)
    payload = {"files": [{"filename": "img.png"}]}  # no "channel" key
    with patch("aiohttp.ClientSession", return_value=_mock_session()):
        await post_payload(payload, client, CHANNEL_IDS, BASE_URL, TOKEN)

    client.get_channel.assert_called_with(111)


async def test_no_header_sends_none_content():
    channel = _channel()
    payload = {"files": [{"filename": "img.png"}]}  # no "header"
    with patch("aiohttp.ClientSession", return_value=_mock_session()):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"] is None

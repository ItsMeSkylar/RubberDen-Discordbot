import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.discord_scripts import post_payload

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


async def _chunked(data: bytes):
    yield data


def _mock_session(data=b"bytes", content_type="image/png", status=200, video_link=None):
    """Build a mock session object (not a context manager — _get_http_session returns it directly)."""
    headers = {"Content-Type": content_type}
    if video_link:
        headers["X-Video-Link"] = video_link

    r = MagicMock()
    r.status = status
    r.text = AsyncMock(return_value="error body")
    r.headers = headers
    r.content.iter_chunked = MagicMock(side_effect=lambda _: _chunked(data))

    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=r)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get.return_value = get_cm

    return session


async def test_image_embed_has_image_and_footer():
    channel = _channel()
    payload = {
        "files": [{"filename": "pics/cat.png", "description": "a cat"}],
        "header": "Hello",
        "footer": "Footer text",
    }
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session(content_type="image/png"))):
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
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session(
        content_type="video/mp4", video_link="https://cdn/clip.mp4"
    ))):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    kwargs = channel.send.call_args.kwargs
    embed = kwargs["embeds"][0]
    assert embed.image.url == "attachment://clip.jpg"
    assert any("https://cdn/clip.mp4" in f.value for f in embed.fields)


async def test_undescribed_images_share_one_gallery_url():
    """Consecutive images with no description get the same embed url (merged grid)."""
    channel = _channel()
    payload = {
        "files": [
            {"filename": "pics/a.png"},
            {"filename": "pics/b.png"},
            {"filename": "pics/c.png"},
        ],
    }
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session(content_type="image/png"))):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    embeds = channel.send.call_args.kwargs["embeds"]
    assert len(embeds) == 3
    # All three share one url (single merged gallery), none carry text.
    assert len({e.url for e in embeds}) == 1
    assert embeds[0].url is not None
    assert all(e.description is None for e in embeds)


async def test_described_image_gets_its_own_embed():
    """A described image is standalone; undescribed neighbours still merge."""
    channel = _channel()
    payload = {
        "files": [
            {"filename": "pics/a.png", "description": "solo caption"},
            {"filename": "pics/b.png"},
            {"filename": "pics/c.png"},
        ],
    }
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session(content_type="image/png"))):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    embeds = channel.send.call_args.kwargs["embeds"]
    assert len(embeds) == 3
    # Described image: own embed, carries the caption, no gallery url.
    assert embeds[0].description == "solo caption"
    assert embeds[0].url is None
    # The two undescribed images merge into one gallery.
    assert embeds[1].url is not None
    assert embeds[1].url == embeds[2].url
    assert embeds[1].description is None


async def test_more_than_four_undescribed_images_split_into_separate_galleries():
    channel = _channel()
    payload = {"files": [{"filename": f"pics/{i}.png"} for i in range(5)]}
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session(content_type="image/png"))):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    embeds = channel.send.call_args.kwargs["embeds"]
    assert len(embeds) == 5
    # First 4 in one gallery, 5th in a second.
    assert len({e.url for e in embeds}) == 2
    assert embeds[0].url == embeds[3].url
    assert embeds[4].url != embeds[0].url


async def test_video_between_images_breaks_the_gallery_run():
    channel = _channel()
    payload = {
        "files": [
            {"filename": "pics/a.png"},
            {"filename": "clips/clip.mp4"},
            {"filename": "pics/b.png"},
        ],
    }

    def _session_for(path, **_):
        # The video item carries a video link; images do not.
        is_video = str(path).endswith(".mp4")
        return _mock_session(
            content_type="video/mp4" if is_video else "image/png",
            video_link="https://cdn/clip.mp4" if is_video else None,
        )

    # Each download builds its own response keyed on the requested path.
    session = MagicMock()

    def _get(url, params=None, headers=None):
        return _session_for(params["path"]).get.return_value

    session.get.side_effect = _get
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=session)):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    embeds = channel.send.call_args.kwargs["embeds"]
    assert len(embeds) == 3
    # The two images flank a video, so they land in separate galleries.
    assert embeds[0].url != embeds[2].url
    # The video embed has no gallery url and carries the link field.
    assert embeds[1].url is None
    assert any("https://cdn/clip.mp4" in f.value for f in embeds[1].fields)


async def test_missing_filename_raises():
    channel = _channel()
    payload = {"files": [{"description": "no path"}]}
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session())):
        with pytest.raises(RuntimeError, match="file missing filename/file_dir"):
            await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)


async def test_backend_non_200_raises():
    channel = _channel()
    payload = {"files": [{"filename": "img.png"}]}
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session(status=404))):
        with pytest.raises(RuntimeError, match="backend file failed: 404"):
            await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)


async def test_defaults_to_bots_channel_when_channel_missing():
    channel = _channel()
    client = _client(channel)
    payload = {"files": [{"filename": "img.png"}]}  # no "channel" key
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session())):
        await post_payload(payload, client, CHANNEL_IDS, BASE_URL, TOKEN)

    client.get_channel.assert_called_with(111)


async def test_no_header_sends_none_content():
    channel = _channel()
    payload = {"files": [{"filename": "img.png"}]}  # no "header"
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session())):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"] is None


# ─────────────────────────────────────────────────────
# Path traversal (#6)
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_path", [
    "../secrets.txt",
    "../../etc/passwd",
    "/etc/passwd",
    "\\Windows\\system32",
    "C:/Windows/system32",
    "C:\\Windows",
])
async def test_path_traversal_raises(bad_path):
    channel = _channel()
    payload = {"files": [{"filename": bad_path}]}
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session())):
        with pytest.raises((ValueError, RuntimeError)):
            await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)


# ─────────────────────────────────────────────────────
# Malformed Content-Length (#5)
# ─────────────────────────────────────────────────────

async def test_malformed_content_length_is_tolerated():
    """Non-integer Content-Length should be logged and skipped, not crash."""
    channel = _channel()
    payload = {"files": [{"filename": "img.png"}]}
    mock = _mock_session(data=b"bytes")
    mock.get.return_value.__aenter__.return_value.headers["Content-Length"] = "not-a-number"
    with patch("services.discord_scripts._get_http_session", AsyncMock(return_value=mock)), \
         patch("services.discord_scripts.scrub_metadata_bytes", AsyncMock(return_value=b"bytes")):
        await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)
    channel.send.assert_called_once()


# ─────────────────────────────────────────────────────
# File size limits (#12)
# ─────────────────────────────────────────────────────

async def test_file_too_large_via_content_length_raises():
    channel = _channel()
    payload = {"files": [{"filename": "big.png"}]}
    mock = _mock_session()
    mock.get.return_value.__aenter__.return_value.headers["Content-Length"] = "11"
    with patch("services.discord_scripts._MAX_DOWNLOAD_BYTES", 10), \
         patch("services.discord_scripts._get_http_session", AsyncMock(return_value=mock)):
        with pytest.raises(RuntimeError, match="file too large"):
            await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)


async def test_file_too_large_via_body_raises():
    channel = _channel()
    payload = {"files": [{"filename": "big.png"}]}
    with patch("services.discord_scripts._MAX_DOWNLOAD_BYTES", 10), \
         patch("services.discord_scripts._get_http_session", AsyncMock(return_value=_mock_session(data=b"x" * 11))):
        with pytest.raises(RuntimeError, match="file too large"):
            await post_payload(payload, _client(channel), CHANNEL_IDS, BASE_URL, TOKEN)

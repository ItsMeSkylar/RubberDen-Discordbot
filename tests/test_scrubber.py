import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.DiscordScripts import scrub_metadata_bytes

# Minimal valid JPEG with EXIF marker (APP1 = 0xFFE1)
# SOI + APP1 marker + length + "Exif\x00\x00" + dummy payload + EOI
_EXIF_JPEG = (
    b"\xff\xd8"                          # SOI
    b"\xff\xe1" + struct.pack(">H", 18)  # APP1 marker + length (18 bytes incl. length field)
    + b"Exif\x00\x00" + b"\x00" * 10    # Exif header + padding
    + b"\xff\xd9"                        # EOI
)

# Minimal valid JPEG without EXIF
_PLAIN_JPEG = b"\xff\xd8\xff\xd9"


# ─────────────────────────────
# Unit tests (mocked ffmpeg)
# ─────────────────────────────

def _make_proc(returncode=0, stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


async def test_success_returns_scrubbed_bytes():
    scrubbed = b"clean image data"
    proc = _make_proc(returncode=0)

    with patch("asyncio.create_subprocess_exec", return_value=proc), \
         patch("builtins.open", MagicMock(return_value=MagicMock(
             __enter__=lambda s: MagicMock(read=lambda: scrubbed),
             __exit__=MagicMock(return_value=False),
         ))):
        # We patch open only for the read; write is handled by tempfile
        pass

    # Use a real temp-file write but mock the subprocess and output read
    import builtins
    real_open = builtins.open

    def fake_open(path, mode="r", **kw):
        if "rb" in mode:
            m = MagicMock()
            m.__enter__ = lambda s: MagicMock(read=lambda: scrubbed)
            m.__exit__ = MagicMock(return_value=False)
            return m
        return real_open(path, mode, **kw)

    with patch("asyncio.create_subprocess_exec", return_value=proc), \
         patch("builtins.open", side_effect=fake_open):
        result = await scrub_metadata_bytes(_PLAIN_JPEG, "photo.jpg")

    assert result == scrubbed


async def test_ffmpeg_failure_returns_original():
    proc = _make_proc(returncode=1, stderr=b"some ffmpeg error")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await scrub_metadata_bytes(_PLAIN_JPEG, "photo.jpg")

    assert result == _PLAIN_JPEG


async def test_video_uses_copy_codec():
    proc = _make_proc(returncode=1)  # fail so we don't need to mock file read
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _make_proc(returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await scrub_metadata_bytes(b"videodata", "clip.mp4")

    assert "-c" in captured["args"]
    assert "copy" in captured["args"]


async def test_image_does_not_use_copy_codec():
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _make_proc(returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await scrub_metadata_bytes(_PLAIN_JPEG, "photo.png")

    assert "copy" not in captured["args"]
    assert "-q:v" in captured["args"]


async def test_map_metadata_flag_always_present():
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _make_proc(returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await scrub_metadata_bytes(_PLAIN_JPEG, "photo.jpg")

    assert "-map_metadata" in captured["args"]
    assert "-1" in captured["args"]


# ─────────────────────────────
# Integration test (real ffmpeg)
# ─────────────────────────────

def _make_real_jpeg_with_exif() -> bytes:
    """Create a real 1x1 JPEG with an EXIF comment using Pillow."""
    import io as _io
    import piexif
    from PIL import Image

    img = Image.new("RGB", (64, 64), color=(255, 0, 0))
    exif = piexif.dump({"0th": {piexif.ImageIFD.Make: b"TestCamera"}})
    buf = _io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


async def test_integration_scrubs_jpeg_exif():
    """Run real ffmpeg — verifies it's installed and strips EXIF from a valid JPEG."""
    jpeg = _make_real_jpeg_with_exif()
    assert b"TestCamera" in jpeg  # confirm EXIF is present before scrub

    result = await scrub_metadata_bytes(jpeg, "photo.jpg")

    assert len(result) > 0
    assert b"TestCamera" not in result

import os
import struct
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from services.scrubber import scrub_metadata_bytes

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
    """When ffmpeg succeeds, scrub_metadata_bytes returns the output file contents."""
    scrubbed = b"clean video data"

    real_mkstemp = tempfile.mkstemp
    proc = _make_proc(returncode=0)

    def fake_mkstemp(suffix=""):
        fd, path = real_mkstemp(suffix=suffix)
        # Pre-populate the output file with the "scrubbed" content so the
        # code's open(out_path, "rb").read() returns it.
        os.write(fd, scrubbed)
        return fd, path

    with patch("asyncio.create_subprocess_exec", return_value=proc), \
         patch("tempfile.mkstemp", side_effect=fake_mkstemp):
        result = await scrub_metadata_bytes(b"raw video data", "clip.mp4")

    assert result == scrubbed


async def test_ffmpeg_failure_returns_original():
    proc = _make_proc(returncode=1, stderr=b"some ffmpeg error")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await scrub_metadata_bytes(_PLAIN_JPEG, "photo.mp4")

    assert result == _PLAIN_JPEG


async def test_video_uses_copy_codec():
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _make_proc(returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await scrub_metadata_bytes(b"videodata", "clip.mp4")

    assert "-c" in captured["args"]
    assert "copy" in captured["args"]


async def test_image_scrub_does_not_call_ffmpeg():
    """Images are scrubbed via Pillow, not ffmpeg — create_subprocess_exec must not be called."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await scrub_metadata_bytes(_PLAIN_JPEG, "photo.png")

    mock_exec.assert_not_called()


async def test_map_metadata_flag_always_present():
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _make_proc(returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await scrub_metadata_bytes(b"videodata", "photo.mp4")

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

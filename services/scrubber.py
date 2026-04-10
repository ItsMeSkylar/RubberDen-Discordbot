import asyncio
import io
import logging
import os
import tempfile

from PIL import Image, ImageOps
from prometheus_client import Counter

log = logging.getLogger(__name__)

_scrub_total = Counter(
    "jenniferbot_scrub_total",
    "Metadata scrub operations",
    ["media_type", "outcome"],
)

_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _scrub_image_bytes(data: bytes) -> bytes:
    """Apply EXIF orientation physically, then strip all metadata. Returns scrubbed bytes."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # physically rotate based on EXIF orientation
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


async def _scrub_video_bytes(data: bytes, filename: str) -> bytes:
    """Strip metadata from video bytes using ffmpeg. Returns scrubbed bytes."""
    ext = os.path.splitext(filename)[1].lower() or ".bin"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_in:
        tmp_in.write(data)
        in_path = tmp_in.name

    out_fd, out_path = tempfile.mkstemp(suffix=ext)
    os.close(out_fd)

    try:
        args = ["ffmpeg", "-y", "-i", in_path, "-map_metadata", "-1", "-c", "copy", out_path]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            log.error("ffmpeg timed out for %s, using original", filename)
            _scrub_total.labels(media_type="video", outcome="timeout").inc()
            return data

        if proc.returncode != 0:
            log.error(
                "ffmpeg metadata scrub failed for %s: %s",
                filename,
                stderr[-500:].decode(errors="replace"),
            )
            _scrub_total.labels(media_type="video", outcome="failure").inc()
            return data  # fall back to original

        _scrub_total.labels(media_type="video", outcome="success").inc()
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(in_path)
        except OSError:
            pass
        try:
            os.unlink(out_path)
        except OSError:
            pass


async def scrub_metadata_bytes(data: bytes, filename: str) -> bytes:
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    if ext in _IMAGE_EXTS:
        try:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _scrub_image_bytes, data),
                timeout=30,
            )
            _scrub_total.labels(media_type="image", outcome="success").inc()
            return result
        except asyncio.TimeoutError:
            log.error("image scrub timed out for %s, using original", filename)
            _scrub_total.labels(media_type="image", outcome="timeout").inc()
            return data
        except Exception as e:
            log.error("Pillow scrub failed for %s: %s", filename, e)
            _scrub_total.labels(media_type="image", outcome="failure").inc()
            return data
    if ext in _VIDEO_EXTS:
        return await _scrub_video_bytes(data, filename)
    return data

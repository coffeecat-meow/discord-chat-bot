"""Bounded Discord CDN raster reads; model receives PNGs, never Lottie JSON."""

import base64
import binascii
import io
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, UnidentifiedImageError

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4_000_000
MAX_DECODE_PIXELS = 16_000_000
MAX_FRAMES = 120
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class MediaUnavailable(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Media unavailable: {status}")


def validate_url(url):
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
        or p.username is not None
        or p.password is not None
        or p.port not in {None, 443}
        or not p.path.startswith(("/attachments/", "/ephemeral-attachments/", "/stickers/"))
    ):
        raise ValueError("Media must come from a known Discord CDN source")
    return url


async def read_limited(content):
    data = bytearray()
    async for chunk in content.iter_chunked(65536):
        if len(data) + len(chunk) > MAX_BYTES:
            raise ValueError("Media byte limit exceeded")
        data.extend(chunk)
    if not data:
        raise ValueError("Empty media")
    return bytes(data)


async def download(url):
    # Bounded inline fixtures are also useful for an offline installation check.
    if url.startswith("data:image/"):
        if len(url) > MAX_BYTES * 4 // 3 + 100:
            raise ValueError("Media byte limit exceeded")
        head, separator, payload = url.partition(",")
        if not separator or head not in {
            "data:image/" + f + ";base64" for f in ("png", "jpeg", "webp", "gif")
        }:
            raise ValueError("Invalid inline image")
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid inline image") from exc
        if not data or len(data) > MAX_BYTES:
            raise ValueError("Media byte limit exceeded")
        return data
    validate_url(url)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=12), trust_env=False
    ) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise MediaUnavailable(response.status)
            size = response.headers.get("Content-Length")
            if size is not None and not 0 < int(size) <= MAX_BYTES:
                raise ValueError("Media byte limit exceeded")
            return await read_limited(response.content)


def frames(data, *, limit=3):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Media byte limit exceeded")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                raise ValueError("Unsupported raster format")
            pixels = image.width * image.height
            if not 0 < pixels <= MAX_PIXELS:
                raise ValueError("Media pixel limit exceeded")
            count = getattr(image, "n_frames", 1)
            if count > MAX_FRAMES or pixels * count > MAX_DECODE_PIXELS:
                raise ValueError("Animation work limit exceeded")
            indices = sorted({0, count // 2, count - 1})[:limit]
            urls, size = [], 0
            for index in indices:
                image.seek(index)
                frame = image.convert("RGBA")
                frame.thumbnail((1600, 1600))
                buffer = io.BytesIO()
                frame.save(buffer, format="PNG")
                raw = buffer.getvalue()
                size += len(raw)
                if size > MAX_OUTPUT_BYTES:
                    raise ValueError("Normalized image byte limit exceeded")
                urls.append("data:image/png;base64," + base64.b64encode(raw).decode("ascii"))
            return {
                "urls": urls,
                "sampled_frames": indices,
                "total_frames": count,
                "sample_only": count > 1,
            }
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid or oversized image") from exc


async def prepare(url):
    return frames(await download(url))

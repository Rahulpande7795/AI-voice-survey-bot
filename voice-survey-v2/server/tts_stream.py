"""
tts_stream.py — gTTS async wrapper with in-memory cache.

Cache key: "{lang}:{text}"
- Fixed survey phrases are pre-warmed at startup → always <1ms
- LLM ack text misses on first call, hits on any repeat

synthesise_chunks() streams MP3 in 4KB pieces so pipeline.py can
forward them to the browser while still generating later chunks.
"""
import asyncio
import io
import logging
import os

from gtts import gTTS

log = logging.getLogger("survey")

LANG     = os.getenv("TTS_LANG", "en")
TLD      = os.getenv("TTS_TLD",  "com")
CHUNK_SZ = 4096

# In-memory cache: "{lang}:{text}" → raw MP3 bytes
_cache: dict[str, bytes] = {}


def _blocking_synthesise(text: str, lang: str, tld: str) -> bytes:
    tts = gTTS(text=text, lang=lang, tld=tld, slow=False)
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    buf.seek(0)
    return buf.read()


async def synthesise(text: str) -> bytes:
    """Async wrapper — returns complete MP3 bytes. Caches result."""
    if not text or not text.strip():
        return b""
    key = f"{LANG}:{text}"
    if key in _cache:
        log.debug("TTS  HIT  %d chars", len(text))
        return _cache[key]
    log.debug("TTS  MISS %d chars — synthesising", len(text))
    mp3 = await asyncio.to_thread(_blocking_synthesise, text, LANG, TLD)
    _cache[key] = mp3
    return mp3


async def synthesise_chunks(text: str):
    """Async generator that yields MP3 in CHUNK_SZ pieces."""
    mp3_bytes = await synthesise(text)
    for i in range(0, len(mp3_bytes), CHUNK_SZ):
        yield mp3_bytes[i : i + CHUNK_SZ]


async def warm_cache(phrases: list[str]) -> None:
    """Pre-synthesise all survey phrases at startup so they hit in <1ms."""
    log.info("TTS  ▶  warming cache for %d phrases…", len(phrases))
    results = await asyncio.gather(
        *[synthesise(p) for p in phrases], return_exceptions=True
    )
    failed = sum(1 for r in results if isinstance(r, Exception))
    if failed:
        log.warning("TTS  ⚠  %d phrases failed to pre-warm", failed)
    else:
        log.info("TTS  ✔  cache warm — all %d phrases ready", len(phrases))
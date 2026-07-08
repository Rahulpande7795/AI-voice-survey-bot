"""
tts_stream.py — edge-tts neural TTS with bounded LRU in-memory cache.

~150ms uncached (vs 800ms gTTS). Hindi: hi-IN-SwaraNeural.

BUG FIXED:
  _cache changed from plain dict → OrderedDict.
  Plain dict has no move_to_end() method — caused AttributeError
  whenever _cache_put() tried to refresh an existing key's LRU position.
  This crash happened on any second request for the same phrase (i.e.
  every survey turn after the first), completely breaking TTS caching.

  Also fixed:
  - synthesise() and synthesise_chunks() now call move_to_end() on
    cache hit so frequently-used phrases are not evicted.
  - Eviction uses popitem(last=False) — cleaner than next(iter()).
"""
import asyncio
import logging
from collections import OrderedDict

import edge_tts
import config

log = logging.getLogger("survey")

TTS_VOICE = config.BEST_TTS_VOICE
CHUNK_SZ  = 4096

# LRU cap: at ~10KB per phrase, 500 entries ≈ 5MB RAM max
MAX_CACHE_ENTRIES = 500

# BUG FIX: must be OrderedDict — plain dict has no move_to_end() method
_cache: OrderedDict[str, bytes] = OrderedDict()


def _cache_put(key: str, value: bytes) -> None:
    """Insert into LRU cache, evicting oldest entry if at capacity."""
    if key in _cache:
        # Refresh LRU position so this entry is not evicted first
        _cache.move_to_end(key)
        return
    _cache[key] = value
    if len(_cache) > MAX_CACHE_ENTRIES:
        # Remove the OLDEST (first-inserted) entry
        _cache.popitem(last=False)


async def synthesise(text: str) -> bytes:
    """Synthesise `text` to MP3 bytes. Returns cached bytes if available."""
    if not text or not text.strip():
        return b""
    key = f"{TTS_VOICE}:{text}"
    if key in _cache:
        # BUG FIX: refresh LRU position on every cache hit
        _cache.move_to_end(key)
        return _cache[key]
    try:
        communicate = edge_tts.Communicate(text, TTS_VOICE)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        mp3 = b"".join(chunks)
        if mp3:
            _cache_put(key, mp3)
        return mp3
    except Exception as exc:
        log.error("TTS  ✘  synthesise: %s", exc)
        return b""


async def synthesise_chunks(text: str):
    """
    Synthesise `text` and yield CHUNK_SZ-byte MP3 chunks for streaming.
    Streams immediately even on cache miss to reduce TTFB.
    """
    if not text or not text.strip():
        return

    key = f"{TTS_VOICE}:{text}"
    if key in _cache:
        # BUG FIX: refresh LRU position on cache hit
        _cache.move_to_end(key)
        mp3 = _cache[key]
        for i in range(0, len(mp3), CHUNK_SZ):
            yield mp3[i: i + CHUNK_SZ]
        return

    try:
        communicate = edge_tts.Communicate(text, TTS_VOICE)
        collected: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                data = chunk["data"]
                collected.append(data)
                yield data
        # Cache the full audio after streaming completes
        mp3 = b"".join(collected)
        if mp3:
            _cache_put(key, mp3)
    except Exception as exc:
        log.error("TTS  ✘  synthesise_chunks: %s", exc)


async def warm_cache(phrases: list[str]) -> None:
    """Pre-synthesise all phrases concurrently at startup."""
    log.info("TTS  ▶  warming %d phrases  voice=%s…", len(phrases), TTS_VOICE)
    results = await asyncio.gather(
        *[synthesise(p) for p in phrases],
        return_exceptions=True
    )
    failed = sum(1 for r in results if isinstance(r, Exception))
    if failed:
        log.warning("TTS  ⚠  %d phrases failed to warm", failed)
    else:
        total_kb = sum(len(r) for r in results if isinstance(r, bytes)) / 1024
        log.info(
            "TTS  ✔  cache warm — %d phrases ready  (%.0f KB total)",
            len(phrases), total_kb
        )
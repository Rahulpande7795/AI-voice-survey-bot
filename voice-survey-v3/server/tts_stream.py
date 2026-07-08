"""
tts_stream.py — TASK 3 + 7: gTTS with Hindi support and in-memory cache.

TTS_LANG env var: "en" (default) or "hi" for Hindi.
All fixed node texts are pre-warmed at startup → <1ms on every call.
"""
import asyncio
import io
import logging
import os

from gtts import gTTS

log      = logging.getLogger("survey")
TTS_LANG = os.getenv("TTS_LANG", "en")
TTS_TLD  = os.getenv("TTS_TLD",  "co.in" if TTS_LANG == "hi" else "com")
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
    if not text or not text.strip():
        return b""
    key = f"{TTS_LANG}:{text}"
    if key in _cache:
        return _cache[key]
    mp3 = await asyncio.to_thread(_blocking_synthesise, text, TTS_LANG, TTS_TLD)
    _cache[key] = mp3
    return mp3


async def synthesise_chunks(text: str):
    mp3 = await synthesise(text)
    for i in range(0, len(mp3), CHUNK_SZ):
        yield mp3[i : i + CHUNK_SZ]


async def warm_cache(phrases: list[str]) -> None:
    log.info("TTS  ▶  warming %d phrases (lang=%s)…", len(phrases), TTS_LANG)
    results = await asyncio.gather(
        *[synthesise(p) for p in phrases], return_exceptions=True
    )
    failed = sum(1 for r in results if isinstance(r, Exception))
    if failed:
        log.warning("TTS  ⚠  %d phrases failed to warm", failed)
    else:
        log.info("TTS  ✔  cache warm — %d phrases ready", len(phrases))
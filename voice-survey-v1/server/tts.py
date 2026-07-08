"""
tts.py — Google Text-to-Speech (gTTS) wrapper.

100% free, no API key required, works globally including India.
Uses Google Translate's TTS endpoint under the hood.
Returns raw MP3 bytes ready to stream to the browser.
"""

import asyncio
import io
import os

from gtts import gTTS

# Language and TLD control the accent:
#   tld="com"    → US English
#   tld="co.uk"  → British English
#   tld="com.au" → Australian English
LANG = os.getenv("TTS_LANG", "en")
TLD  = os.getenv("TTS_TLD",  "com")


def _blocking_synthesise(text: str) -> bytes:
    """Run gTTS synchronously and return MP3 bytes (called in a thread)."""
    tts = gTTS(text=text, lang=LANG, tld=TLD, slow=False)
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    buf.seek(0)
    return buf.read()


async def synthesise(text: str) -> bytes:
    """
    Convert text to speech via gTTS and return raw MP3 bytes.
    Runs the blocking gTTS call in a thread pool so it doesn't block the
    FastAPI event loop.

    Args:
        text: The text to synthesise.

    Returns:
        MP3 audio bytes.
    """
    if not text or not text.strip():
        raise ValueError("Cannot synthesise empty text.")
    try:
        mp3_bytes = await asyncio.to_thread(_blocking_synthesise, text)
        return mp3_bytes
    except Exception as exc:
        raise RuntimeError(f"gTTS synthesis failed: {exc}") from exc
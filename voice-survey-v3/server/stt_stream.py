"""
stt_stream.py — TASK 3: Groq Whisper STT with Hindi/Hinglish support.

Set STT_LANGUAGE=hi in .env for Hindi. Default "en".
WebM EBML(Extensible Binary Meta Language) header fix preserved from V2.
"""
import logging
import os

from groq import AsyncGroq

log = logging.getLogger("survey")

MIN_AUDIO_BYTES = 3_000
WEBM_MAGIC      = b"\x1a\x45\xdf\xa3"
STT_LANGUAGE    = os.getenv("STT_LANGUAGE", "en")


class AudioCollector:
    """Accumulates WebM chunks; drops pre-EBML-header chunks."""

    def __init__(self):
        self._chunks: list[bytes] = []
        self._header_found = False

    def add(self, chunk: bytes):
        if len(chunk) < 50:
            return
        if not self._header_found:
            idx = chunk.find(WEBM_MAGIC)
            if idx != -1:
                self._header_found = True
                self._chunks.append(chunk[idx:])
            else:
                log.debug("STT  ⚠  pre-header chunk dropped (%.1f KB)", len(chunk) / 1024)
            return
        self._chunks.append(chunk)

    def assemble(self) -> bytes:
        return b"".join(self._chunks)

    def reset(self):
        self._chunks = []
        self._header_found = False

    @property
    def total_bytes(self) -> int:
        return sum(len(c) for c in self._chunks)

    @property
    def has_header(self) -> bool:
        return self._header_found


async def transcribe_blob(audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
    """
    Transcribe audio with Groq Whisper Large-v3-Turbo.
    Language is controlled by STT_LANGUAGE env var (default "en", use "hi" for Hindi).
    """
    if len(audio_bytes) < MIN_AUDIO_BYTES:
        log.warning("STT  ⚠  blob too small (%.1f KB)", len(audio_bytes) / 1024)
        return ""

    if audio_bytes[:4] != WEBM_MAGIC:
        log.warning("STT  ⚠  missing WebM magic bytes")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set in .env")

    client = AsyncGroq(api_key=api_key)

    try:
        response = await client.audio.transcriptions.create(
            file=("audio.webm", audio_bytes, "audio/webm"),
            model="whisper-large-v3-turbo",
            language=STT_LANGUAGE,   # "en" or "hi" — set in .env
            response_format="text",
            temperature=0.0,
        )
        transcript = (response if isinstance(response, str) else response.text).strip()
        return transcript

    except Exception as exc:
        raise RuntimeError(f"Groq Whisper failed: {exc}") from exc
"""
stt_stream.py — Groq Whisper Large-v3-Turbo STT.

Latency notes (India → Groq US):
  - Network RTT adds ~200-300ms unavoidable base overhead.
  - Smaller audio blobs upload faster. Client sends chunks every 100ms
    (down from 150ms) to reduce blob size without cutting off speech.
  - whisper-large-v3-turbo at 216x real-time: 5s audio → ~23ms compute.
    Most of the measured ~2500ms is upload time for the blob.

WebM header fix: browser MediaRecorder fires ondataavailable once before
writing the EBML(Extensible Binary Meta Language) magic bytes (1A 45 DF A3). We drop pre-header chunks.
"""
import logging
import os

from groq import AsyncGroq

log = logging.getLogger("survey")

MIN_AUDIO_BYTES = 3_000          # < 3 KB → too short for real speech
WEBM_MAGIC      = b"\x1a\x45\xdf\xa3"


class AudioCollector:
    """Accumulates raw WebM audio chunks for one user turn."""

    def __init__(self):
        self._chunks: list[bytes] = []
        self._header_found = False

    def add(self, chunk: bytes):
        if len(chunk) < 50:
            return  # discard noise / empty keepalives

        if not self._header_found:
            idx = chunk.find(WEBM_MAGIC)
            if idx != -1:
                self._header_found = True
                self._chunks.append(chunk[idx:])
            else:
                log.debug("STT  ⚠  pre-header chunk (%.1f KB) dropped", len(chunk) / 1024)
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
    Transcribe a complete WebM blob with Groq Whisper Large-v3-Turbo.
    Returns the transcript string (empty on silence/error).
    """
    if len(audio_bytes) < MIN_AUDIO_BYTES:
        log.warning("STT  ⚠  blob too small (%.1f KB) — skipped", len(audio_bytes) / 1024)
        return ""

    if audio_bytes[:4] != WEBM_MAGIC:
        log.warning("STT  ⚠  blob missing WebM magic bytes")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set in .env")

    client = AsyncGroq(api_key=api_key)

    try:
        response = await client.audio.transcriptions.create(
            file=("audio.webm", audio_bytes, "audio/webm"),
            model="whisper-large-v3-turbo",
            language="en",              # change to "hi" for Hindi in V3
            response_format="text",
            temperature=0.0,            # deterministic → faster, no sampling overhead
        )
        transcript = (response if isinstance(response, str) else response.text).strip()
        return transcript

    except Exception as exc:
        raise RuntimeError(f"Groq Whisper transcription failed: {exc}") from exc